"""Persistence model.

``users`` is a human, ``subscribers`` is one of their chat identities,
``reminders`` is what and when, ``access_codes`` are the short-lived codes
that bring a new identity or device in (``/join``, ``/link``, ``/pair``), and
``devices`` are paired app installs, each with its own revocable token.

An identity is allowed to use the bot exactly when it is linked to a user.
There is no separate allow-list to drift out of sync with that.

The reminder's *delivery* route is stored separately from the subscriber who
created it, which is what makes "capture on LINE, deliver on Discord" a data
change rather than a rewrite.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
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


class User(Base):
    """One human, with any number of chat identities linked to them."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    """Lowercase slug, unique: what ``@name`` and ``/remove name`` refer to."""

    auto_named: Mapped[bool] = mapped_column(Boolean, default=False)
    """Imported from a bare allow-list entry with no name given. Such a user
    is credited by their chat display name and can't be ``@name``-d."""

    deliver_on: Mapped[str | None] = mapped_column(String(32), default=None)
    """Platform that reminders set in a one-to-one chat go to; ``None`` means
    wherever they were set."""

    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    identities: Mapped[list[Subscriber]] = relationship(back_populates="user")


class Subscriber(Base):
    """One chat identity: a person as one platform knows them.

    Linked to a :class:`User` when allowed. The same human on LINE and Discord
    is two rows pointing at one user; they prove both are theirs with
    ``/link``.
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
    """Kept in step across a user's identities by ``/tz``."""

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    user: Mapped[User | None] = relationship(back_populates="identities")
    reminders: Mapped[list[Reminder]] = relationship(back_populates="subscriber")


class CodeKind(enum.StrEnum):
    INVITE = "invite"
    """Made by an admin for a named newcomer; redeemed with ``/join``."""
    LINK = "link"
    """Made by a user; redeemed with ``/link`` from their other app."""
    PAIR = "pair"
    """Made by a user with ``/pair``; redeemed by the app for a device token."""


class AccessCode(Base):
    """A short-lived, single-use code. Only its hash is stored."""

    __tablename__ = "access_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[CodeKind] = mapped_column(
        SAEnum(
            CodeKind,
            native_enum=False,
            length=16,
            values_callable=lambda enum: [member.value for member in enum],
        )
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    """LINK: whose identity this adds. PAIR: whose device this pairs. INVITE:
    unset."""
    invite_name: Mapped[str | None] = mapped_column(String(64), default=None)
    """INVITE: the name the newcomer will get."""
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), default=None)


class Device(Base):
    """One paired app install. Only a hash of its bearer token is stored.

    Revoking sets ``revoked_at`` rather than deleting the row, so ``/devices``
    history and "why was I signed out?" stay answerable. ``/remove`` deletes a
    user's devices outright, along with the user.
    """

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), default=None)


class PhoneDelivery(Base):
    """Whether a user's own reminders come as phone notifications, chat
    messages, or both (``core.phone``). No row means chat only.

    Its own table rather than a column on ``users`` so existing databases
    gain it without a rebuild: ``create_all`` adds tables, not columns.
    """

    __tablename__ = "phone_delivery"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)


class DeviceSync(Base):
    """A paired app's last report of what it scheduled on the phone.

    The scheduler trusts the phone with a reminder only if this says the app
    saw it, scheduled it, and has notifications on (``phone.covers``).
    """

    __tablename__ = "device_syncs"

    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    synced_at: Mapped[datetime] = mapped_column(UtcDateTime())
    """Server time of the reminder list the app scheduled from."""
    notifications: Mapped[bool] = mapped_column(Boolean)
    complete: Mapped[bool] = mapped_column(Boolean)
    """False when iOS's pending-notification cap made the app drop some."""


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
    """When the user asked to be reminded. Immutable per occurrence -- it records
    intent. A recurring reminder moves it to the next occurrence once one is
    delivered or given up on (see ``service.mark_sent``)."""

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
    recipient: Mapped[str | None] = mapped_column(String(64), default=None)
    """The ``REMINDARR_PEOPLE`` name it was set for, when that is not the
    setter. The setter still owns it; the recipient is told who set it."""
    """On ``delivery_platform``: where to deliver if ``delivery_address`` (a
    direct-message address) is refused -- the chat the reminder was set in."""

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    recurrence: Mapped[str | None] = mapped_column(String(256), default=None)
    """NULL for a one-off; else a DTSTART+RRULE pair (``core.recurrence``)."""

    subscriber: Mapped[Subscriber] = relationship(back_populates="reminders")
