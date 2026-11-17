"""Phone delivery: the chat/both/phone setting, the app's sync reports, and
the guard that only skips chat when the phone provably has the reminder."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.adapters.base import DeliveryRegistry
from remindarr.core import access
from remindarr.core.api import Api
from remindarr.core.models import DeviceSync, PhoneDelivery, Reminder, ReminderState
from remindarr.core.router import Router
from remindarr.core.scheduler import Scheduler

from .conftest import FakeAdapter, MutableClock
from .test_api import bearer, paired_token, say
from .test_users import make_settings

Client = TestClient[web.Request, web.Application]


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def router(session_factory: async_sessionmaker[AsyncSession], clock: MutableClock) -> Router:
    return Router(settings=make_settings(), session_factory=session_factory, clock=clock)


@pytest.fixture
async def client(
    router: Router, session_factory: async_sessionmaker[AsyncSession], clock: MutableClock
) -> AsyncIterator[Client]:
    api = Api(
        router=router,
        settings=router.settings,
        session_factory=session_factory,
        clock=clock,
        limiter=access.AttemptLimiter(),
    )
    c: Client = TestClient(TestServer(api.app()))
    await c.start_server()
    yield c
    await c.close()


@pytest.fixture
def discord() -> FakeAdapter:
    return FakeAdapter("discord")


@pytest.fixture
def scheduler(
    router: Router,
    session_factory: async_sessionmaker[AsyncSession],
    clock: MutableClock,
    discord: FakeAdapter,
) -> Scheduler:
    registry = DeliveryRegistry()
    registry.register(discord)
    registry.register(FakeAdapter("line"))
    return Scheduler(
        settings=router.settings, session_factory=session_factory, registry=registry, clock=clock
    )


class Phone:
    """The app's side of the protocol, as the iOS app does it."""

    def __init__(self, client: Client, token: str) -> None:
        self.client, self.token = client, token

    async def create(self, text: str) -> dict[str, Any]:
        resp = await self.client.post(
            "/api/v1/reminders", headers=bearer(self.token), json={"text": text}
        )
        assert resp.status == 201, await resp.text()
        reminder: dict[str, Any] = (await resp.json())["reminder"]
        return reminder

    async def sync(self, *, notifications: bool = True, complete: bool = True) -> None:
        listed = await (
            await self.client.get("/api/v1/reminders", headers=bearer(self.token))
        ).json()
        resp = await self.client.post(
            "/api/v1/devices/self/sync",
            headers=bearer(self.token),
            json={"as_of": listed["as_of"], "notifications": notifications, "complete": complete},
        )
        assert resp.status == 204, await resp.text()

    async def settings(self, **body: object) -> dict[str, Any]:
        if body:
            resp = await self.client.put("/api/v1/settings", headers=bearer(self.token), json=body)
        else:
            resp = await self.client.get("/api/v1/settings", headers=bearer(self.token))
        assert resp.status == 200, await resp.text()
        settings: dict[str, Any] = await resp.json()
        return settings


@pytest.fixture
async def app(client: Client, router: Router) -> Phone:
    return Phone(client, await paired_token(client, router))


async def state(factory: async_sessionmaker[AsyncSession], rid: int) -> Reminder:
    async with factory() as s:
        return (await s.execute(select(Reminder).where(Reminder.id == rid))).scalar_one()


