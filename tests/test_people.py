"""Cross-app delivery: people, default delivery apps, and the @app override.

The point of the feature: capture on LINE, where replies are free, and deliver
on Discord, where pushes are unlimited, so LINE's 200/month push cap stops
mattering. The risk it must not introduce: rerouting or eating task text that
merely mentions an app.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.config import Person, Settings
from remindarr.core.delivery import (
    AtTokens,
    Delivery,
    Refusal,
    disclosure,
    label,
    read_at_tokens,
    resolve,
)
from remindarr.core.models import Reminder
from remindarr.core.router import Router
from remindarr.core.types import IncomingMessage, Route, Sender

from .conftest import NOW

LINE_ID = "U0123456789abcdef0123456789abcdef"
DISCORD_ID = "123456789012345678"
ALLOWED = f"line:{LINE_ID}, discord:{DISCORD_ID}, line:Umum, telegram:99"


def settings_with(people: str | None, **extra: Any) -> Settings:
    kwargs: dict[str, Any] = {
        "_env_file": None,
        "database_url": "sqlite+aiosqlite://",
        "allowed_users": ALLOWED,
        "people": people,
        # Tokens only so the platforms count as enabled; nothing connects.
        "discord_token": "x",
        "line_channel_secret": "s",
        "line_channel_access_token": "t",
    }
    kwargs.update(extra)
    return Settings(**kwargs)


OWNER = f"ozgur = line:{LINE_ID}, discord:{DISCORD_ID} > discord"


def msg(
    text: str,
    *,
    platform: str = "line",
    user_id: str = LINE_ID,
    route: str | None = None,
    direct: bool = True,
) -> IncomingMessage:
    return IncomingMessage(
        sender=Sender(platform=platform, user_id=user_id, display_name=""),
        route=Route(platform, route or user_id),
        text=text,
        received_at=NOW,
        direct=direct,
    )


class TestPeopleConfig:
    def test_parses_people_identities_and_default(self) -> None:
        s = settings_with(f"{OWNER}; Mum = line:Umum")
        owner, mum = s.people
        assert owner == Person(
            "ozgur", frozenset({f"line:{LINE_ID}", f"discord:{DISCORD_ID}"}), "discord"
        )
        assert mum == Person("mum", frozenset({"line:Umum"}), None)

    def test_from_env_with_spacing_and_trailing_separators(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "REMINDARR_PEOPLE", f"  ozgur=LINE:{LINE_ID} ,discord:{DISCORD_ID},>  Discord ;; "
        )
        settings = Settings(
            _env_file=None,
            allowed_users=ALLOWED,
            discord_token="x",
            line_channel_secret="s",
            line_channel_access_token="t",
        )
        [owner] = settings.people
        assert owner.deliver_on == "discord"
        assert owner.user_id_on("line") == LINE_ID
        assert owner.user_id_on("discord") == DISCORD_ID

    def test_unset_and_blank_mean_no_people(self) -> None:
        assert settings_with(None).people == ()
        assert settings_with("  ").people == ()

    def test_default_app_is_optional(self) -> None:
        [owner] = settings_with(f"ozgur = line:{LINE_ID}, discord:{DISCORD_ID}").people
        assert owner.deliver_on is None

    @pytest.mark.parametrize(
        ("people", "error"),
        [
            (f"line:{LINE_ID}", "is not 'name = platform:id"),  # no name
            (f"Ozgur Kose = line:{LINE_ID}", "is not 'name = platform:id"),  # space in name
            ("ozgur = ", "has no identities"),
            (f"ozgur = line:{LINE_ID}, line:Umum", "two line identities"),
            (f"ozgur = line:{LINE_ID}; mum = line:{LINE_ID}", "only one person"),
            (f"ozgur = line:{LINE_ID}; ozgur = line:Umum", "defined twice"),
            (f"ozgur = line:{LINE_ID} > discord", "has no discord identity"),
            (f"ozgur = line:{LINE_ID} > sms", "unknown platform 'sms'"),
            (f"ozgur = lnie:{LINE_ID}", "unknown platform 'lnie'"),
            (f"Discord = line:{LINE_ID}", "is a platform name"),  # @discord is reserved
            (f"ozgur = line:{LINE_ID} discord:{DISCORD_ID}", "is not platform:user_id"),
        ],
    )
    def test_malformed_people_are_rejected(self, people: str, error: str) -> None:
        with pytest.raises(ValidationError, match=error):
            settings_with(people)

    def test_every_identity_must_be_allow_listed(self) -> None:
        """Being in a person does not grant access: the allow-list stays the
        single place that decides who may use the bot."""
        with pytest.raises(ValidationError, match="not in REMINDARR_ALLOWED_USERS"):
            settings_with(f"ozgur = line:{LINE_ID}, discord:777")

    def test_default_app_must_be_enabled(self) -> None:
        with pytest.raises(ValidationError, match="not enabled"):
            settings_with(OWNER, discord_token=None)

    def test_person_of(self) -> None:
        s = settings_with(OWNER)
        assert s.person_of("discord", DISCORD_ID) is s.people[0]
        assert s.person_of("LINE", LINE_ID) is s.people[0]
        assert s.person_of("line", "Umum") is None


def route_of(result: Delivery | Refusal) -> Route:
    assert isinstance(result, Delivery), result
    return result.route


class TestDefaultRouting:
    def test_no_people_means_deliver_where_set(self) -> None:
        m = msg("x in 5m")
        assert route_of(resolve(m, m.text, settings_with(None))) == m.route

    def test_one_to_one_uses_the_persons_default_app(self) -> None:
        m = msg("x in 5m")
        assert route_of(resolve(m, m.text, settings_with(OWNER))) == Route(
            "discord", f"user:{DISCORD_ID}"
        )

    def test_group_reminder_stays_in_the_group(self) -> None:
        """A reminder set in a group is for the group."""
        m = msg("x in 5m", route="Cgroup", direct=False)
        assert route_of(resolve(m, m.text, settings_with(OWNER))) == m.route

    def test_default_on_the_same_app_is_here(self) -> None:
        m = msg("x in 5m", platform="discord", user_id=DISCORD_ID, route="dmchan")
        assert route_of(resolve(m, m.text, settings_with(OWNER))) == m.route

    def test_person_without_default_stays_here(self) -> None:
        m = msg("x in 5m")
        people = f"ozgur = line:{LINE_ID}, discord:{DISCORD_ID}"
        assert route_of(resolve(m, m.text, settings_with(people))) == m.route


class TestOverride:
    def test_override_reroutes_and_is_stripped(self) -> None:
        m = msg("water the plants @discord tomorrow at 9")
        result = resolve(m, m.text, settings_with(OWNER))
        assert result == Delivery(
            "water the plants tomorrow at 9", Route("discord", f"user:{DISCORD_ID}")
        )

    def test_override_beats_the_default(self) -> None:
        m = msg("x in 5m @line")
        assert route_of(resolve(m, m.text, settings_with(OWNER))) == m.route

    def test_override_from_a_group_goes_to_the_senders_dm(self) -> None:
        m = msg("x in 5m @line", route="Cgroup", direct=False)
        assert route_of(resolve(m, m.text, settings_with(OWNER))) == Route(
            "line", f"user:{LINE_ID}"
        )

    def test_override_is_case_insensitive(self) -> None:
        m = msg("x in 5m @Discord")
        assert route_of(resolve(m, m.text, settings_with(OWNER))).platform == "discord"

    @pytest.mark.parametrize(
        "text",
        [
            "post the update on discord tomorrow at 9",
            "tell line support about it tomorrow at 9",
            "email bob@discord.example tomorrow at 9",  # not whitespace-delimited
            "ping @bob tomorrow at 9",  # an unknown @word mid-text is just text
        ],
    )
    def test_prose_that_mentions_an_app_is_left_alone(self, text: str) -> None:
        """The parser's core rule: never silently eat task text."""
        m = msg(text, direct=False, route="Cgroup")
        assert resolve(m, text, settings_with(OWNER)) == Delivery(text, m.route)

    @pytest.mark.parametrize(
        "text",
        ["email @john about the invoice tomorrow at 9", "x in 5m @whatsapp", "@bob x in 5m"],
    )
    def test_other_at_words_stay_in_the_text(self, text: str) -> None:
        """Only app words are acted on. Anything else is task text -- and stays
        free to mean a person in a later feature."""
        m = msg(text)
        assert resolve(m, text, settings_with(None)) == Delivery(text, m.route)

    def test_at_word_is_kept_through_to_the_stored_reminder(self) -> None:
        tokens = read_at_tokens("email @john about the invoice @discord tomorrow")
        assert tokens == AtTokens("email @john about the invoice tomorrow", ("discord",))

    def test_two_targets_are_refused(self) -> None:
        m = msg("x @line in 5m @discord")
        result = resolve(m, m.text, settings_with(OWNER))
        assert isinstance(result, Refusal) and "Pick one" in result.reason

    def test_unlinked_target_is_refused(self) -> None:
        m = msg("x in 5m @discord", user_id="Umum")
        result = resolve(m, m.text, settings_with(OWNER))
        assert isinstance(result, Refusal)
        assert "don't know who you are on Discord" in result.reason

    def test_disabled_target_is_refused(self) -> None:
        m = msg("x in 5m @telegram")
        result = resolve(m, m.text, settings_with(OWNER))
        assert isinstance(result, Refusal) and "Telegram isn't set up" in result.reason


