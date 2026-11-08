"""Transport-neutral types that flow between adapters and the core.

The whole architecture rests on adapters never leaking platform objects into the
core. A capture adapter turns a Telegram ``Update`` or a Discord ``Message`` into
an :class:`IncomingMessage`; a delivery adapter turns an :class:`OutgoingMessage`
back into whatever its platform wants. The core only ever sees these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

USER_PREFIX = "user:"
"""Route address prefix for "this user's one-to-one chat"; see :class:`Route`."""


@dataclass(frozen=True, slots=True)
class Route:
    """An addressable conversation on some platform.

    ``address`` is whatever that platform uses to reach a conversation -- a
    Telegram chat id, a Discord channel id. It is deliberately a string: every
    platform spells these differently and the core has no business caring.

    One form is shared: ``user:<user_id>`` means "that user's one-to-one chat
    on this platform", for delivering somewhere the user has not written from.
    Each delivery adapter resolves it its own way (on Discord, by opening a
    DM), so the core can ask for it without knowing how.
    """

    platform: str
    address: str

    @classmethod
    def direct_to(cls, platform: str, user_id: str) -> Route:
        return cls(platform, f"{USER_PREFIX}{user_id}")

    @property
    def is_direct_address(self) -> bool:
        return self.address.startswith(USER_PREFIX)

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

    direct: bool = False
    """True in a one-to-one chat with the bot (LINE 1:1, Discord DM, Telegram
    private chat). Defaults to False so an adapter that forgets to say gets
    the conservative group behaviour: delivery stays where it was set."""

    deliver_privately: bool = False
    """The adapter asks for reminders set in this (shared) chat to go to the
    sender's 1:1 chat instead, with this chat as the fallback if that is
    refused. Discord sets it for server channels; a platform where a group
    reminder is for the group leaves it False."""


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """Something the core wants said on a platform."""

    route: Route
    text: str

    fallback: Route | None = None
    """Where to deliver instead if ``route`` -- a ``user:<id>`` address -- is
    refused by the recipient (e.g. Discord DMs closed). The adapter delivers
    there mentioning that user, so they are still notified. Adapters that
    cannot tell a refusal apart ignore it."""

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
