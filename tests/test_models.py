"""Round-trip tests for the two column types that are easy to get wrong.

Both of these started as real bugs. They are pinned here because neither shows
up where it is caused -- one disables delivery entirely, the other surfaces as
a TypeError in whatever code touches the value first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.core.models import Reminder, ReminderState, Subscriber

from .conftest import NOW


async def _subscriber(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        sub = Subscriber(
            platform="telegram", platform_user_id="u1", timezone="Asia/Tokyo", created_at=NOW
        )
        s.add(sub)
        await s.commit()
        return sub.id


class TestReminderState:
    async def test_round_trips_as_an_enum_not_a_string(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Regression: a plain String column returns ``str``, so every
        ``state is ReminderState.PENDING`` check was False and the scheduler
        skipped every single reminder."""
        sub_id = await _subscriber(session_factory)
        async with session_factory() as s:
            s.add(
                Reminder(
                    subscriber_id=sub_id,
                    text="x",
                    due_at=NOW,
                    next_attempt_at=NOW,
                    delivery_platform="telegram",
                    delivery_address="c1",
                    created_at=NOW,
                )
            )
            await s.commit()

        async with session_factory() as s:
            row = (await s.execute(select(Reminder))).scalar_one()

        assert isinstance(row.state, ReminderState)
        assert row.state is ReminderState.PENDING


class TestUtcDateTime:
    async def test_round_trips_timezone_aware(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Regression: SQLite returns naive datetimes even from a
        ``DateTime(timezone=True)`` column, which then cannot be subtracted
        from an aware "now"."""
        sub_id = await _subscriber(session_factory)
        async with session_factory() as s:
            s.add(
                Reminder(
                    subscriber_id=sub_id,
                    text="x",
                    due_at=NOW,
                    next_attempt_at=NOW,
                    delivery_platform="telegram",
                    delivery_address="c1",
                    created_at=NOW,
                )
            )
            await s.commit()

        async with session_factory() as s:
            row = (await s.execute(select(Reminder))).scalar_one()

        assert row.due_at.tzinfo is not None
        assert row.due_at == NOW
        # The thing that actually broke: arithmetic against an aware now.
        assert (row.due_at - NOW).total_seconds() == 0

    async def test_non_utc_input_is_normalised(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        sub_id = await _subscriber(session_factory)
        tokyo_time = datetime(2026, 8, 19, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        async with session_factory() as s:
            s.add(
                Reminder(
                    subscriber_id=sub_id,
                    text="x",
                    due_at=tokyo_time,
                    next_attempt_at=tokyo_time,
                    delivery_platform="telegram",
                    delivery_address="c1",
                    created_at=NOW,
                )
            )
            await s.commit()

        async with session_factory() as s:
            row = (await s.execute(select(Reminder))).scalar_one()

        assert row.due_at == datetime(2026, 8, 19, 0, 0, tzinfo=UTC)

    async def test_naive_datetime_is_refused(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Storing a naive datetime means someone used ``datetime.now()``.
        Assuming UTC would bake a nine-hour error into the database."""
        sub_id = await _subscriber(session_factory)
        # SQLAlchemy wraps a bind-param error in StatementError, but the
        # original message is preserved.
        with pytest.raises(StatementError, match="naive datetime"):
            async with session_factory() as s:
                s.add(
                    Reminder(
                        subscriber_id=sub_id,
                        text="x",
                        due_at=datetime(2026, 8, 19, 9, 0),
                        next_attempt_at=NOW,
                        delivery_platform="telegram",
                        delivery_address="c1",
                        created_at=NOW,
                    )
                )
                await s.commit()