class TestWording:
    def test_disclosure_only_when_elsewhere(self) -> None:
        here = Route("line", LINE_ID)
        assert disclosure(here, here) is None
        assert (
            disclosure(Route("discord", f"user:{DISCORD_ID}"), Route("discord", "chan"))
            == "I'll DM you when it's due."
        )
        assert (
            disclosure(Route("discord", f"user:{DISCORD_ID}"), here)
            == "It'll arrive as a direct message on Discord."
        )

    def test_list_labels(self) -> None:
        here = Route("line", LINE_ID)
        assert label(here, here) == ""
        assert label(Route("discord", f"user:{DISCORD_ID}"), here) == " → Discord DM"
        assert label(Route("line", f"user:{LINE_ID}"), Route("line", "Cgroup")) == " → LINE DM"
        assert label(Route("discord", "chan"), here) == " → Discord"
        assert label(Route("line", "Cgroup"), here) == " → another chat"


def router(settings: Settings, factory: async_sessionmaker[AsyncSession]) -> Router:
    return Router(settings=settings, session_factory=factory, clock=lambda: NOW)


async def only_reminder(factory: async_sessionmaker[AsyncSession]) -> Reminder:
    async with factory() as s:
        return (await s.execute(select(Reminder))).scalar_one()


class TestRouter:
    async def test_capture_on_line_deliver_on_discord(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with(OWNER), session_factory)
        reply = await r.handle(msg("take the bins out tomorrow at 9"))
        assert reply is not None
        assert "It'll arrive as a direct message on Discord." in reply.text
        row = await only_reminder(session_factory)
        assert (row.delivery_platform, row.delivery_address) == ("discord", f"user:{DISCORD_ID}")
        assert row.text == "take the bins out"

    async def test_no_disclosure_when_delivered_here(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with(None), session_factory)
        reply = await r.handle(msg("take the bins out tomorrow at 9"))
        assert reply is not None and "arrive" not in reply.text

    async def test_task_text_mentioning_discord_is_kept_and_not_rerouted(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with(None), session_factory)
        await r.handle(msg("post the update on discord tomorrow at 9"))
        row = await only_reminder(session_factory)
        assert row.text == "post the update on discord"
        assert row.delivery_platform == "line"

    async def test_override_is_not_part_of_the_task(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with(OWNER), session_factory)
        reply = await r.handle(msg("call mum @line tomorrow at 9"))
        assert reply is not None and "“call mum”" in reply.text
        row = await only_reminder(session_factory)
        assert (row.text, row.delivery_platform) == ("call mum", "line")

    async def test_refusal_stores_nothing(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with(OWNER), session_factory)
        reply = await r.handle(msg("x @line tomorrow at 9 @discord"))
        assert reply is not None and "Pick one" in reply.text
        async with session_factory() as s:
            assert (await s.execute(select(Reminder))).first() is None

    async def test_list_shows_where_it_will_arrive(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with(OWNER), session_factory)
        await r.handle(msg("bins tomorrow at 9"))  # default: Discord
        await r.handle(msg("milk tomorrow at 10 @line"))  # here
        reply = await r.handle(msg("/list"))
        assert reply is not None
        lines = reply.text.splitlines()
        assert lines[0] == "Pending:"
        assert lines[1].startswith("  #1 bins — tomorrow") and lines[1].endswith(" → Discord DM")
        assert lines[2].startswith("  #2 milk — tomorrow") and "→" not in lines[2]

    async def test_cancel_accepts_the_hash_form_list_shows(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with(None), session_factory)
        await r.handle(msg("bins tomorrow at 9"))
        reply = await r.handle(msg("/cancel #1"))
        assert reply is not None and "Cancelled" in reply.text


