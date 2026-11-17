"""Things an admin needs to hear about before a user notices.

Two today, both of which would otherwise fail quietly:

* **The AI provider rejecting our credentials** -- the Claude API key is a
  30-day key, and when it expires AI reading just stops, with the grammar's
  "I didn't understand" as the only symptom.
* **The LINE push quota running low** -- at the monthly cap LINE refuses
  every push until the month turns over.

A notice is a message to every admin, queued as a reminder due now, so the
scheduler delivers it like anything else: on the admin's default app, with
its retries. Each problem is announced once, not every check: once per
episode for the AI (re-armed when the credentials work again) and once per
month for LINE. Both are remembered in memory only, so a restart while a
problem persists announces it once more. That is a deliberate trade for not
adding a table.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from remindarr.ai.reading import AiStatus
from remindarr.config import Settings

from . import service
from .clock import utcnow
from .db import session_scope
from .models import Subscriber, User
from .types import Route

log = structlog.get_logger(__name__)

NOTICE_PREFIX = "Admin notice: "

# Where a notice goes when the admin has no default app: anywhere but LINE
# first, since a LINE push is the one delivery that is metered.
_PREFERENCE = ("discord", "telegram", "line")


class Probed(Protocol):
    status: AiStatus

    async def probe(self) -> None: ...


@runtime_checkable
class QuotaSource(Protocol):
    async def quota(self) -> tuple[int | None, int]: ...


def ai_notice(status: AiStatus) -> str:
    return (
        f"{NOTICE_PREFIX}AI reading is off: {status.problem} (most likely it expired). "
        "Until it's fixed, messages the grammar can't read get the grammar's answer. "
        "Rotation steps: docs/deploy-gcp.md, “Rotating the Claude API key”."
    )


def line_quota_notice(used: int, limit: int) -> str:
    return (
        f"{NOTICE_PREFIX}LINE pushes this month: {used} of {limit}. At {limit}, LINE "
        "refuses every push until next month, so reminders delivered on LINE would fail. "
        "/deliver discord moves your own reminders off LINE."
    )


async def notify_admins(
    session: AsyncSession, settings: Settings, text: str, *, now: datetime
) -> int:
    """Queue ``text`` for every admin; returns how many it was queued for."""
    enabled = settings.enabled_platforms
    admins = (
        await session.execute(
            select(User).where(User.is_admin).options(selectinload(User.identities))
        )
    ).scalars()
    queued = 0
    for admin in admins:
        target = _pick_identity(admin, enabled)
        if target is None:
            log.warning("watch.admin_unreachable", admin=admin.name)
            continue
        await service.create_reminder(
            session,
            subscriber=target,
            text=text,
            due_at=now,
            route=Route.direct_to(target.platform, target.platform_user_id),
            now=now,
        )
        queued += 1
    return queued


def _pick_identity(user: User, enabled: list[str]) -> Subscriber | None:
    usable = {s.platform: s for s in user.identities if s.platform in enabled}
    if user.deliver_on in usable:
        return usable[user.deliver_on]
    return next((usable[p] for p in _PREFERENCE if p in usable), None)


@dataclass(slots=True)
class Watch:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    ai: Probed | None = None
    line: QuotaSource | None = None
    clock: Callable[[], datetime] = utcnow
    _ai_announced: bool = False
    _line_announced_month: str | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self) -> None:
        interval = timedelta(minutes=self.settings.ai_watch_minutes).total_seconds()
        while not self._stop.is_set():
            try:
                await self.check()
            except Exception:
                log.exception("watch.check_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    def stop(self) -> None:
        self._stop.set()

    async def check(self) -> list[str]:
        """One round of checks; returns the notices it queued (for tests)."""
        notices: list[str] = []
        if self.ai is not None:
            await self.ai.probe()
            notice = self._ai_notice()
            if notice is not None:
                notices.append(notice)
        if self.line is not None:
            notice = await self._line_notice()
            if notice is not None:
                notices.append(notice)
        if notices:
            now = self.clock()
            async with session_scope(self.session_factory) as session:
                for text in notices:
                    count = await notify_admins(session, self.settings, text, now=now)
                    log.warning("watch.notice", notice=text, admins=count)
        return notices

    def _ai_notice(self) -> str | None:
        assert self.ai is not None
        status = self.ai.status
        if status.ok:
            self._ai_announced = False  # re-arm: the next failure is news again
            return None
        if self._ai_announced:
            return None
        self._ai_announced = True
        return ai_notice(status)

    async def _line_notice(self) -> str | None:
        assert self.line is not None
        try:
            limit, used = await self.line.quota()
        except Exception as exc:  # a check must not take the loop down
            log.info("watch.line_quota_unavailable", error=f"{type(exc).__name__}: {exc}")
            return None
        if limit is None or limit <= 0:
            return None  # no monthly limit on this plan
        threshold = math.ceil(limit * self.settings.line_quota_warn_percent / 100)
        log.info("line.quota", used=used, limit=limit)
        if used < threshold:
            return None
        # LINE's month is Japan's month for a Japanese account; the default
        # timezone is the closest thing to it that config knows.
        month = f"{self.clock().astimezone(ZoneInfo(self.settings.default_timezone)):%Y-%m}"
        if self._line_announced_month == month:
            return None
        self._line_announced_month = month
        return line_quota_notice(used, limit)
