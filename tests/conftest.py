from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from remindarr.adapters.base import DeliveryError, DeliveryRegistry
from remindarr.config import Settings
from remindarr.core.models import Base
from remindarr.core.types import IncomingMessage, OutgoingMessage, Route, Sender

TZ = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 8, 18, 10, 30, tzinfo=TZ).astimezone(UTC)  # Tuesday


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory SQLite shared across sessions via StaticPool.

    Without StaticPool every connection would get its own private ``:memory:``
    database and the scheduler would never see what the router wrote.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        default_timezone="Asia/Tokyo",
        tick_seconds=0.01,
        max_delivery_attempts=3,
        retry_backoff_seconds=30.0,
        late_delivery_grace_minutes=60,
    )


class FakeAdapter:
    """A delivery adapter that records instead of sending.

    ``fail_with`` makes the next N sends raise, which is how the retry and
    give-up paths get exercised without touching a network.
    """

    def __init__(self, platform: str = "telegram") -> None:
        self._platform = platform
        self.sent: list[OutgoingMessage] = []
        self.fail_with: DeliveryError | None = None
        self.fail_times = 0

    @property
    def platform(self) -> str:
        return self._platform

    async def send(self, message: OutgoingMessage) -> None:
        if self.fail_with is not None and self.fail_times != 0:
            if self.fail_times > 0:
                self.fail_times -= 1
            raise self.fail_with
        self.sent.append(message)

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]


class MutableClock:
    """A clock the test can advance.

    Needed because retry backoff pushes ``next_attempt_at`` into the future: a
    frozen clock makes a reminder eligible exactly once, so the give-up path is
    unreachable without time actually moving.
    """

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def registry(adapter: FakeAdapter) -> DeliveryRegistry:
    reg = DeliveryRegistry()
    reg.register(adapter)
    return reg


def message(text: str, *, user_id: str = "u1", chat: str = "c1") -> IncomingMessage:
    return IncomingMessage(
        sender=Sender(platform="telegram", user_id=user_id, display_name=f"user {user_id}"),
        route=Route("telegram", chat),
        text=text,
        received_at=NOW,
    )
