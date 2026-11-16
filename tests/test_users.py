"""Users in the database: the config import, admins, invites, linking."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from remindarr.config import Settings
from remindarr.core import access
from remindarr.core.directory import bootstrap
from remindarr.core.directory import load as load_directory
from remindarr.core.models import Reminder, ReminderState, Subscriber, User
from remindarr.core.router import BAD_CODE, Router
from remindarr.core.types import IncomingMessage, Route, Sender

from .conftest import NOW, MutableClock

OWNER_LINE = "Uowner"
OWNER_DISCORD = "111"


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "database_url": "sqlite+aiosqlite://",
        "default_timezone": "Asia/Tokyo",
        "line_channel_secret": "s",
        "line_channel_access_token": "t",
        "discord_token": "d",
        "allowed_users": f"line:{OWNER_LINE},discord:{OWNER_DISCORD}",
        "people": f"oz = line:{OWNER_LINE}, discord:{OWNER_DISCORD}",
        "admins": "oz",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def msg(
    text: str, *, platform: str = "line", user_id: str = OWNER_LINE, name: str = ""
) -> IncomingMessage:
    return IncomingMessage(
        sender=Sender(platform=platform, user_id=user_id, display_name=name or user_id),
        route=Route(platform, user_id),
        text=text,
        received_at=NOW,
        direct=True,
    )


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def router(session_factory: async_sessionmaker[AsyncSession], clock: MutableClock) -> Router:
    return Router(settings=make_settings(), session_factory=session_factory, clock=clock)


async def say(router: Router, text: str, **kw: str) -> str:
    reply = await router.handle(msg(text, **kw))
    assert reply is not None
    return reply.text


def code_in(text: str, command: str) -> str:
    match = re.search(rf"/{command} ([0-9A-Z]{{4}}-[0-9A-Z]{{4}})", text)
    assert match, text
    return match.group(1)


async def users(factory: async_sessionmaker[AsyncSession]) -> dict[str, User]:
    async with factory() as s:
        return {u.name: u for u in (await s.execute(select(User))).scalars()}


class TestImport:
    async def test_people_and_allow_list_become_users(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = make_settings(
            allowed_users=f"line:{OWNER_LINE},discord:{OWNER_DISCORD},telegram:999",
            people=f"oz = line:{OWNER_LINE}, discord:{OWNER_DISCORD} > discord",
        )
        async with session_factory() as s:
            result = await bootstrap(s, settings, now=NOW)
            await s.commit()
            people = await load_directory(s, settings)
        assert result.imported and result.users == 2 and result.admins == 1
        oz = people.person_of("line", OWNER_LINE)
        assert oz is not None and oz.name == "oz" and oz.deliver_on == "discord"
        assert people.person_of("discord", OWNER_DISCORD) == oz
        stray = people.person_of("telegram", "999")
        assert stray is not None and stray.auto_named and stray.name == "telegram-999"

    async def test_config_is_ignored_once_users_exist(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Seed only: adding someone to the env file later grants nothing."""
        async with session_factory() as s:
            await bootstrap(s, make_settings(), now=NOW)
            await s.commit()
        later = make_settings(allowed_users=f"line:{OWNER_LINE},discord:{OWNER_DISCORD},line:Unew")
        async with session_factory() as s:
            result = await bootstrap(s, later, now=NOW)
            await s.commit()
            people = await load_directory(s, later)
        assert not result.imported
        assert not people.is_allowed("line", "Unew")


