"""Transport-neutral types that flow between adapters and the core.

The whole architecture rests on adapters never leaking platform objects into the
core. A capture adapter turns a Telegram ``Update`` or a Discord ``Message`` into
an :class:`IncomingMessage`; a delivery adapter turns an :class:`OutgoingMessage`
back into whatever its platform wants. The core only ever sees these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Route:
    """An addressable conversation on some platform.

    ``address`` is whatever that platform uses to reach a conversation -- a
    Telegram chat id, a Discord channel id. It is deliberately a string: every
    platform spells these differently and the core has no business caring.
    """

    platform: str
    address: str

    def __str__(self) -> str:
        return f"{self.platform}:{self.address}"


@dataclass(frozen=True, slots=True)
class Sender:
    """Who sent a captured message, as that platform identifies them."""

    platform: str
    user_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A message a capture adapter observed."""

    sender: Sender
    route: Route
    text: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """Something the core wants said on a platform."""

    route: Route
    text: str

    idempotency_key: str | None = None
    """Stable across retries of the same logical message, so a platform that
    supports it can drop a duplicate whose first attempt actually landed (LINE's
    ``X-Line-Retry-Key``). A UUID string. Adapters without such a mechanism
    ignore it."""


@dataclass(frozen=True, slots=True)
class Reply:
    """The core's answer to an incoming message.

    Separate from :class:`OutgoingMessage` because a reply is *reactive* -- it
    goes back where it came from, and on some platforms (notably LINE) replying
    is free while pushing is quota-metered. Keeping the two distinct means an
    adapter can route them down different APIs.
    """

    text: str


@dataclass(frozen=True, slots=True)
class ParsedReminder:
    """The result of understanding a capture message as a reminder request."""

    text: str
    due_at: datetime
    """Always timezone-aware UTC."""

    matched_phrase: str
    """The substring recognised as the time expression, for echoing back."""

    warnings: list[str] = field(default_factory=list)
