"""Phone delivery: whether reminders arrive on the phone, in chat, or both.

The app schedules local notifications from the reminder list (there is no
remote push on Apple's free tier). A user picks one of :data:`MODES`:

* ``chat``: chat only, as before the app existed. The app schedules nothing.
* ``both``: the phone and chat. Nothing can be missed; everything comes twice.
* ``phone``: the phone instead of chat, **with a guard**.

The guard: under ``phone`` the scheduler skips chat for a reminder only when
the phone has demonstrably got it covered (:func:`covers`). A paired device
must have reported, within :data:`FRESH`, that it scheduled from a list that
already held this reminder and this occurrence, with notifications on and
nothing dropped by iOS's 64-notification cap. Anything else (app deleted,
free-tier signing lapsed, notifications turned off, a reminder set in chat
since the phone last synced, an hourly series past what was scheduled) falls
back to chat. So "phone only" can produce a chat message when the phone isn't
provably ready, but never silence.

Only the user's own reminders are affected. A reminder someone else set for
them isn't in their app's list, so it always goes to chat.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import service
from .models import Device, DeviceSync, PhoneDelivery, Reminder, Subscriber

MODES = ("chat", "both", "phone")
DEFAULT_MODE = "chat"

FRESH = timedelta(hours=24)
"""How recent a phone's last sync must be for chat to be skipped. Bounds how
long reminders can go to a phone that has quietly stopped working."""

COVERED_OCCURRENCES = 9
"""How far into a series a sync counts as covering. The app schedules the 10
occurrences the API lists (``api.UPCOMING``) starting from ``due_at``; one
fewer leaves room for a ``due_at`` that was already past at the sync."""


async def mode_of(session: AsyncSession, user_id: int) -> str:
    row = await session.get(PhoneDelivery, user_id)
    return row.mode if row is not None else DEFAULT_MODE


async def set_mode(session: AsyncSession, user_id: int, mode: str, now: datetime) -> None:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    row = await session.get(PhoneDelivery, user_id)
    if row is None:
        session.add(PhoneDelivery(user_id=user_id, mode=mode, updated_at=now))
    else:
        row.mode, row.updated_at = mode, now


async def record_sync(
    session: AsyncSession,
    device: Device,
    *,
    as_of: datetime,
    notifications: bool,
    complete: bool,
    now: datetime,
) -> None:
    """Store what a device reports it scheduled. ``as_of`` is the server time
    on the list it scheduled from; a future one is clamped to now."""
    synced_at = min(as_of, now)
    row = await session.get(DeviceSync, device.id)
    if row is None:
        session.add(
            DeviceSync(
                device_id=device.id,
                user_id=device.user_id,
                synced_at=synced_at,
                notifications=notifications,
                complete=complete,
            )
        )
    else:
        row.synced_at, row.notifications, row.complete = synced_at, notifications, complete


def _sync_covers(reminder: Reminder, synced_at: datetime) -> bool:
    """Whether a list fetched at ``synced_at`` led the app to schedule the
    occurrence at ``reminder.due_at``."""
    when = reminder.due_at
    if reminder.created_at > synced_at or when <= synced_at:
        return False  # set after the sync, or already past at it (not scheduled)
    series = service.series_of(reminder)
    if series is None:
        return True
    occurrence: datetime | None = synced_at
    for _ in range(COVERED_OCCURRENCES):
        occurrence = series.next_after(occurrence) if occurrence is not None else None
        if occurrence is None or occurrence > when:
            return False
        if occurrence == when:
            return True
    return False


async def covers(session: AsyncSession, reminder: Reminder, now: datetime) -> bool:
    """True when chat can be skipped for this occurrence: see the module doc."""
    if reminder.recipient is not None:
        return False
    subscriber = await session.get(Subscriber, reminder.subscriber_id)
    if subscriber is None or subscriber.user_id is None:
        return False
    if await mode_of(session, subscriber.user_id) != "phone":
        return False
    stmt = (
        select(DeviceSync)
        .join(Device, Device.id == DeviceSync.device_id)
        .where(
            DeviceSync.user_id == subscriber.user_id,
            Device.revoked_at.is_(None),
            DeviceSync.notifications.is_(True),
            DeviceSync.complete.is_(True),
            DeviceSync.synced_at >= now - FRESH,
        )
    )
    syncs = (await session.execute(stmt)).scalars()
    return any(_sync_covers(reminder, s.synced_at) for s in syncs)


async def phone_ready(session: AsyncSession, user_id: int, now: datetime) -> bool:
    """Whether some device has synced recently with notifications on: what
    ``/deliver`` and the app use to warn that "phone only" is falling back."""
    stmt = (
        select(DeviceSync.device_id)
        .join(Device, Device.id == DeviceSync.device_id)
        .where(
            DeviceSync.user_id == user_id,
            Device.revoked_at.is_(None),
            DeviceSync.notifications.is_(True),
            DeviceSync.synced_at >= now - FRESH,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None
