"""``GET /healthz``: is the bot actually doing its job?

"The process is up" isn't the question: a process whose scheduler loop is
failing every tick still answers HTTP. So health is two facts the rest of the
app already records:

* the scheduler finished a tick recently (it only records a tick that
  succeeded, so a dead database shows up here too), and
* the newest backup isn't overdue.

AI is reported too (``"ai": "ok" | "degraded" | "off"``) but never fails the
check: the bot still works without it, so a rejected key is an admin notice
(see ``core.watch``), not a page.

An external uptime check polls this and alerts on anything but 200. The body
is a small JSON object with ages in seconds and nothing about users or
reminders, because the endpoint is public.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from aiohttp import web

from remindarr.ai.reading import AiStatus

from .backup import Backups
from .clock import utcnow
from .scheduler import Scheduler

log = structlog.get_logger(__name__)

STARTUP_GRACE = timedelta(minutes=2)
"""Before the first tick, how long to answer 200 anyway, so a restart isn't an
outage."""


@dataclass(slots=True)
class Health:
    scheduler: Scheduler
    backups: Backups | None
    tick_seconds: float
    started_at: datetime
    clock: Callable[[], datetime] = utcnow
    ai: AiStatus | None = None
    """None when no AI is configured."""

    def check(self) -> tuple[bool, dict[str, Any]]:
        now = self.clock()
        problems: list[str] = []
        body: dict[str, Any] = {}

        tick_limit = timedelta(seconds=max(3 * self.tick_seconds, 60))
        last = self.scheduler.last_tick
        if last is None:
            body["scheduler_age_s"] = None
            if now - self.started_at > STARTUP_GRACE:
                problems.append("scheduler has not ticked since start")
        else:
            age = now - last
            body["scheduler_age_s"] = round(age.total_seconds())
            if age > tick_limit:
                problems.append("scheduler is not ticking")

        if self.backups is not None:
            newest = self.backups.newest()
            # One missed run plus slack, so a slow backup doesn't page anyone.
            backup_limit = 2 * self.backups.interval + timedelta(hours=1)
            if newest is None:
                body["backup_age_s"] = None
                if now - self.started_at > timedelta(hours=1):
                    problems.append("no backup yet")
            else:
                age = now - newest
                body["backup_age_s"] = round(age.total_seconds())
                if age > backup_limit:
                    problems.append("backups are overdue")

        if self.ai is None:
            body["ai"] = "off"
        else:
            body["ai"] = "ok" if self.ai.ok else "degraded"

        body["ok"] = not problems
        if problems:
            body["problems"] = problems
        return not problems, body

    async def handle(self, request: web.Request) -> web.Response:
        ok, body = self.check()
        if not ok:
            log.warning("health.unhealthy", problems=body["problems"])
        return web.json_response(body, status=200 if ok else 503)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/healthz", self.handle)
        return app


class HealthServer:
    """A tiny HTTP listener of its own, so health works whether or not LINE
    (which owns the other listener) is enabled."""

    def __init__(self, health: Health, *, host: str, port: int) -> None:
        self._health = health
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._health.app(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        log.info("health.listening", host=self._host, port=self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
