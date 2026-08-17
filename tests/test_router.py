"""Router tests: message in, reply out, correct rows written."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.config import Settings
from remindarr.core.models import Reminder, ReminderState, Subscriber
from remindarr.core.router import Router

from .conftest import NOW, message


@pytest.fixture
def router(settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> Router:
    return Router(settings=settings, session_factory=session_factory, clock=lambda: NOW)


async def reminders(factory: async_sessionmaker[AsyncSession]) -> list[Reminder]:
    async with factory() as s:
        return list((await s.execute(select(Reminder))).scalars())


class TestCreating:
    async def test_creates_a_reminder(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        reply = await router.handle(message("call the dentist tomorrow at 9"))
        assert reply is not None

        rows = await reminders(session_factory)
        assert len(rows) == 1
        assert rows[0].text == "call the dentist"
        assert rows[0].state is ReminderState.PENDING
        assert rows[0].delivery_platform == "telegram"
        assert rows[0].delivery_address == "c1"

    async def test_confirmation_states_the_resolved_time(self, router: Router) -> None:
        reply = await router.handle(message("call the dentist tomorrow at 9"))
        assert reply is not None
        # The absolute time must be echoed -- that is the only way the user can
        # catch a misparse before it costs them.
        assert "09:00" in reply.text
        assert "tomorrow" in reply.text
        assert "call the dentist" in reply.text

    async def test_next_attempt_starts_equal_to_due(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await router.handle(message("x in 10 minutes"))
        row = (await reminders(session_factory))[0]
        assert row.next_attempt_at == row.due_at

    async def test_unparseable_time_explains_itself(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        reply = await router.handle(message("buy milk"))
        assert reply is not None
        assert "/help" in reply.text
        assert await reminders(session_factory) == []

    async def test_command_words_in_reminder_text_are_not_commands(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Regression: "list the groceries..." must not be swallowed by /list."""
        reply = await router.handle(message("list the groceries tomorrow at 9"))
        assert reply is not None
        rows = await reminders(session_factory)
        assert len(rows) == 1
        assert rows[0].text == "list the groceries"

    async def test_empty_message_is_ignored(self, router: Router) -> None:
        assert await router.handle(message("   ")) is None


class TestSubscribers:
    async def test_first_message_creates_a_subscriber_with_default_tz(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await router.handle(message("/help"))
        async with session_factory() as s:
            subs = list((await s.execute(select(Subscriber))).scalars())
        assert len(subs) == 1
        assert subs[0].timezone == "Asia/Tokyo"
        assert subs[0].platform == "telegram"

    async def test_repeat_messages_reuse_one_subscriber(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        for _ in range(3):
            await router.handle(message("x in 5m"))
        async with session_factory() as s:
            assert len(list((await s.execute(select(Subscriber))).scalars())) == 1

    async def test_same_user_id_on_another_platform_is_a_separate_subscriber(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from remindarr.core.types import IncomingMessage, Route, Sender

        await router.handle(message("x in 5m", user_id="42"))
        await router.handle(
            IncomingMessage(
                sender=Sender(platform="discord", user_id="42", display_name="same id"),
                route=Route("discord", "chan"),
                text="y in 5m",
                received_at=NOW,
            )
        )
        async with session_factory() as s:
            assert len(list((await s.execute(select(Subscriber))).scalars())) == 2


class TestCommands:
    async def test_help(self, router: Router) -> None:
        reply = await router.handle(message("/help"))
        assert reply is not None and "/cancel" in reply.text

    async def test_help_with_telegram_group_suffix(self, router: Router) -> None:
        reply = await router.handle(message("/help@remindarr_bot"))
        assert reply is not None and "/cancel" in reply.text

    async def test_unknown_command(self, router: Router) -> None:
        reply = await router.handle(message("/frobnicate"))
        assert reply is not None and "Unknown command" in reply.text

    async def test_list_empty(self, router: Router) -> None:
        reply = await router.handle(message("/list"))
        assert reply is not None and "Nothing pending" in reply.text

    async def test_list_shows_pending_with_ids(self, router: Router) -> None:
        await router.handle(message("water the plants tomorrow at 9"))
        reply = await router.handle(message("/list"))
        assert reply is not None
        assert "water the plants" in reply.text
        assert "1." in reply.text

    async def test_cancel(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await router.handle(message("x tomorrow at 9"))
        reply = await router.handle(message("/cancel 1"))
        assert reply is not None and "Cancelled" in reply.text
        assert (await reminders(session_factory))[0].state is ReminderState.CANCELLED

    async def test_cancel_needs_an_id(self, router: Router) -> None:
        reply = await router.handle(message("/cancel"))
        assert reply is not None and "Which one" in reply.text

    async def test_cancel_unknown_id(self, router: Router) -> None:
        reply = await router.handle(message("/cancel 99"))
        assert reply is not None and "No reminder" in reply.text

    async def test_cancel_twice(self, router: Router) -> None:
        await router.handle(message("x tomorrow at 9"))
        await router.handle(message("/cancel 1"))
        reply = await router.handle(message("/cancel 1"))
        assert reply is not None and "already" in reply.text

    async def test_cannot_cancel_another_subscribers_reminder(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Reminder ids are global, so scoping the lookup is what stops one
        user cancelling another's by guessing a number."""
        await router.handle(message("private thing tomorrow at 9", user_id="alice"))
        reply = await router.handle(message("/cancel 1", user_id="mallory"))
        assert reply is not None and "No reminder" in reply.text
        assert (await reminders(session_factory))[0].state is ReminderState.PENDING

    async def test_list_is_scoped_per_subscriber(self, router: Router) -> None:
        await router.handle(message("alice thing tomorrow at 9", user_id="alice"))
        reply = await router.handle(message("/list", user_id="bob"))
        assert reply is not None and "Nothing pending" in reply.text


class TestTimezone:
    async def test_shows_current(self, router: Router) -> None:
        reply = await router.handle(message("/tz"))
        assert reply is not None and "Asia/Tokyo" in reply.text

    async def test_sets(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        reply = await router.handle(message("/tz Europe/Berlin"))
        assert reply is not None and "Europe/Berlin" in reply.text
        async with session_factory() as s:
            sub = (await s.execute(select(Subscriber))).scalar_one()
        assert sub.timezone == "Europe/Berlin"

    async def test_rejects_nonsense(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        reply = await router.handle(message("/tz Mars/Olympus_Mons"))
        assert reply is not None and "do not know" in reply.text
        async with session_factory() as s:
            sub = (await s.execute(select(Subscriber))).scalar_one()
        assert sub.timezone == "Asia/Tokyo"

    async def test_new_timezone_changes_how_reminders_resolve(
        self, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await router.handle(message("a tomorrow at 9"))
        await router.handle(message("/tz Europe/London"))
        await router.handle(message("b tomorrow at 9"))
        rows = await reminders(session_factory)
        # Same wall-clock request, different zone, so different absolute times.
        assert rows[0].due_at != rows[1].due_at
