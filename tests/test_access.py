"""Access control: who may use the bot at all.

Deny by default. On LINE every reminder a stranger sets spends the owner's
monthly push quota, and on every platform it stores a stranger's data.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from remindarr.adapters.line import LineAdapter
from remindarr.app import warn_if_nobody_allowed
from remindarr.config import Settings
from remindarr.core.models import Reminder, Subscriber
from remindarr.core.router import Router
from remindarr.core.types import IncomingMessage, Route, Sender

from .conftest import NOW
from .test_line import SECRET, TOKEN, FakeLineApi, payload, sign, text_event


def settings_with(allowed: str | None) -> Settings:
    return Settings(_env_file=None, database_url="sqlite+aiosqlite://", allowed_users=allowed)


def msg(
    text: str, *, platform: str = "line", user_id: str = "Uowner", route: str | None = None
) -> IncomingMessage:
    return IncomingMessage(
        sender=Sender(platform=platform, user_id=user_id, display_name=""),
        route=Route(platform, route or user_id),
        text=text,
        received_at=NOW,
    )


async def count(factory: async_sessionmaker[AsyncSession], model: type[object]) -> int:
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


def router(settings: Settings, factory: async_sessionmaker[AsyncSession]) -> Router:
    return Router(settings=settings, session_factory=factory, clock=lambda: NOW)


class TestConfigParsing:
    def test_unset_and_blank_mean_nobody(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REMINDARR_ALLOWED_USERS", raising=False)
        assert Settings(_env_file=None).allowed_users == frozenset()
        monkeypatch.setenv("REMINDARR_ALLOWED_USERS", "  ")
        assert Settings(_env_file=None).allowed_users == frozenset()

    def test_comma_separated_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Through the real env path: pydantic-settings must not try to read
        the value as JSON."""
        monkeypatch.setenv("REMINDARR_ALLOWED_USERS", " line:U1 ,discord:42,, telegram : 7 ,")
        assert Settings(_env_file=None).allowed_users == {"line:U1", "discord:42", "telegram:7"}

    def test_platform_is_case_insensitive_user_id_is_not(self) -> None:
        settings = settings_with("LINE:Uabc")
        assert settings.allowed_users == {"line:Uabc"}
        assert settings.is_allowed("line", "Uabc")
        assert not settings.is_allowed("line", "uabc")

    @pytest.mark.parametrize(
        "bad",
        [
            "U1234",  # no platform
            "line:",  # no id
            ":U1234",  # no platform name
            "line:U1 U2",  # space instead of comma
            "lnie:U1234",  # typo'd platform
            "whatsapp:123",  # not a platform remindarr has
        ],
    )
    def test_malformed_entries_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="allowed_users entry"):
            settings_with(f"line:Ugood,{bad}")

    def test_user_id_may_contain_colons(self) -> None:
        """Only the first colon separates; ids are opaque strings."""
        assert settings_with("discord:a:b").allowed_users == {"discord:a:b"}


class TestRouter:
    async def test_allowed_user_works_normally(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with("line:Uowner"), session_factory)
        reply = await r.handle(msg("water the plants in 2 hours"))
        assert reply is not None and "water the plants" in reply.text
        assert await count(session_factory, Reminder) == 1

    async def test_denied_user_gets_their_id_and_nothing_is_stored(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with("line:Uowner"), session_factory)
        with capture_logs() as logs:
            reply = await r.handle(msg("water the plants in 2 hours", user_id="Ustranger"))
        assert reply is not None
        assert reply.text == "Not authorised. Ask the owner to add: line:Ustranger"
        assert await count(session_factory, Subscriber) == 0
        assert await count(session_factory, Reminder) == 0
        assert {"event": "access.denied", "user": "line:Ustranger"}.items() <= logs[0].items()
        assert logs[0]["log_level"] == "info"

    @pytest.mark.parametrize(
        "command", ["/help", "/start", "/list", "/tz Europe/Berlin", "/cancel 1"]
    )
    async def test_denied_user_cannot_run_any_command(
        self, session_factory: async_sessionmaker[AsyncSession], command: str
    ) -> None:
        r = router(settings_with("line:Uowner"), session_factory)
        reply = await r.handle(msg(command, user_id="Ustranger"))
        assert reply is not None and reply.text.startswith("Not authorised.")
        assert await count(session_factory, Subscriber) == 0

    async def test_same_id_on_another_platform_is_not_allowed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with("discord:42"), session_factory)
        reply = await r.handle(msg("x in 5m", platform="telegram", user_id="42"))
        assert reply is not None and "telegram:42" in reply.text

    async def test_empty_list_denies_everyone(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with(None), session_factory)
        for platform, uid in [("line", "U1"), ("telegram", "1"), ("discord", "2")]:
            reply = await r.handle(msg("/help", platform=platform, user_id=uid))
            assert reply is not None and f"{platform}:{uid}" in reply.text
        assert await count(session_factory, Subscriber) == 0

    async def test_group_message_is_authorised_by_sender_not_group(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        r = router(settings_with("line:Uowner"), session_factory)
        allowed = await r.handle(msg("bins in 1 hour", user_id="Uowner", route="Cgroup"))
        denied = await r.handle(msg("bins in 1 hour", user_id="Ufriend", route="Cgroup"))
        assert allowed is not None and "bins" in allowed.text
        assert denied is not None and "line:Ufriend" in denied.text
        assert await count(session_factory, Reminder) == 1


class TestStartupWarning:
    def test_warns_when_nobody_is_allowed(self) -> None:
        with capture_logs() as logs:
            warn_if_nobody_allowed(settings_with(None))
        assert [(e["event"], e["log_level"]) for e in logs] == [
            ("access.allow_list_empty", "warning")
        ]

    def test_silent_when_someone_is_allowed(self) -> None:
        with capture_logs() as logs:
            warn_if_nobody_allowed(settings_with("line:Uowner"))
        assert logs == []


Client = TestClient[web.Request, web.Application]


class TestOverLine:
    """The denial must go back on LINE's free reply path, never as a push."""

    @pytest.fixture
    async def api(self) -> AsyncIterator[tuple[FakeLineApi, str]]:
        fake = FakeLineApi()
        server = TestServer(fake.app())
        await server.start_server()
        yield fake, str(server.make_url(""))
        await server.close()

    async def test_stranger_is_answered_by_reply_with_their_id(
        self,
        api: tuple[FakeLineApi, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake, base = api
        adapter = LineAdapter(SECRET, TOKEN, api_base=base)
        r = router(settings_with("line:Uowner"), session_factory)
        client: Client = TestClient(TestServer(adapter.webhook_app(r.handle)))
        await client.start_server()
        try:
            body = json.dumps(
                payload(text_event(source={"type": "user", "userId": "Ustranger"}))
            ).encode()
            resp = await client.post(
                "/line/webhook", data=body, headers={"X-Line-Signature": sign(body)}
            )
            assert resp.status == 200
            await adapter.drain()
        finally:
            await client.close()
            await adapter.stop()

        [(path, _, sent)] = fake.requests
        assert path == "/v2/bot/message/reply"
        assert sent["messages"][0]["text"] == "Not authorised. Ask the owner to add: line:Ustranger"
        assert await count(session_factory, Subscriber) == 0
