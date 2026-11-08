"""Domain operations against the store.

Everything here takes an explicit ``session``. Transaction boundaries belong to
the caller -- the router wraps one message in one transaction, the scheduler
wraps one delivery in another -- so these functions never commit on their own.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Reminder, ReminderState, Subscriber
from .types import Route, Sender


async def get_or_create_subscriber(
    session: AsyncSession, sender: Sender, *, default_timezone: str, now: datetime
) -> Subscriber:
    stmt = select(Subscriber).where(
        Subscriber.platform == sender.platform,
        Subscriber.platform_user_id == sender.user_id,
    )
    subscriber = (await session.execute(stmt)).scalar_one_or_none()

    if subscriber is None:
        subscriber = Subscriber(
            platform=sender.platform,
            platform_user_id=sender.user_id,
            display_name=sender.display_name,
            timezone=default_timezone,
            created_at=now,
        )
        session.add(subscriber)
        await session.flush()
    elif sender.display_name and subscriber.display_name != sender.display_name:
        subscriber.display_name = sender.display_name

    return subscriber


async def create_reminder(
    session: AsyncSession,
    *,
    subscriber: Subscriber,
    text: str,
    due_at: datetime,
    route: Route,
    now: datetime,
    fallback: Route | None = None,
) -> Reminder:
    if fallback is not None and fallback.platform != route.platform:
        raise ValueError("a fallback route must be on the delivery platform")
    reminder = Reminder(
        subscriber_id=subscriber.id,
        text=text,
        due_at=due_at,
        next_attempt_at=due_at,
        state=ReminderState.PENDING,
        delivery_platform=route.platform,
        delivery_address=route.address,
        fallback_address=fallback.address if fallback is not None else None,
        created_at=now,
    )
    session.add(reminder)
    await session.flush()
    return reminder


async def list_pending(session: AsyncSession, *, subscriber_id: int) -> list[Reminder]:
    stmt = (
        select(Reminder)
        .where(
            Reminder.subscriber_id == subscriber_id,
            Reminder.state == ReminderState.PENDING,
        )
        .order_by(Reminder.due_at)
    )
    return list((await session.execute(stmt)).scalars())


async def get_reminder(
    session: AsyncSession, *, subscriber_id: int, reminder_id: int
) -> Reminder | None:
    """Scoped to the subscriber on purpose -- an id from another person's list
    must not be addressable."""
    stmt = select(Reminder).where(
        Reminder.id == reminder_id,
        Reminder.subscriber_id == subscriber_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def cancel_reminder(session: AsyncSession, *, reminder: Reminder) -> None:
    reminder.state = ReminderState.CANCELLED


async def set_timezone(session: AsyncSession, *, subscriber: Subscriber, tz: str) -> None:
    ZoneInfo(tz)  # raises if unknown; caller turns that into a user-facing error
    subscriber.timezone = tz


async def claim_due(session: AsyncSession, *, now: datetime, limit: int) -> list[Reminder]:
    """Pending reminders whose time has come.

    Includes anything that came due while the process was down -- the query is
    ``<= now``, not ``== now``, so downtime catch-up needs no special handling.
    """
    stmt = (
        select(Reminder)
        .where(
            Reminder.state == ReminderState.PENDING,
            Reminder.next_attempt_at <= now,
        )
        .order_by(Reminder.next_attempt_at)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


def mark_sent(reminder: Reminder, *, now: datetime) -> None:
    reminder.state = ReminderState.SENT
    reminder.sent_at = now
    reminder.attempts += 1
    reminder.last_error = None


def mark_retry(reminder: Reminder, *, now: datetime, backoff_base: float, error: str) -> None:
    reminder.attempts += 1
    reminder.last_error = error
    delay = backoff_base * (2 ** (reminder.attempts - 1))
    reminder.next_attempt_at = now + timedelta(seconds=delay)


def mark_failed(reminder: Reminder, *, error: str) -> None:
    reminder.state = ReminderState.FAILED
    reminder.attempts += 1
    reminder.last_error = error