class TestPhoneOnly:
    async def test_covered_reminder_skips_chat(
        self,
        app: Phone,
        scheduler: Scheduler,
        discord: FakeAdapter,
        clock: MutableClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await app.settings(delivery="phone")
        r = await app.create("stretch in 10 minutes")
        await app.sync()
        clock.advance(timedelta(minutes=11))
        assert await scheduler.tick() == 1
        assert discord.sent == []
        assert (await state(session_factory, r["id"])).state is ReminderState.SENT

    async def test_reminder_set_after_the_last_sync_goes_to_chat(
        self, app: Phone, scheduler: Scheduler, discord: FakeAdapter, clock: MutableClock
    ) -> None:
        await app.settings(delivery="phone")
        await app.sync()
        clock.advance(timedelta(minutes=1))
        await app.create("stretch in 10 minutes")  # the phone hasn't seen this one
        clock.advance(timedelta(minutes=11))
        await scheduler.tick()
        assert discord.texts == ["⏰ stretch"]

    async def test_no_sync_at_all_goes_to_chat(
        self, app: Phone, scheduler: Scheduler, discord: FakeAdapter, clock: MutableClock
    ) -> None:
        await app.settings(delivery="phone")
        await app.create("stretch in 10 minutes")
        clock.advance(timedelta(minutes=11))
        await scheduler.tick()
        assert discord.texts == ["⏰ stretch"]

    @pytest.mark.parametrize("report", [{"notifications": False}, {"complete": False}])
    async def test_notifications_off_or_dropped_goes_to_chat(
        self,
        app: Phone,
        scheduler: Scheduler,
        discord: FakeAdapter,
        clock: MutableClock,
        report: dict[str, bool],
    ) -> None:
        await app.settings(delivery="phone")
        await app.create("stretch in 10 minutes")
        await app.sync(**report)
        clock.advance(timedelta(minutes=11))
        await scheduler.tick()
        assert discord.texts == ["⏰ stretch"]

    async def test_stale_sync_goes_to_chat(
        self, app: Phone, scheduler: Scheduler, discord: FakeAdapter, clock: MutableClock
    ) -> None:
        # The phone went quiet (app deleted, signing lapsed): after a day, chat.
        await app.settings(delivery="phone")
        await app.create("stretch in 25 hours")
        await app.sync()
        clock.advance(timedelta(hours=25, minutes=1))
        await scheduler.tick()
        assert discord.texts == ["⏰ stretch"]

    async def test_signed_out_device_goes_to_chat(
        self,
        app: Phone,
        client: Client,
        scheduler: Scheduler,
        discord: FakeAdapter,
        clock: MutableClock,
    ) -> None:
        await app.settings(delivery="phone")
        await app.create("stretch in 10 minutes")
        await app.sync()
        await client.delete("/api/v1/devices/self", headers=bearer(app.token))
        clock.advance(timedelta(minutes=11))
        await scheduler.tick()
        assert discord.texts == ["⏰ stretch"]

    async def test_hourly_series_beyond_what_was_scheduled_goes_to_chat(
        self,
        app: Phone,
        scheduler: Scheduler,
        discord: FakeAdapter,
        clock: MutableClock,
    ) -> None:
        await app.settings(delivery="phone")
        await app.create("drink water every hour")
        await app.sync()
        sent = []
        for _ in range(12):  # the phone scheduled 10; 9 count as covered
            clock.advance(timedelta(hours=1))
            await scheduler.tick()
            sent.append(len(discord.sent))
        assert sent[:9] == [0] * 9
        assert sent[9:] == [1, 2, 3]

    async def test_reminder_for_someone_else_still_goes_to_chat(
        self,
        app: Phone,
        router: Router,
        scheduler: Scheduler,
        clock: MutableClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        mum = (await say(router, "/invite mum")).split("/join ")[1].split()[0]
        await say(router, f"/join {mum}", user_id="Umum")
        await app.settings(delivery="phone")
        r = await app.create("call grandma in 10 minutes @mum")
        assert r["for"] == "mum"
        await app.sync()
        clock.advance(timedelta(minutes=11))
        await scheduler.tick()
        row = await state(session_factory, r["id"])
        assert row.state is ReminderState.SENT and row.attempts == 1

    @pytest.mark.parametrize("mode", ["both", "chat"])
    async def test_other_modes_always_use_chat(
        self,
        app: Phone,
        scheduler: Scheduler,
        discord: FakeAdapter,
        clock: MutableClock,
        mode: str,
    ) -> None:
        await app.settings(delivery=mode)
        await app.create("stretch in 10 minutes")
        await app.sync()
        clock.advance(timedelta(minutes=11))
        await scheduler.tick()
        assert discord.texts == ["⏰ stretch"]


class TestSettingsApi:
    async def test_defaults_and_update(self, app: Phone) -> None:
        got = await app.settings()
        assert got["delivery"] == "chat"
        assert got["chat_app"] is None
        assert got["apps"] == ["discord", "line"]
        assert got["phone_ready"] is False
        got = await app.settings(delivery="both", chat_app="discord")
        assert (got["delivery"], got["chat_app"]) == ("both", "discord")
        assert got["summary"] == "Reminders arrive on your phone and on Discord."
        got = await app.settings(chat_app=None)
        assert got["chat_app"] is None

    async def test_phone_ready_follows_sync(self, app: Phone) -> None:
        await app.settings(delivery="phone")
        assert "Right now it isn't" in (await app.settings())["summary"]
        await app.sync()
        got = await app.settings()
        assert got["phone_ready"] is True and "Right now" not in got["summary"]

    @pytest.mark.parametrize(
        ("body", "status"),
        [
            ({"delivery": "pigeon"}, 400),
            ({"chat_app": 3}, 400),
            ({"chat_app": "telegram"}, 422),  # not set up here
            ({"chat_app": "fax"}, 422),
        ],
    )
    async def test_rejects_bad_settings(
        self, app: Phone, client: Client, body: dict[str, object], status: int
    ) -> None:
        resp = await client.put("/api/v1/settings", headers=bearer(app.token), json=body)
        assert resp.status == status

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"as_of": "yesterday", "notifications": True, "complete": True},
            {"as_of": "2026-08-18T01:00:00", "notifications": True, "complete": True},
            {"as_of": "2026-08-18T01:00:00Z", "notifications": "yes", "complete": True},
        ],
    )
    async def test_rejects_bad_sync(
        self, app: Phone, client: Client, body: dict[str, object]
    ) -> None:
        resp = await client.post("/api/v1/devices/self/sync", headers=bearer(app.token), json=body)
        assert resp.status == 400

    async def test_future_as_of_is_clamped(
        self,
        app: Phone,
        client: Client,
        clock: MutableClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        future = (clock.now + timedelta(days=3)).isoformat()
        resp = await client.post(
            "/api/v1/devices/self/sync",
            headers=bearer(app.token),
            json={"as_of": future, "notifications": True, "complete": True},
        )
        assert resp.status == 204
        async with session_factory() as s:
            row = (await s.execute(select(DeviceSync))).scalar_one()
        assert row.synced_at == clock.now

    async def test_settings_need_a_token(self, client: Client) -> None:
        assert (await client.get("/api/v1/settings")).status == 401
        assert (await client.put("/api/v1/settings", json={"delivery": "phone"})).status == 401


class TestDeliverCommand:
    async def test_phone_modes_need_a_paired_app(self, router: Router) -> None:
        assert "Pair the phone app first" in await say(router, "/deliver phone")
        assert "/deliver phone" in await say(router, "/deliver")

    async def test_chat_and_app_share_one_setting(self, app: Phone, router: Router) -> None:
        reply = await say(router, "/deliver phone")
        assert "on your phone" in reply and "Right now it isn't" in reply
        assert (await app.settings())["delivery"] == "phone"
        await app.settings(delivery="both", chat_app="discord")
        assert "on your phone and on Discord" in await say(router, "/deliver")
        assert "arrive on Discord" in await say(router, "/deliver chat")
        assert (await app.settings())["delivery"] == "chat"

    async def test_remove_clears_phone_rows(
        self,
        client: Client,
        router: Router,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        mum = (await say(router, "/invite mum")).split("/join ")[1].split()[0]
        await say(router, f"/join {mum}", user_id="Umum")
        mums_app = Phone(client, await paired_token(client, router, user_id="Umum"))
        await mums_app.settings(delivery="both")
        await mums_app.sync()
        assert "Removed mum" in await say(router, "/remove mum")
        async with session_factory() as s:
            assert (await s.execute(select(PhoneDelivery))).scalars().all() == []
            assert (await s.execute(select(DeviceSync))).scalars().all() == []
