"""The whole loop, in one test each.

Router and scheduler are tested separately elsewhere. These exist because the
thing that actually matters -- "I said a sentence and the right message came
back at the right time" -- crosses both, and a seam between two individually
correct halves is exactly where a working system fails.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.adapters.base import DeliveryRegistry
from remindarr.config import Settings
from remindarr.core.router import Router
from remindarr.core.scheduler import Scheduler

from .conftest import FakeAdapter, MutableClock, message


async def test_capture_to_delivery(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
    adapter: FakeAdapter,
) -> None:
    clock = MutableClock()
    router = Router(settings=settings, session_factory=session_factory, clock=clock)
    scheduler = Scheduler(
        settings=settings, session_factory=session_factory, registry=registry, clock=clock
    )

    reply = await router.handle(message("take the bins out in 30 minutes"))
    assert reply is not None and "take the bins out" in reply.text

    # Nothing yet.
    await scheduler.tick()
    assert adapter.sent == []

    # 29 minutes later: still nothing.
    clock.advance(timedelta(minutes=29))
    await scheduler.tick()
    assert adapter.sent == []

    # 31 minutes: delivered, once, to where it was captured.
    clock.advance(timedelta(minutes=2))
    await scheduler.tick()
    assert len(adapter.sent) == 1
    assert "take the bins out" in adapter.texts[0]
    assert adapter.sent[0].route.platform == "telegram"
    assert adapter.sent[0].route.address == "c1"

    # And never again.
    clock.advance(timedelta(hours=2))
    await scheduler.tick()
    assert len(adapter.sent) == 1


async def test_cancelled_before_firing_never_arrives(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
    adapter: FakeAdapter,
) -> None:
    clock = MutableClock()
    router = Router(settings=settings, session_factory=session_factory, clock=clock)
    scheduler = Scheduler(
        settings=settings, session_factory=session_factory, registry=registry, clock=clock
    )

    await router.handle(message("pointless thing in 10 minutes"))
    await router.handle(message("/cancel 1"))

    clock.advance(timedelta(minutes=20))
    await scheduler.tick()
    assert adapter.sent == []


async def test_timezone_change_is_respected_end_to_end(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
    adapter: FakeAdapter,
) -> None:
    """A London subscriber saying "tomorrow at 9" must fire at 09:00 London,
    which is 08:00 UTC -- not 09:00 in the server's default Tokyo."""
    clock = MutableClock()
    router = Router(settings=settings, session_factory=session_factory, clock=clock)
    scheduler = Scheduler(
        settings=settings, session_factory=session_factory, registry=registry, clock=clock
    )

    await router.handle(message("/tz Europe/London"))
    await router.handle(message("standup tomorrow at 9"))

    # 09:00 JST on the 19th (= 00:00 UTC) is far too early for London.
    clock.advance(timedelta(hours=23))  # NOW is 01:30 UTC -> 00:30 UTC on the 19th
    await scheduler.tick()
    assert adapter.sent == []

    # 08:00 UTC on the 19th is 09:00 in London.
    clock.advance(timedelta(hours=8))
    await scheduler.tick()
    assert len(adapter.sent) == 1


async def test_reminder_survives_a_restart(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
    adapter: FakeAdapter,
) -> None:
    """State lives in the database, not in a scheduler's memory. A brand new
    Scheduler instance must pick up work created by a previous one."""
    clock = MutableClock()
    router = Router(settings=settings, session_factory=session_factory, clock=clock)
    await router.handle(message("survive the restart in 5 minutes"))

    # Original process goes away entirely; a fresh Scheduler takes over.
    clock.advance(timedelta(minutes=10))
    fresh = Scheduler(
        settings=settings, session_factory=session_factory, registry=registry, clock=clock
    )
    await fresh.tick()

    assert len(adapter.sent) == 1
    assert "survive the restart" in adapter.texts[0]
