"""Persistence model.

Two tables, deliberately. ``subscribers`` is who, ``reminders`` is what and when.

The reminder's *delivery* route is stored separately from the subscriber who
created it. In v0 they always match, but keeping them apart is what makes
"capture on Telegram, deliver to Discord" a data change rather than a rewrite.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .clock import utcnow


class Base(DeclarativeBase):
    pass


class UtcDateTime(TypeDecorator[datetime]):
    """A datetime column that is timezone-aware UTC on both sides of the wire.

    SQLite has no tz-aware type: it returns **naive** datetimes even from a
    ``DateTime(timezone=True)`` column. Naive and aware datetimes cannot be
    subtracted or compared, so this leaks out as a ``TypeError`` in whichever
    unlucky bit of code touches the value first -- far from the cause.

    Rejecting naive values on write is deliberate. A naive datetime here means
    someone used ``datetime.now()`` instead of ``utcnow()``, and silently
    assuming UTC would turn a nine-hour error into a stored fact.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"refusing to store naive datetime {value!r}; use clock.utcnow()")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ReminderState(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Subscriber(Base):
    """A person, as identified on one platform.

    Note the scope: this is a *platform* identity, not a global one. The same
    human on Telegram and Discord is two rows. Linking them is a real feature
    with real UX (proving you are both accounts) and is deliberately not in v0.
    """

    __tablename__ = "subscribers"
    __table_args__ = (
        UniqueConstraint("platform", "platform_user_id", name="uq_subscriber_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    platform_user_id: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(256), default="")
    timezone: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    reminders: Mapped[list[Reminder]] = relationship(back_populates="subscriber")


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        # The scheduler's hot query: pending work that is due.
        Index("ix_reminder_due", "state", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"))
    text: Mapped[str] = mapped_column(Text)

    due_at: Mapped[datetime] = mapped_column(UtcDateTime())
    """When the user asked to be reminded. Immutable -- it records intent."""

    next_attempt_at: Mapped[datetime] = mapped_column(UtcDateTime())
    """When the scheduler should next try. Moves on retry; ``due_at`` does not."""

    state: Mapped[ReminderState] = mapped_column(
        # A plain String column round-trips as ``str``, not ``ReminderState``,
        # which silently breaks every ``is`` comparison against the enum -- and
        # the scheduler gates delivery on exactly such a check. values_callable
        # stores the lowercase value rather than the member name, so the column
        # stays readable in hand-written SQL.
        SAEnum(
            ReminderState,
            native_enum=False,
            length=16,
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=ReminderState.PENDING,
    )

    delivery_platform: Mapped[str] = mapped_column(String(32))
    delivery_address: Mapped[str] = mapped_column(String(128))
    fallback_address: Mapped[str | None] = mapped_column(String(128), default=None)
    """On ``delivery_platform``: where to deliver if ``delivery_address`` (a
    direct-message address) is refused -- the chat the reminder was set in."""

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    recurrence: Mapped[str | None] = mapped_column(String(256), default=None)
    """Reserved for RRULE strings. v0 writes NULL and never reads it."""

    subscriber: Mapped[Subscriber] = relationship(back_populates="reminders")
