"""Scheduler tests: the critical path.

Everything else in this project is in service of "the reminder actually
arrives, once, at roughly the right time, and says so honestly if it is late".
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.adapters.base import DeliveryError, DeliveryRegistry
from remindarr.config import Settings
from remindarr.core.models import Reminder, ReminderState, Subscriber
from remindarr.core.scheduler import Scheduler, idempotency_key
from remindarr.core.types import OutgoingMessage

from .conftest import NOW, FakeAdapter, MutableClock


async def make_subscriber(factory: async_sessionmaker[AsyncSession]) -> int:
    """Get-or-create, since several tests add more than one reminder and the
    (platform, user_id) pair is uniquely constrained."""
    async with factory() as s:
        existing = (
            await s.execute(
                select(Subscriber).where(
                    Subscriber.platform == "telegram", Subscriber.platform_user_id == "u1"
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id
        sub = Subscriber(
            platform="telegram", platform_user_id="u1", timezone="Asia/Tokyo", created_at=NOW
        )
        s.add(sub)
        await s.commit()
        return sub.id


async def add_reminder(
    factory: async_sessionmaker[AsyncSession],
    *,
    due_offset: timedelta,
    text: str = "take the bins out",
    platform: str = "telegram",
    address: str = "c1",
    state: ReminderState = ReminderState.PENDING,
) -> int:
    sub_id = await make_subscriber(factory)
    async with factory() as s:
        r = Reminder(
            subscriber_id=sub_id,
            text=text,
            due_at=NOW + due_offset,
            next_attempt_at=NOW + due_offset,
            state=state,
            delivery_platform=platform,
            delivery_address=address,
            created_at=NOW,
        )
        s.add(r)
        await s.commit()
        return r.id


async def get(factory: async_sessionmaker[AsyncSession], rid: int) -> Reminder:
    async with factory() as s:
        return (await s.execute(select(Reminder).where(Reminder.id == rid))).scalar_one()


def make_scheduler(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
) -> Scheduler:
    return Scheduler(
        settings=settings, session_factory=factory, registry=registry, clock=lambda: NOW
    )


class TestDelivery:
    async def test_delivers_a_due_reminder(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        rid = await add_reminder(session_factory, due_offset=timedelta(minutes=-1))
        assert await make_scheduler(settings, session_factory, registry).tick() == 1

        assert len(adapter.sent) == 1
        assert "take the bins out" in adapter.texts[0]
        assert adapter.sent[0].route.address == "c1"
        assert (await get(session_factory, rid)).state is ReminderState.SENT

    async def test_does_not_deliver_early(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        rid = await add_reminder(session_factory, due_offset=timedelta(hours=1))
        assert await make_scheduler(settings, session_factory, registry).tick() == 0
        assert adapter.sent == []
        assert (await get(session_factory, rid)).state is ReminderState.PENDING

    async def test_delivers_only_once(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        """The single most damaging bug a reminder tool can have is repeating
        itself every tick forever."""
        await add_reminder(session_factory, due_offset=timedelta(minutes=-1))
        scheduler = make_scheduler(settings, session_factory, registry)
        for _ in range(5):
            await scheduler.tick()
        assert len(adapter.sent) == 1

    async def test_cancelled_reminders_are_skipped(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        await add_reminder(
            session_factory, due_offset=timedelta(minutes=-1), state=ReminderState.CANCELLED
        )
        await make_scheduler(settings, session_factory, registry).tick()
        assert adapter.sent == []

    async def test_downtime_catch_up(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        """A reminder that came due while the process was down still fires."""
        await add_reminder(session_factory, due_offset=timedelta(days=-3))
        await make_scheduler(settings, session_factory, registry).tick()
        assert len(adapter.sent) == 1

    async def test_one_failure_does_not_block_others(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        good = FakeAdapter("telegram")
        bad = FakeAdapter("discord")
        bad.fail_with = DeliveryError("nope")
        bad.fail_times = -1  # always
        reg = DeliveryRegistry()
        reg.register(good)
        reg.register(bad)

        await add_reminder(
            session_factory, due_offset=timedelta(minutes=-1), platform="discord", text="bad one"
        )
        ok_id = await add_reminder(
            session_factory, due_offset=timedelta(minutes=-1), platform="telegram", text="good one"
        )

        await make_scheduler(settings, session_factory, reg).tick()
        assert len(good.sent) == 1
        assert (await get(session_factory, ok_id)).state is ReminderState.SENT


class TestFailureHandling:
    async def test_transient_failure_schedules_a_retry(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        adapter.fail_with = DeliveryError("network blip")
        adapter.fail_times = 1
        rid = await add_reminder(session_factory, due_offset=timedelta(minutes=-1))

        await make_scheduler(settings, session_factory, registry).tick()
        row = await get(session_factory, rid)
        assert row.state is ReminderState.PENDING
        assert row.attempts == 1
        assert row.next_attempt_at > NOW
        assert row.last_error is not None

    async def test_retry_backoff_is_exponential(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        adapter.fail_with = DeliveryError("blip")
        adapter.fail_times = -1
        rid = await add_reminder(session_factory, due_offset=timedelta(minutes=-1))
        scheduler = make_scheduler(settings, session_factory, registry)

        await scheduler.tick()
        first = (await get(session_factory, rid)).next_attempt_at
        assert first == NOW + timedelta(seconds=30)

    async def test_due_at_is_never_moved_by_a_retry(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        """due_at records intent; the lateness notice is computed from it."""
        adapter.fail_with = DeliveryError("blip")
        adapter.fail_times = 1
        rid = await add_reminder(session_factory, due_offset=timedelta(minutes=-1))
        original = (await get(session_factory, rid)).due_at

        await make_scheduler(settings, session_factory, registry).tick()
        assert (await get(session_factory, rid)).due_at == original

    async def test_permanent_failure_does_not_retry(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        adapter.fail_with = DeliveryError("blocked", permanent=True)
        adapter.fail_times = -1
        rid = await add_reminder(session_factory, due_offset=timedelta(minutes=-1))

        await make_scheduler(settings, session_factory, registry).tick()
        row = await get(session_factory, rid)
        assert row.state is ReminderState.FAILED
        assert row.attempts == 1

    async def test_gives_up_after_max_attempts(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        adapter.fail_with = DeliveryError("blip")
        adapter.fail_times = -1
        rid = await add_reminder(session_factory, due_offset=timedelta(days=-1))

        # Time has to actually move: each retry pushes next_attempt_at further
        # out, so a frozen clock would make the reminder eligible exactly once.
        clock = MutableClock()
        scheduler = Scheduler(
            settings=settings, session_factory=session_factory, registry=registry, clock=clock
        )
        for _ in range(settings.max_delivery_attempts + 2):
            await scheduler.tick()
            clock.advance(timedelta(hours=1))

        row = await get(session_factory, rid)
        assert row.state is ReminderState.FAILED
        assert row.attempts <= settings.max_delivery_attempts

    async def test_unexpected_adapter_exception_is_retried_not_fatal(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        class Exploding:
            platform = "telegram"

            async def send(self, message: object) -> None:
                raise RuntimeError("adapter bug")

        reg = DeliveryRegistry()
        reg.register(Exploding())
        rid = await add_reminder(session_factory, due_offset=timedelta(minutes=-1))

        await make_scheduler(settings, session_factory, reg).tick()
        row = await get(session_factory, rid)
        assert row.state is ReminderState.PENDING
        assert "RuntimeError" in (row.last_error or "")

    async def test_missing_adapter_is_retried(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
    ) -> None:
        """A reminder addressed to a platform with no adapter loaded is a
        configuration problem, and might resolve on the next restart."""
        rid = await add_reminder(session_factory, due_offset=timedelta(minutes=-1), platform="line")
        await make_scheduler(settings, session_factory, registry).tick()
        row = await get(session_factory, rid)
        assert row.state is ReminderState.PENDING
        assert "no delivery adapter" in (row.last_error or "")


class TestLateness:
    async def test_late_delivery_says_so(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        await add_reminder(session_factory, due_offset=timedelta(hours=-5))
        await make_scheduler(settings, session_factory, registry).tick()
        assert "delivered late" in adapter.texts[0]

    async def test_on_time_delivery_is_quiet(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        await add_reminder(session_factory, due_offset=timedelta(seconds=-5))
        await make_scheduler(settings, session_factory, registry).tick()
        assert "delivered late" not in adapter.texts[0]

    async def test_within_grace_is_quiet(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        await add_reminder(session_factory, due_offset=timedelta(minutes=-30))
        await make_scheduler(settings, session_factory, registry).tick()
        assert "delivered late" not in adapter.texts[0]


class TestIdempotencyKey:
    async def test_retries_of_one_reminder_share_a_key(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
        adapter: FakeAdapter,
    ) -> None:
        """A retry whose first attempt actually landed must look like the same
        message to the platform, or LINE delivers (and bills) it twice."""
        adapter.fail_with = DeliveryError("response lost")
        adapter.fail_times = 1
        rid = await add_reminder(session_factory, due_offset=timedelta(minutes=-1))
        clock = MutableClock()
        scheduler = Scheduler(
            settings=settings, session_factory=session_factory, registry=registry, clock=clock
        )
        sent_keys: list[str | None] = []
        original_send = adapter.send

        async def spy(message: OutgoingMessage) -> None:
            sent_keys.append(message.idempotency_key)
            await original_send(message)

        adapter.send = spy  # type: ignore[method-assign]

        await scheduler.tick()
        clock.advance(timedelta(hours=1))
        await scheduler.tick()

        assert (await get(session_factory, rid)).state is ReminderState.SENT
        assert len(sent_keys) == 2
        assert sent_keys[0] is not None and sent_keys[0] == sent_keys[1]
        assert uuid.UUID(sent_keys[0])  # LINE requires a UUID

    async def test_different_reminders_get_different_keys(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        a = await get(session_factory, await add_reminder(session_factory, due_offset=timedelta()))
        b = await get(session_factory, await add_reminder(session_factory, due_offset=timedelta()))
        assert idempotency_key(a) != idempotency_key(b)

    async def test_reused_id_gets_a_different_key(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """SQLite hands out a deleted row's id again; the key must not follow it."""
        first = await get(
            session_factory, await add_reminder(session_factory, due_offset=timedelta())
        )
        reborn = Reminder(
            id=first.id,
            subscriber_id=first.subscriber_id,
            text="something else",
            due_at=first.due_at + timedelta(days=1),
            next_attempt_at=first.next_attempt_at,
            delivery_platform="line",
            delivery_address="U1",
            created_at=first.created_at + timedelta(seconds=5),
        )
        assert idempotency_key(first) != idempotency_key(reborn)
