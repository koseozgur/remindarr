"""The adapter contract.

Two protocols, not one, because capture and delivery really are separate
problems with separate constraints. Telegram and Discord happen to implement
both. A one-way SMS gateway implements only :class:`DeliveryAdapter`, and that
is a first-class citizen, not a degraded case.

Keeping them apart also leaves room for platforms where the two directions have
different economics -- on LINE, replying to someone who messaged you is free
while pushing to them is quota-metered. An adapter can send a :class:`Reply`
down the cheap path and an :class:`OutgoingMessage` down the metered one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from remindarr.core.types import IncomingMessage, OutgoingMessage, Reply

MessageHandler = Callable[[IncomingMessage], Awaitable[Reply | None]]
"""What the core gives a capture adapter: hand me a message, get a reply."""


class DeliveryError(RuntimeError):
    """A delivery attempt failed.

    ``permanent`` distinguishes "try again later" (rate limit, network blip)
    from "this will never work" (blocked bot, deleted channel). The scheduler
    retries the first and gives up immediately on the second.
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@runtime_checkable
class DeliveryAdapter(Protocol):
    """Can send a message to a route on its platform."""

    @property
    def platform(self) -> str: ...

    async def send(self, message: OutgoingMessage) -> None:
        """Deliver, or raise :class:`DeliveryError`."""
        ...


@runtime_checkable
class CaptureAdapter(Protocol):
    """Can observe incoming messages and feed them to the core."""

    @property
    def platform(self) -> str: ...

    async def start(self, handler: MessageHandler) -> None:
        """Begin listening. Returns once the adapter is connected."""
        ...

    async def stop(self) -> None:
        """Disconnect cleanly."""
        ...


class DeliveryRegistry:
    """Platform name -> delivery adapter.

    The scheduler holds one of these. It is the only thing standing between
    "this reminder says telegram" and an actual API call.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, DeliveryAdapter] = {}

    def register(self, adapter: DeliveryAdapter) -> None:
        self._adapters[adapter.platform] = adapter

    def get(self, platform: str) -> DeliveryAdapter | None:
        return self._adapters.get(platform)

    def platforms(self) -> list[str]:
        return sorted(self._adapters)

    def __len__(self) -> int:
        return len(self._adapters)