def discord_channel_msg(text: str) -> IncomingMessage:
    """What the Discord adapter produces for a server-channel message."""
    return IncomingMessage(
        sender=Sender(platform="discord", user_id=DISCORD_ID, display_name=""),
        route=Route("discord", "chan42"),
        text=text,
        received_at=NOW,
        direct=False,
        deliver_privately=True,
    )


class TestPrivateDelivery:
    def test_channel_capture_goes_to_the_setters_dm_with_the_channel_as_fallback(self) -> None:
        m = discord_channel_msg("x in 5m")
        assert resolve(m, m.text, settings_with(OWNER)) == Delivery(
            "x in 5m", Route("discord", f"user:{DISCORD_ID}"), Route("discord", "chan42")
        )

    def test_same_app_override_keeps_the_fallback(self) -> None:
        m = discord_channel_msg("x in 5m @discord")
        result = resolve(m, m.text, settings_with(OWNER))
        assert isinstance(result, Delivery) and result.fallback == Route("discord", "chan42")

    def test_cross_app_override_from_a_channel_has_no_fallback(self) -> None:
        m = discord_channel_msg("x in 5m @line")
        assert resolve(m, m.text, settings_with(OWNER)) == Delivery(
            "x in 5m", Route("line", f"user:{LINE_ID}")
        )

    def test_line_group_is_unchanged(self) -> None:
        """LINE does not ask for private delivery: a group reminder is for the group."""
        m = msg("x in 5m", route="Cgroup", direct=False)
        assert resolve(m, m.text, settings_with(OWNER)) == Delivery("x in 5m", m.route)

    async def test_router_stores_the_fallback_and_says_it_will_dm(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with(None), session_factory)
        reply = await r.handle(discord_channel_msg("stand-up notes tomorrow at 9"))
        assert reply is not None and "I'll DM you when it's due." in reply.text
        row = await only_reminder(session_factory)
        assert (row.delivery_platform, row.delivery_address, row.fallback_address) == (
            "discord",
            f"user:{DISCORD_ID}",
            "chan42",
        )
