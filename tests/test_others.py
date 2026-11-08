"""Reminding someone else: ``take out the bins @user2 tomorrow 9pm``.

The setter types it and owns it (their /list, their /cancel, their timezone);
the recipient gets it, told who set it. Ids here are made up.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.adapters.base import DeliveryRegistry
from remindarr.adapters.discord import DiscordAdapter
from remindarr.config import Settings
from remindarr.core.delivery import Delivery, Refusal, read_at_tokens, resolve
from remindarr.core.models import Reminder
from remindarr.core.router import Router
from remindarr.core.scheduler import Scheduler
from remindarr.core.types import IncomingMessage, OutgoingMessage, Route, Sender

from .conftest import NOW, FakeAdapter, MutableClock
from .test_discord import FakeChannel, FakeClient, FakeUser, http_error

OZ_LINE, OZ_DISCORD = "Uoz0000000000000000000000000000000", "111111111111111111"
U2_LINE, U2_DISCORD = "Uu20000000000000000000000000000000", "222222222222222222"
KID_LINE = "Ukid000000000000000000000000000000"
GRANDPA_TG = "99"

PEOPLE = (
    f"oz = line:{OZ_LINE}, discord:{OZ_DISCORD} > discord;"
    f" user2 = line:{U2_LINE}, discord:{U2_DISCORD};"
    f" kid = line:{KID_LINE}"
)
ALLOWED = ", ".join(
    [
        f"line:{OZ_LINE}",
        f"discord:{OZ_DISCORD}",
        f"line:{U2_LINE}",
        f"discord:{U2_DISCORD}",
        f"line:{KID_LINE}",
        f"telegram:{GRANDPA_TG}",
    ]
)


def settings(**extra: Any) -> Settings:
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "database_url": "sqlite+aiosqlite://",
        "allowed_users": ALLOWED,
        "people": PEOPLE,
        "discord_token": "x",
        "telegram_token": "y",
        "line_channel_secret": "s",
        "line_channel_access_token": "t",
    }
    kwargs.update(extra)
    return Settings(**kwargs)


def line_dm(text: str, user_id: str = OZ_LINE) -> IncomingMessage:
    return IncomingMessage(
        sender=Sender("line", user_id, ""),
        route=Route("line", user_id),
        text=text,
        received_at=NOW,
        direct=True,
    )


def discord_channel(text: str, user_id: str = OZ_DISCORD) -> IncomingMessage:
    return IncomingMessage(
        sender=Sender("discord", user_id, "oz"),
        route=Route("discord", "chan42"),
        text=text,
        received_at=NOW,
        direct=False,
        deliver_privately=True,
    )


def delivered(result: Delivery | Refusal) -> Delivery:
    assert isinstance(result, Delivery), result
    return result


def refused(result: Delivery | Refusal) -> str:
    assert isinstance(result, Refusal), result
    return result.reason


class TestTokens:
    @pytest.mark.parametrize("token", ["@user2", "@User2", "@USER2"])
    def test_name_resolves_case_insensitively_and_is_stripped(self, token: str) -> None:
        tokens = read_at_tokens(
            f"take out the bins {token} tomorrow 9pm", settings(), platform="line"
        )
        assert tokens.text == "take out the bins tomorrow 9pm"
        assert [p.name for p in tokens.people] == ["user2"]

    @pytest.mark.parametrize("mention", [f"<@{U2_DISCORD}>", f"<@!{U2_DISCORD}>"])
    def test_discord_mention_of_a_person_resolves(self, mention: str) -> None:
        tokens = read_at_tokens(f"bins {mention} tomorrow", settings(), platform="discord")
        assert tokens.text == "bins tomorrow"
        assert [p.name for p in tokens.people] == ["user2"]

    def test_mention_of_a_stranger_or_on_another_app_stays_as_text(self) -> None:
        s = settings()
        assert read_at_tokens("bins <@333> tomorrow", s, platform="discord").text == (
            "bins <@333> tomorrow"
        )
        line = read_at_tokens(f"bins <@{U2_DISCORD}> tomorrow", s, platform="line")
        assert line.people == () and line.text == f"bins <@{U2_DISCORD}> tomorrow"

    def test_unknown_at_word_stays_as_text(self) -> None:
        tokens = read_at_tokens(
            "email @john about the invoice @user2 tomorrow", settings(), platform="line"
        )
        assert tokens.text == "email @john about the invoice tomorrow"

    def test_same_person_twice_is_one_person(self) -> None:
        tokens = read_at_tokens(
            f"x @user2 <@{U2_DISCORD}> tomorrow", settings(), platform="discord"
        )
        assert [p.name for p in tokens.people] == ["user2"]


class TestRouting:
    def test_recipients_default_app(self) -> None:
        """oz's default is Discord, so user2 reminding oz lands in oz's DMs."""
        m = line_dm("x @oz tomorrow at 9", user_id=U2_LINE)
        d = delivered(resolve(m, m.text, settings()))
        assert (d.route, d.recipient) == (Route("discord", f"user:{OZ_DISCORD}"), "oz")

    def test_recipients_only_identity(self) -> None:
        m = line_dm("x @kid tomorrow at 9")
        assert delivered(resolve(m, m.text, settings())).route == Route("line", f"user:{KID_LINE}")

    def test_several_apps_and_no_default_is_refused(self) -> None:
        m = line_dm("x @user2 tomorrow at 9")
        assert refused(resolve(m, m.text, settings())) == (
            "user2 has several apps; add @discord or @line to pick one."
        )

    def test_person_plus_app(self) -> None:
        m = line_dm("x @user2 @line tomorrow at 9")
        d = delivered(resolve(m, m.text, settings()))
        assert (d.text, d.route, d.recipient) == (
            "x tomorrow at 9",
            Route("line", f"user:{U2_LINE}"),
            "user2",
        )

    def test_recipient_not_on_the_requested_app(self) -> None:
        m = line_dm("x @kid @discord tomorrow at 9")
        assert refused(resolve(m, m.text, settings())) == "kid isn't on Discord."

    def test_two_recipients_are_refused(self) -> None:
        m = line_dm("x @user2 @kid tomorrow at 9")
        reason = refused(resolve(m, m.text, settings()))
        assert "One person per reminder" in reason and "@user2 and @kid" in reason

    def test_self_is_a_normal_reminder(self) -> None:
        m = line_dm("x @oz tomorrow at 9")
        d = delivered(resolve(m, m.text, settings()))
        # Own default (Discord), no recipient, the token gone.
        assert (d.text, d.recipient) == ("x tomorrow at 9", None)
        assert d.route == Route("discord", f"user:{OZ_DISCORD}")

    def test_sender_outside_people_can_remind_a_person(self) -> None:
        """Any allow-listed sender may remind anyone in REMINDARR_PEOPLE."""
        m = IncomingMessage(
            Sender("telegram", GRANDPA_TG, "Grandpa"),
            Route("telegram", GRANDPA_TG),
            "x @kid tomorrow at 9",
            NOW,
            direct=True,
        )
        assert delivered(resolve(m, m.text, settings())).recipient == "kid"

    def test_discord_channel_capture_falls_back_to_that_channel(self) -> None:
        m = discord_channel(f"x <@{U2_DISCORD}> tomorrow at 9")
        d = delivered(
            resolve(
                m,
                m.text,
                settings(
                    people=PEOPLE.replace(
                        f"discord:{U2_DISCORD};", f"discord:{U2_DISCORD} > discord;"
                    )
                ),
            )
        )
        assert d.route == Route("discord", f"user:{U2_DISCORD}")
        assert d.fallback == Route("discord", "chan42")

    def test_no_fallback_from_a_dm_or_another_app(self) -> None:
        """Falling back into the setter's own DM would put the recipient's
        reminder where the recipient can't see it."""
        dm = IncomingMessage(
            Sender("discord", OZ_DISCORD, "oz"),
            Route("discord", "ozdm"),
            "x @user2 @discord tomorrow at 9",
            NOW,
            direct=True,
        )
        assert delivered(resolve(dm, dm.text, settings())).fallback is None
        other_app = discord_channel("x @user2 @line tomorrow at 9")
        assert delivered(resolve(other_app, other_app.text, settings())).fallback is None