class TestAdmins:
    async def test_admin_identity_bootstraps_a_fresh_install(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = make_settings(allowed_users=None, people=None, admins=f"line:{OWNER_LINE}")
        async with session_factory() as s:
            result = await bootstrap(s, settings, now=NOW)
            await s.commit()
            people = await load_directory(s, settings)
        assert (result.users, result.admins) == (1, 1)
        assert people.is_admin("line", OWNER_LINE)
        owner = people.person_of("line", OWNER_LINE)
        assert owner is not None and owner.name == "owner"

    async def test_unknown_admin_name_is_logged_not_fatal(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        with capture_logs() as logs:
            async with session_factory() as s:
                await bootstrap(s, make_settings(admins="nobody"), now=NOW)
        assert any(e["event"] == "users.admin_unknown" for e in logs)

    def test_bad_admin_entry_is_a_config_error(self) -> None:
        with pytest.raises(ValueError, match="admins entry"):
            make_settings(admins="Not A Name")


class TestInvite:
    async def test_invite_then_join(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        reply = await say(router, "/invite Mum")
        assert "mum" in reply and "24 hours" in reply
        code = code_in(reply, "join")

        # Before joining, mum is a stranger.
        assert (await say(router, "/help", user_id="Umum")).startswith("Not authorised")
        welcome = await say(router, f"/join {code}", user_id="Umum")
        assert welcome.startswith("Welcome, mum!")

        # Now she's in and can set reminders.
        assert "water" in await say(router, "water the plants tomorrow at 9", user_id="Umum")
        assert "mum" in await users(session_factory)

    async def test_codes_forgive_case_hyphen_and_lookalikes(self, router: Router) -> None:
        code = code_in(await say(router, "/invite mum"), "join")
        sloppy = code.replace("-", "").lower().replace("0", "o").replace("1", "l")
        assert (await say(router, f"/join {sloppy}", user_id="Umum")).startswith("Welcome")

    async def test_code_works_once(self, router: Router) -> None:
        code = code_in(await say(router, "/invite mum"), "join")
        await say(router, f"/join {code}", user_id="Umum")
        assert await say(router, f"/join {code}", user_id="Uother") == BAD_CODE

    async def test_code_expires(self, router: Router, clock: MutableClock) -> None:
        code = code_in(await say(router, "/invite mum"), "join")
        clock.advance(access.INVITE_TTL + timedelta(seconds=1))
        assert await say(router, f"/join {code}", user_id="Umum") == BAD_CODE

    async def test_an_invite_code_is_not_a_link_code(self, router: Router) -> None:
        code = code_in(await say(router, "/invite mum"), "join")
        assert await say(router, f"/link {code}", user_id="Umum") == BAD_CODE

    async def test_only_admins_invite(self, router: Router) -> None:
        code = code_in(await say(router, "/invite mum"), "join")
        await say(router, f"/join {code}", user_id="Umum")
        assert await say(router, "/invite dad", user_id="Umum") == "Only an admin can /invite."

    @pytest.mark.parametrize("name", ["line", "Bad Name", "x" * 40, "@mum"])
    async def test_bad_names_are_refused(self, router: Router, name: str) -> None:
        assert "Names are" in await say(router, f"/invite {name}")

    async def test_existing_name_is_refused(self, router: Router) -> None:
        assert "already someone called oz" in await say(router, "/invite oz")

    async def test_two_invites_for_one_name_only_one_wins(self, router: Router) -> None:
        first = code_in(await say(router, "/invite mum"), "join")
        second = code_in(await say(router, "/invite mum"), "join")
        await say(router, f"/join {first}", user_id="Umum")
        assert await say(router, f"/join {second}", user_id="Ustranger") == BAD_CODE

    async def test_already_in(self, router: Router) -> None:
        code = code_in(await say(router, "/invite mum"), "join")
        assert "already in, as oz" in await say(router, f"/join {code}")


class TestRateLimit:
    async def test_too_many_wrong_codes_blocks_for_an_hour(
        self, router: Router, clock: MutableClock
    ) -> None:
        code = code_in(await say(router, "/invite mum"), "join")
        for _ in range(5):
            assert await say(router, "/join AAAA-AAAA", user_id="Umum") == BAD_CODE
        # Even the right code is refused while blocked...
        assert "Too many" in await say(router, f"/join {code}", user_id="Umum")
        # ...and works again after the window.
        clock.advance(timedelta(hours=1, seconds=1))
        assert (await say(router, f"/join {code}", user_id="Umum")).startswith("Welcome")

    async def test_garbage_counts_as_a_failure(self, router: Router) -> None:
        for _ in range(5):
            await say(router, "/join hello", user_id="Umum")
        assert "Too many" in await say(router, "/join AAAA-AAAA", user_id="Umum")


class TestLink:
    async def test_link_a_second_app(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        mum = code_in(await say(router, "/invite mum"), "join")
        await say(router, f"/join {mum}", user_id="Umum")
        await say(router, "/tz Europe/London", user_id="Umum")

        code = code_in(await say(router, "/link", user_id="Umum"), "link")
        reply = await say(router, f"/link {code}", platform="discord", user_id="222")
        assert "now part of mum" in reply

        async with session_factory() as s:
            people = await load_directory(s, make_settings())
            [linked] = (
                await s.execute(select(Subscriber).where(Subscriber.platform_user_id == "222"))
            ).scalars()
        assert people.person_of("discord", "222") == people.person_of("line", "Umum")
        assert linked.timezone == "Europe/London"  # one person, one clock

    async def test_one_identity_per_app(self, router: Router) -> None:
        code = code_in(await say(router, "/link"), "link")
        reply = await say(router, f"/link {code}", user_id="Uanother")
        assert "already has a LINE account linked" in reply

    async def test_link_code_expires_quickly(self, router: Router, clock: MutableClock) -> None:
        code = code_in(await say(router, "/link"), "link")
        clock.advance(access.LINK_TTL + timedelta(seconds=1))
        assert await say(router, f"/link {code}", platform="telegram", user_id="9") == BAD_CODE

    async def test_linked_account_cannot_redeem(self, router: Router) -> None:
        code = code_in(await say(router, "/link"), "link")
        assert "already linked to oz" in await say(router, f"/link {code}")


class TestTimezone:
    async def test_tz_applies_to_every_linked_app(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await say(router, "/tz America/New_York")
        async with session_factory() as s:
            subs = (
                await s.execute(
                    select(Subscriber).where(
                        Subscriber.platform_user_id.in_([OWNER_LINE, OWNER_DISCORD])
                    )
                )
            ).scalars()
            assert {sub.timezone for sub in subs} == {"America/New_York"}


class TestDeliver:
    async def test_show_set_and_reset(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        assert "wherever you set them" in await say(router, "/deliver")
        assert "now arrive on Discord" in await say(router, "/deliver discord")
        assert (await users(session_factory))["oz"].deliver_on == "discord"
        assert "on Discord" in await say(router, "/deliver")
        assert "wherever you set them" in await say(router, "/deliver here")
        assert (await users(session_factory))["oz"].deliver_on is None

    async def test_unlinked_app_is_refused(self, router: Router) -> None:
        mum = code_in(await say(router, "/invite mum"), "join")
        await say(router, f"/join {mum}", user_id="Umum")
        assert "haven't linked Discord" in await say(router, "/deliver discord", user_id="Umum")

    async def test_disabled_app_is_refused(self, router: Router) -> None:
        assert "isn't set up here" in await say(router, "/deliver telegram")


class TestUsersAndRemove:
    async def test_users_lists_everyone(self, router: Router) -> None:
        mum = code_in(await say(router, "/invite mum"), "join")
        await say(router, f"/join {mum}", user_id="Umum")
        listing = await say(router, "/users")
        assert "oz: Discord, LINE (admin)" in listing
        assert "mum: LINE" in listing

    async def test_remove_takes_them_out_and_cancels_their_reminders(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        mum = code_in(await say(router, "/invite mum"), "join")
        await say(router, f"/join {mum}", user_id="Umum")
        await say(router, "her own thing tomorrow at 9", user_id="Umum")
        await say(router, "for mum @mum tomorrow at 9")  # set by oz, delivered to mum
        await say(router, "oz's own thing tomorrow at 9")

        reply = await say(router, "/remove mum")
        assert reply == "Removed mum. Cancelled 2 pending reminder(s)."
        assert (await say(router, "/help", user_id="Umum")).startswith("Not authorised")
        async with session_factory() as s:
            states = {r.text: r.state for r in (await s.execute(select(Reminder))).scalars()}
        assert states == {
            "her own thing": ReminderState.CANCELLED,
            "for mum": ReminderState.CANCELLED,
            "oz's own thing": ReminderState.PENDING,
        }

    async def test_cannot_remove_yourself(self, router: Router) -> None:
        assert await say(router, "/remove oz") == "You can't remove yourself."

    async def test_non_admin_cannot_remove(self, router: Router) -> None:
        mum = code_in(await say(router, "/invite mum"), "join")
        await say(router, f"/join {mum}", user_id="Umum")
        assert await say(router, "/remove oz", user_id="Umum") == "Only an admin can /remove."

    async def test_admin_help_only_for_admins(self, router: Router) -> None:
        assert "/invite" in await say(router, "/help")
        mum = code_in(await say(router, "/invite mum"), "join")
        await say(router, f"/join {mum}", user_id="Umum")
        assert "/invite" not in await say(router, "/help", user_id="Umum")


class TestPeople:
    async def test_invited_user_is_reachable_by_name(self, router: Router) -> None:
        mum = code_in(await say(router, "/invite mum"), "join")
        await say(router, f"/join {mum}", user_id="Umum")
        reply = await say(router, "call grandpa @mum tomorrow at 9")
        assert "It'll go to mum" in reply

    async def test_auto_named_user_cannot_be_at_mentioned(
        self, session_factory: async_sessionmaker[AsyncSession], clock: MutableClock
    ) -> None:
        r = Router(
            settings=make_settings(
                allowed_users=f"line:{OWNER_LINE},discord:{OWNER_DISCORD},line:Ustray"
            ),
            session_factory=session_factory,
            clock=clock,
        )
        reply = await say(r, "ping @line-ustray tomorrow at 9")
        assert "It'll go to" not in reply
