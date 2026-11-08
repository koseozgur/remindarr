"""The tick loop that actually fires reminders.

Deliberately a poll over the reminders table rather than an in-memory timer
wheel or a job-store library. The table is the only source of truth, which buys
three things for free:

* **Restart safety.** State lives in one place; there is no job store to fall
  out of sync with the reminders it mirrors.
* **Downtime catch-up.** The query is "due at or before now", so a reminder
  that came due while the container was rebuilding fires on the next tick.
* **Debuggability.** "Why did this not fire?" is answerable with one SQL query
  against a table a human can read.

The cost is up to ``tick_seconds`` of latency, which for reminders is noise.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.adapters.base import DeliveryError, DeliveryRegistry
from remindarr.config import Settings

from . import service
from .clock import utcnow
from .db import session_scope
from .delivery import setter_name
from .humanize import format_delta
from .models import Reminder, ReminderState, Subscriber
from .types import OutgoingMessage, Route

log = structlog.get_logger(__name__)

IDEMPOTENCY_NAMESPACE = uuid.UUID("6f0c2b5e-4a8e-5d8c-9a51-2f6c3e1d7b40")
"""Fixed namespace for :func:`idempotency_key`. Changing it changes every key."""

BATCH_LIMIT = 100
"""Ceiling on one tick's work, so a large backlog after long downtime is drained
across several ticks instead of blocking the loop in one burst."""


@dataclass(slots=True)
class Scheduler:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    registry: DeliveryRegistry
    clock: Callable[[], datetime] = utcnow
    """Injected so tests can pin "now" without patching the module."""
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self) -> None:
        log.info("scheduler.start", tick_seconds=self.settings.tick_seconds)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                # A tick must never kill the loop -- a transient DB error would
                # otherwise silently stop every future reminder.
                log.exception("scheduler.tick_failed")
            # Sleep until the next tick, but wake immediately on shutdown.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.tick_seconds)
        log.info("scheduler.stopped")

    def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> int:
        """Deliver everything currently due. Returns how many were attempted."""
        now = self.clock()
        async with session_scope(self.session_factory) as session:
            due = await service.claim_due(session, now=now, limit=BATCH_LIMIT)
            ids = [r.id for r in due]

        if not ids:
            return 0

        log.info("scheduler.due", count=len(ids))
        for reminder_id in ids:
            # One transaction per reminder: a failure on one must not roll back
            # the successful delivery of another.
            await self._deliver_one(reminder_id)
        return len(ids)

    async def _deliver_one(self, reminder_id: int) -> None:
        now = self.clock()
        async with session_scope(self.session_factory) as session:
            reminder = await session.get(Reminder, reminder_id)
            if reminder is None or reminder.state is not ReminderState.PENDING:
                return  # cancelled between claim and delivery

            adapter = self.registry.get(reminder.delivery_platform)
            if adapter is None:
                self._retry(
                    reminder, now, f"no delivery adapter for {reminder.delivery_platform!r}"
                )
                return

            message = OutgoingMessage(
                route=Route(reminder.delivery_platform, reminder.delivery_address),
                text=await self._render(session, reminder, now),
                fallback=(
                    Route(reminder.delivery_platform, reminder.fallback_address)
                    if reminder.fallback_address is not None
                    else None
                ),
                idempotency_key=idempotency_key(reminder),
            )
            try:
                await adapter.send(message)
            except DeliveryError as exc:
                if exc.permanent:
                    service.mark_failed(reminder, error=str(exc))
                    log.warning(
                        "delivery.permanent_failure", reminder_id=reminder_id, error=str(exc)
                    )
                else:
                    self._retry(reminder, now, str(exc))
                return
            except Exception as exc:  # adapter bug or unexpected client error
                self._retry(reminder, now, f"{type(exc).__name__}: {exc}")
                log.exception("delivery.unexpected_error", reminder_id=reminder_id)
                return

            service.mark_sent(reminder, now=now)
            log.info("delivery.sent", reminder_id=reminder_id, platform=adapter.platform)

    def _retry(self, reminder: Reminder, now: datetime, error: str) -> None:
        if reminder.attempts + 1 >= self.settings.max_delivery_attempts:
            service.mark_failed(reminder, error=error)
            log.warning("delivery.gave_up", reminder_id=reminder.id, error=error)
            return
        service.mark_retry(
            reminder,
            now=now,
            backoff_base=self.settings.retry_backoff_seconds,
            error=error,
        )
        log.info(
            "delivery.will_retry",
            reminder_id=reminder.id,
            attempts=reminder.attempts,
            next_attempt_at=reminder.next_attempt_at.isoformat(),
            error=error,
        )

    async def _render(self, session: AsyncSession, reminder: Reminder, now: datetime) -> str:
        """Reminder text, who set it if it is for someone else, and an honest
        note when it is arriving late.

        Silently delivering a two-hour-old reminder as if it were on time is
        worse than being late -- the user cannot tell whether they misread the
        clock or the system failed them.
        """
        lateness = now - reminder.due_at
        grace = self.settings.late_delivery_grace_minutes * 60
        body = f"⏰ {reminder.text}"
        if reminder.recipient is not None:
            setter = await session.get(Subscriber, reminder.subscriber_id)
            if setter is not None:
                by = setter_name(
                    self.settings, setter.platform, setter.platform_user_id, setter.display_name
                )
                body += f" (from {by})"
        if lateness.total_seconds() > grace:
            body += f"\n(this was due {format_delta(-lateness)} — delivered late)"
        return body


def idempotency_key(reminder: Reminder) -> str:
    """The same UUID for every delivery attempt of one reminder.

    Derived from the row rather than generated per attempt, so a retry after a
    lost response carries the key of the attempt that may already have landed.
    ``created_at`` and ``due_at`` are mixed in because SQLite reuses the
    highest id after a delete -- and a clean rebuild of the database restarts
    ids from 1 -- so an id alone could collide with a different reminder's key
    inside a platform's dedupe window and get a real reminder dropped.
    """
    name = f"reminder:{reminder.id}:{reminder.created_at.isoformat()}:{reminder.due_at.isoformat()}"
    return str(uuid.uuid5(IDEMPOTENCY_NAMESPACE, name))