def router(s: Settings, factory: async_sessionmaker[AsyncSession], clock: Any = None) -> Router:
    return Router(settings=s, session_factory=factory, clock=clock or (lambda: NOW))


async def reminders(factory: async_sessionmaker[AsyncSession]) -> list[Reminder]:
    async with factory() as s:
        return list((await s.execute(select(Reminder))).scalars())


class TestRouter:
    async def test_confirmation_names_recipient_app_and_attribution(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings(), session_factory)
        reply = await r.handle(line_dm("take out the bins @kid tomorrow at 21:00"))
        assert reply is not None
        assert "“take out the bins” tomorrow at 21:00" in reply.text
        assert "It'll go to kid as a direct message on LINE, marked “from oz”." in reply.text
        [row] = await reminders(session_factory)
        assert (row.text, row.recipient, row.delivery_address) == (
            "take out the bins",
            "kid",
            f"user:{KID_LINE}",
        )

    async def test_parsed_in_the_setters_timezone(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings(), session_factory)
        await r.handle(line_dm("/tz Europe/Berlin"))
        await r.handle(line_dm("bins @kid tomorrow at 9"))
        [row] = await reminders(session_factory)
        local = row.due_at.astimezone(ZoneInfo("Europe/Berlin"))
        assert (local.hour, local.minute) == (9, 0)

    async def test_setter_lists_and_cancels_it_recipient_does_not_see_it(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings(), session_factory)
        await r.handle(line_dm("bins @kid tomorrow at 9"))
        listing = await r.handle(line_dm("/list"))
        assert listing is not None and listing.text.splitlines()[1].endswith(" → kid on LINE")
        theirs = await r.handle(line_dm("/list", user_id=KID_LINE))
        assert theirs is not None and theirs.text == "Nothing pending."
        not_theirs = await r.handle(line_dm("/cancel 1", user_id=KID_LINE))
        assert not_theirs is not None and "No reminder" in not_theirs.text
        mine = await r.handle(line_dm("/cancel #1"))
        assert mine is not None and "Cancelled" in mine.text


