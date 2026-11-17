"""Pairing (/pair, /devices, /unpair) and the v1 app API."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.config import Settings
from remindarr.core import access
from remindarr.core.api import Api
from remindarr.core.models import Device, Reminder, ReminderState
from remindarr.core.router import Router

from .conftest import TZ, MutableClock
from .test_users import OWNER_DISCORD, make_settings, msg

Client = TestClient[web.Request, web.Application]


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def router(session_factory: async_sessionmaker[AsyncSession], clock: MutableClock) -> Router:
    return Router(settings=make_settings(), session_factory=session_factory, clock=clock)


@pytest.fixture
def limiter() -> access.AttemptLimiter:
    return access.AttemptLimiter(per_identity=3)


@pytest.fixture
async def client(
    router: Router,
    session_factory: async_sessionmaker[AsyncSession],
    clock: MutableClock,
    limiter: access.AttemptLimiter,
) -> AsyncIterator[Client]:
    api = Api(
        router=router,
        settings=router.settings,
        session_factory=session_factory,
        clock=clock,
        limiter=limiter,
    )
    c: Client = TestClient(TestServer(api.app()))
    await c.start_server()
    yield c
    await c.close()


async def say(router: Router, text: str, **kw: str) -> str:
    reply = await router.handle(msg(text, **kw))
    assert reply is not None
    return reply.text


async def pair_code(router: Router, **kw: str) -> str:
    match = re.search(r"([0-9A-Z]{4}-[0-9A-Z]{4})", await say(router, "/pair", **kw))
    assert match
    return match.group(1)


async def paired_token(client: Client, router: Router, **kw: str) -> str:
    code = await pair_code(router, **kw)
    resp = await client.post("/api/v1/pair", json={"code": code, "name": "Test iPhone"})
    assert resp.status == 201, await resp.text()
    token: str = (await resp.json())["token"]
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestPairing:
    async def test_pair_then_me(self, client: Client, router: Router) -> None:
        code = await pair_code(router)
        resp = await client.post(
            "/api/v1/pair", json={"code": code.lower(), "name": " Oz's\nphone "}
        )
        assert resp.status == 201
        body = await resp.json()
        assert body["user"] == {"name": "oz"}
        assert body["device"]["name"] == "Oz's phone"
        assert len(body["token"]) >= 40

        me = await client.get("/api/v1/me", headers=bearer(body["token"]))
        assert me.status == 200
        assert await me.json() == {
            "name": "oz",
            "timezone": "Asia/Tokyo",
            "apps": ["discord", "line"],
            "deliver_on": None,
            "device": {"id": body["device"]["id"], "name": "Oz's phone"},
        }

    async def test_only_hashes_are_stored(
        self, client: Client, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        token = await paired_token(client, router)
        async with session_factory() as s:
            device = (await s.execute(select(Device))).scalar_one()
        assert device.token_hash == access.token_hash(token)
        assert token not in device.token_hash

    async def test_code_works_once(self, client: Client, router: Router) -> None:
        code = await pair_code(router)
        first = await client.post("/api/v1/pair", json={"code": code, "name": "a"})
        again = await client.post("/api/v1/pair", json={"code": code, "name": "b"})
        assert first.status == 201
        assert again.status == 401

    async def test_failures_look_alike(
        self, client: Client, router: Router, clock: MutableClock
    ) -> None:
        expired = await pair_code(router)
        clock.advance(access.PAIR_TTL + timedelta(seconds=1))
        bodies = []
        for code in (expired, "ZZZZ-ZZZZ", "nonsense"):
            resp = await client.post("/api/v1/pair", json={"code": code, "name": "x"})
            assert resp.status == 401
            bodies.append(await resp.json())
        assert bodies[0] == bodies[1] == bodies[2]

    async def test_a_link_code_does_not_pair(self, client: Client, router: Router) -> None:
        match = re.search(r"/link (\S+)", await say(router, "/link"))
        assert match
        resp = await client.post("/api/v1/pair", json={"code": match.group(1), "name": "x"})
        assert resp.status == 401

    async def test_wrong_codes_are_rate_limited(self, client: Client, router: Router) -> None:
        for _ in range(3):
            resp = await client.post("/api/v1/pair", json={"code": "ZZZZ-ZZZZ", "name": "x"})
            assert resp.status == 401
        # Even a right code is refused while blocked.
        code = await pair_code(router)
        resp = await client.post("/api/v1/pair", json={"code": code, "name": "x"})
        assert resp.status == 429

    @pytest.mark.parametrize(
        "body", [b"not json", b"[1, 2]", b'{"code": 5}', b'{"code": "x", "name": 3}']
    )
    async def test_bad_bodies(self, client: Client, body: bytes) -> None:
        resp = await client.post(
            "/api/v1/pair", data=body, headers={"Content-Type": "application/json"}
        )
        assert resp.status == 400

    async def test_big_bodies_are_refused(self, client: Client) -> None:
        resp = await client.post("/api/v1/pair", json={"code": "x", "name": "y" * 10_000})
        assert resp.status == 413

    async def test_strangers_cannot_pair(self, router: Router) -> None:
        reply = await say(router, "/pair", user_id="Ustranger")
        assert reply.startswith("Not authorised")


class TestDenyByDefault:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/me"),
            ("GET", "/api/v1/reminders"),
            ("POST", "/api/v1/reminders"),
            ("DELETE", "/api/v1/reminders/1"),
            ("DELETE", "/api/v1/devices/self"),
        ],
    )
    @pytest.mark.parametrize(
        "headers",
        [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}],
    )
    async def test_needs_a_valid_token(
        self, client: Client, method: str, path: str, headers: dict[str, str]
    ) -> None:
        resp = await client.request(method, path, headers=headers, json={"text": "x"})
        assert resp.status == 401
        assert await resp.json() == {"error": "unauthorized"}

    async def test_unknown_paths_are_404(self, client: Client, router: Router) -> None:
        token = await paired_token(client, router)
        assert (await client.get("/", headers=bearer(token))).status == 404
        assert (await client.get("/api/v2/me", headers=bearer(token))).status == 404


class TestReminders:
    async def test_create_list_cancel(
        self, client: Client, router: Router, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        token = await paired_token(client, router)
        resp = await client.post(
            "/api/v1/reminders", headers=bearer(token), json={"text": "call mum tomorrow at 9"}
        )
        assert resp.status == 201, await resp.text()
        body = await resp.json()
        reminder = body["reminder"]
        assert reminder["text"] == "call mum"
        expected = datetime(2026, 8, 19, 9, 0, tzinfo=TZ).astimezone(UTC)
        assert reminder["due_at"] == expected.isoformat().replace("+00:00", "Z")
        assert reminder["recurrence"] is None
        assert body["confirmation"].startswith("Got it — “call mum”")
        # No default app: Discord before LINE, whose pushes are metered.
        assert reminder["app"] == "discord"

        listed = await (await client.get("/api/v1/reminders", headers=bearer(token))).json()
        assert [r["id"] for r in listed["reminders"]] == [reminder["id"]]

        gone = await client.delete(f"/api/v1/reminders/{reminder['id']}", headers=bearer(token))
        assert gone.status == 204
        again = await client.delete(f"/api/v1/reminders/{reminder['id']}", headers=bearer(token))
        assert again.status == 409
        async with session_factory() as s:
            row = await s.get(Reminder, reminder["id"])
        assert row is not None and row.state is ReminderState.CANCELLED

    async def test_lists_reminders_set_in_any_chat(self, client: Client, router: Router) -> None:
        token = await paired_token(client, router)
        await say(router, "water plants tomorrow at 8")  # on LINE
        await say(router, "bins every monday at 7pm", platform="discord", user_id=OWNER_DISCORD)
        listed = await (await client.get("/api/v1/reminders", headers=bearer(token))).json()
        rows = {r["text"]: r for r in listed["reminders"]}
        assert set(rows) == {"water plants", "bins"}
        assert rows["water plants"]["app"] == "line"
        assert rows["bins"]["recurrence"] is not None

    async def test_default_app_is_respected(self, client: Client, router: Router) -> None:
        await say(router, "/deliver line", platform="discord", user_id=OWNER_DISCORD)
        token = await paired_token(client, router)
        resp = await client.post(
            "/api/v1/reminders", headers=bearer(token), json={"text": "stretch in 10 minutes"}
        )
        assert (await resp.json())["reminder"]["app"] == "line"

    async def test_app_override_works_like_chat(self, client: Client, router: Router) -> None:
        token = await paired_token(client, router)
        resp = await client.post(
            "/api/v1/reminders", headers=bearer(token), json={"text": "stretch in 10 minutes @line"}
        )
        assert resp.status == 201
        assert (await resp.json())["reminder"]["app"] == "line"

    async def test_unreadable_text_is_422(self, client: Client, router: Router) -> None:
        token = await paired_token(client, router)
        resp = await client.post(
            "/api/v1/reminders", headers=bearer(token), json={"text": "something someday"}
        )
        assert resp.status == 422
        assert "error" in await resp.json()

    @pytest.mark.parametrize("text", ["", "   ", None, 7, "/list"])
    async def test_bad_text(self, client: Client, router: Router, text: object) -> None:
        token = await paired_token(client, router)
        resp = await client.post("/api/v1/reminders", headers=bearer(token), json={"text": text})
        assert resp.status in (400, 422)

    async def test_cannot_touch_someone_elses(self, client: Client, router: Router) -> None:
        invite = await say(router, "/invite mum")
        join = re.search(r"/join (\S+)", invite)
        assert join
        await say(router, f"/join {join.group(1)}", platform="discord", user_id="222")
        await say(router, "call oz tomorrow at 9", platform="discord", user_id="222")

        token = await paired_token(client, router)  # oz's
        listed = await (await client.get("/api/v1/reminders", headers=bearer(token))).json()
        assert listed["reminders"] == []
        for rid in ("1", "999", "abc"):
            resp = await client.delete(f"/api/v1/reminders/{rid}", headers=bearer(token))
            assert resp.status == 404


class TestDevices:
    async def test_sign_out(self, client: Client, router: Router) -> None:
        token = await paired_token(client, router)
        assert (await client.delete("/api/v1/devices/self", headers=bearer(token))).status == 204
        assert (await client.get("/api/v1/me", headers=bearer(token))).status == 401

    async def test_devices_and_unpair(self, client: Client, router: Router) -> None:
        first = await paired_token(client, router)
        second = await paired_token(client, router, platform="discord", user_id=OWNER_DISCORD)
        listing = await say(router, "/devices")
        assert listing.count("Test iPhone") == 2

        assert "Which one" in await say(router, "/unpair 3")
        assert "Signed out" in await say(router, "/unpair 1")
        assert (await client.get("/api/v1/me", headers=bearer(first))).status == 401
        assert (await client.get("/api/v1/me", headers=bearer(second))).status == 200
        assert "1. Test iPhone" in await say(router, "/devices")

    async def test_no_devices(self, router: Router) -> None:
        assert "No paired devices" in await say(router, "/devices")

    async def test_remove_signs_out_their_devices(self, client: Client, router: Router) -> None:
        invite = await say(router, "/invite mum")
        join = re.search(r"/join (\S+)", invite)
        assert join
        await say(router, f"/join {join.group(1)}", platform="discord", user_id="222")
        mums = await paired_token(client, router, platform="discord", user_id="222")
        assert (await client.get("/api/v1/me", headers=bearer(mums))).status == 200

        assert "Removed mum" in await say(router, "/remove mum")
        assert (await client.get("/api/v1/me", headers=bearer(mums))).status == 401

    async def test_last_seen_is_recorded(
        self,
        client: Client,
        router: Router,
        clock: MutableClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        token = await paired_token(client, router)
        clock.advance(timedelta(days=3))
        await client.get("/api/v1/me", headers=bearer(token))
        async with session_factory() as s:
            device = (await s.execute(select(Device))).scalar_one()
        assert device.last_seen_at == clock()


def test_api_is_off_by_default() -> None:
    assert Settings(_env_file=None).api_port is None


def test_listener_ports_must_differ() -> None:
    with pytest.raises(ValidationError, match="REMINDARR_API_PORT"):
        Settings(_env_file=None, health_port=8090, api_port=8090)
    with pytest.raises(ValidationError, match="REMINDARR_WEBHOOK_PORT"):
        make_settings(api_port=8080)
