"""Admin notices: a rejected AI credential and a LINE push quota running low."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import anthropic
import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.adapters.base import DeliveryRegistry
from remindarr.ai.claude import ClaudeReader, Federation
from remindarr.ai.reading import AiStatus
from remindarr.app import build_reader
from remindarr.config import Settings
from remindarr.core.directory import bootstrap
from remindarr.core.health import Health
from remindarr.core.models import Reminder
from remindarr.core.scheduler import Scheduler
from remindarr.core.watch import NOTICE_PREFIX, Watch

from .conftest import NOW, FakeAdapter, MutableClock


def admin_settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "database_url": "sqlite+aiosqlite://",
        "line_channel_secret": "s",
        "line_channel_access_token": "t",
        "discord_token": "d",
        "allowed_users": "line:Uowner,discord:111",
        "people": "oz = line:Uowner, discord:111",
        "admins": "oz",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


class FakeProbe:
    def __init__(self) -> None:
        self.status = AiStatus()
        self.probes = 0

    async def probe(self) -> None:
        self.probes += 1


class FakeQuota:
    def __init__(self, limit: int | None, used: int) -> None:
        self.limit, self.used = limit, used
        self.fail = False

    async def quota(self) -> tuple[int | None, int]:
        if self.fail:
            raise RuntimeError("down")
        return self.limit, self.used


async def seeded(factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    async with factory() as s, s.begin():
        await bootstrap(s, settings, now=NOW)


async def notices(factory: async_sessionmaker[AsyncSession]) -> list[Reminder]:
    async with factory() as s:
        return list((await s.execute(select(Reminder))).scalars())


class TestAiNotice:
    async def test_rejected_key_is_announced_once_then_rearmed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = admin_settings()
        await seeded(session_factory, settings)
        ai = FakeProbe()
        watch = Watch(settings=settings, session_factory=session_factory, ai=ai, clock=lambda: NOW)

        assert await watch.check() == []
        ai.status.rejected("the API key was rejected", now=NOW)
        [text] = await watch.check()
        assert text.startswith(NOTICE_PREFIX) and "AI reading is off" in text
        assert await watch.check() == []  # not every half hour

        ai.status.accepted()
        assert await watch.check() == []
        ai.status.rejected("the API key was rejected", now=NOW)
        assert len(await watch.check()) == 1  # a new episode is news again
        assert ai.probes == 5

        rows = await notices(session_factory)
        assert len(rows) == 2
        # The admin has no default app: anywhere but metered LINE first.
        assert {(r.delivery_platform, r.delivery_address) for r in rows} == {
            ("discord", "user:111")
        }
        assert all(r.due_at == NOW for r in rows)

    async def test_notice_follows_the_admins_default_app(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = admin_settings(people="oz = line:Uowner, discord:111 > line")
        await seeded(session_factory, settings)
        ai = FakeProbe()
        ai.status.rejected("x", now=NOW)
        watch = Watch(settings=settings, session_factory=session_factory, ai=ai, clock=lambda: NOW)
        await watch.check()
        [row] = await notices(session_factory)
        assert (row.delivery_platform, row.delivery_address) == ("line", "user:Uowner")

    async def test_the_scheduler_delivers_it(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = admin_settings()
        await seeded(session_factory, settings)
        ai = FakeProbe()
        ai.status.rejected("the API key was rejected", now=NOW)
        await Watch(
            settings=settings, session_factory=session_factory, ai=ai, clock=lambda: NOW
        ).check()
        discord = FakeAdapter("discord")
        registry = DeliveryRegistry()
        registry.register(discord)
        sched = Scheduler(
            settings=settings, session_factory=session_factory, registry=registry, clock=lambda: NOW
        )
        assert await sched.tick() == 1
        [sent] = discord.texts
        assert "AI reading is off" in sent


class TestLineQuota:
    async def test_warns_once_a_month_at_the_threshold(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = admin_settings()
        await seeded(session_factory, settings)
        clock = MutableClock()
        line = FakeQuota(200, 159)
        watch = Watch(settings=settings, session_factory=session_factory, line=line, clock=clock)

        assert await watch.check() == []  # 159 < 160
        line.used = 160
        [text] = await watch.check()
        assert "160 of 200" in text
        line.used = 190
        assert await watch.check() == []  # once a month

        clock.advance(timedelta(days=31))
        assert len(await watch.check()) == 1

    async def test_no_limit_and_failures_are_quiet(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        settings = admin_settings()
        await seeded(session_factory, settings)
        line = FakeQuota(None, 10_000)
        watch = Watch(
            settings=settings, session_factory=session_factory, line=line, clock=lambda: NOW
        )
        assert await watch.check() == []
        line.limit, line.fail = 200, True
        assert await watch.check() == []


class TestHealth:
    def test_ai_is_reported_but_never_fails_the_check(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
    ) -> None:
        clock = MutableClock()
        sched = Scheduler(
            settings=settings, session_factory=session_factory, registry=registry, clock=clock
        )
        sched.last_tick = clock()
        status = AiStatus()
        h = Health(
            scheduler=sched,
            backups=None,
            tick_seconds=15,
            started_at=clock(),
            clock=clock,
            ai=status,
        )
        assert h.check() == (True, {"scheduler_age_s": 0, "ai": "ok", "ok": True})
        status.rejected("the API key was rejected", now=NOW)
        assert h.check() == (True, {"scheduler_age_s": 0, "ai": "degraded", "ok": True})


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("GET", "https://api.anthropic.com/v1/models")
    return cls("no", response=httpx2.Response(status, request=request), body=None)


class TestProbe:
    def reader(self) -> ClaudeReader:
        return ClaudeReader("sk-test", model="m", effort="low", timeout=5)

    async def test_rejected_then_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        r = self.reader()
        answer: list[Exception | None] = [_status_error(anthropic.AuthenticationError, 401)]

        async def fake_list(**kw: Any) -> Any:
            if answer[0] is not None:
                raise answer[0]
            return []

        monkeypatch.setattr(r._client.models, "list", fake_list)
        await r.probe()
        assert r.status.problem == "the API key was rejected"
        answer[0] = None
        await r.probe()
        assert r.status.ok

    async def test_transient_errors_leave_status_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = self.reader()

        async def fake_list(**kw: Any) -> Any:
            raise _status_error(anthropic.InternalServerError, 500)

        monkeypatch.setattr(r._client.models, "list", fake_list)
        await r.probe()
        assert r.status.ok

    async def test_a_rejected_read_marks_the_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from remindarr.ai.reading import AiUnavailable

        r = self.reader()

        async def fake_parse(**kw: Any) -> Any:
            raise _status_error(anthropic.PermissionDeniedError, 403)

        monkeypatch.setattr(r._client.beta.messages, "parse", fake_parse)
        with pytest.raises(AiUnavailable):
            await r.read("x", now_local=NOW)
        assert not r.status.ok


class TestFederationConfig:
    def test_federation_builds_a_keyless_reader(self) -> None:
        reader = build_reader(
            Settings(
                _env_file=None,
                anthropic_federation_rule_id="fdrl_1",
                anthropic_organization_id="org-uuid",
                anthropic_service_account_id="svac_1",
            )
        )
        assert isinstance(reader, ClaudeReader)

    def test_half_a_federation_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="go together"):
            Settings(_env_file=None, anthropic_federation_rule_id="fdrl_1")

    def test_key_and_federation_together_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="Pick one"):
            Settings(
                _env_file=None,
                anthropic_api_key="sk-test",
                anthropic_federation_rule_id="fdrl_1",
                anthropic_organization_id="org-uuid",
            )

    def test_reader_needs_exactly_one_credential(self) -> None:
        with pytest.raises(ValueError):
            ClaudeReader(model="m", effort="low", timeout=5)
        with pytest.raises(ValueError):
            ClaudeReader("sk", federation=Federation("r", "o"), model="m", effort="low", timeout=5)