async def deliver_one(
    s: Settings, factory: async_sessionmaker[AsyncSession], message: IncomingMessage
) -> OutgoingMessage:
    clock = MutableClock()
    await router(s, factory, clock).handle(message)
    adapter = FakeAdapter(platform=(await reminders(factory))[0].delivery_platform)
    registry = DeliveryRegistry()
    registry.register(adapter)
    clock.advance(timedelta(days=2))
    await Scheduler(settings=s, session_factory=factory, registry=registry, clock=clock).tick()
    [sent] = adapter.sent
    return sent


class TestAttribution:
    async def test_recipient_is_told_who_set_it(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        sent = await deliver_one(settings(), session_factory, line_dm("bins @kid tomorrow at 9"))
        assert sent.text.startswith("⏰ bins (from oz)")
        assert sent.route == Route("line", f"user:{KID_LINE}")

    async def test_setter_without_a_person_uses_their_display_name(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        m = IncomingMessage(
            Sender("telegram", GRANDPA_TG, "Grandpa"),
            Route("telegram", GRANDPA_TG),
            "bins @kid tomorrow at 9",
            NOW,
            direct=True,
        )
        sent = await deliver_one(settings(), session_factory, m)
        assert sent.text.startswith("⏰ bins (from Grandpa)")

    async def test_own_reminder_has_no_attribution(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        sent = await deliver_one(settings(), session_factory, line_dm("bins tomorrow at 9"))
        assert "(from" not in sent.text


class TestDiscordFallbackMentionsTheRecipient:
    async def test_refused_dm_pings_only_the_recipient_in_the_setters_channel(self) -> None:
        """The fallback channel is the setter's; the mention must be the
        recipient's, and nobody else -- not the setter, not @everyone, not the
        role or user mentioned in the text -- may be pinged."""
        client = FakeClient()
        client.users[int(U2_DISCORD)] = FakeUser(
            FakeChannel(fail=http_error(discord.Forbidden, 403, 50007))
        )
        chan = FakeChannel()
        client.channels[42] = chan
        adapter = DiscordAdapter("t", client=client)  # type: ignore[arg-type]
        await adapter.send(
            OutgoingMessage(
                route=Route("discord", f"user:{U2_DISCORD}"),
                text=f"⏰ bins @everyone @here <@&555> <@{OZ_DISCORD}> (from oz)",
                fallback=Route("discord", "42"),
            )
        )
        [(text, mentions)] = chan.sent
        assert text.startswith(f"<@{U2_DISCORD}> ")
        assert text.count(f"<@{U2_DISCORD}>") == 1
        assert mentions.everyone is False and mentions.roles is False
        assert [u.id for u in mentions.users] == [int(U2_DISCORD)]
