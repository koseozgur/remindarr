"""Discord adapter -- capture and delivery.

Uses the gateway websocket, which like Telegram long polling is an **outbound**
connection: no inbound ports, no reverse proxy, no certificate.

One setup gotcha with no way around it: reading message text requires the
**Message Content** privileged intent, which is off by default and must be
enabled in the Discord Developer Portal (Bot -> Privileged Gateway Intents).
Without it the bot connects fine and then sees every message as empty text,
which looks like a bug in this code rather than a missing checkbox. The adapter
checks for this at startup and says so plainly.

The intent is self-serve below 100 servers, so for a personal bot it is a
toggle, not an approval process.
"""

from __future__ import annotations

import asyncio
import contextlib

import discord
import structlog

from remindarr.core.clock import utcnow
from remindarr.core.types import IncomingMessage, OutgoingMessage, Route, Sender

from .base import DeliveryError
from .base import MessageHandler as CoreHandler

log = structlog.get_logger(__name__)

PLATFORM = "discord"

# Discord rejects messages over 2000 characters -- half Telegram's limit, which
# is why truncation lives per-adapter rather than in the core.
MAX_MESSAGE_CHARS = 2000


class DiscordAdapter:
    def __init__(self, token: str) -> None:
        self._token = token
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        self._client = discord.Client(intents=intents)
        self._handler: CoreHandler | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()

        self._client.event(self.on_ready)
        self._client.event(self.on_message)

    @property
    def platform(self) -> str:
        return PLATFORM

    async def on_ready(self) -> None:
        self._ready.set()
        user = self._client.user
        log.info("discord.connected", username=str(user) if user else "?")

    async def start(self, handler: CoreHandler) -> None:
        self._handler = handler
        await self._client.login(self._token)
        self._task = asyncio.create_task(self._client.connect(reconnect=True))

        done, _ = await asyncio.wait(
            [asyncio.create_task(self._ready.wait()), self._task],
            timeout=60,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._task in done:
            # connect() returning this early means it raised.
            await self._task
        if not self._ready.is_set():
            raise RuntimeError("discord gateway did not become ready within 60s")

    async def stop(self) -> None:
        await self._client.close()
        if self._task is not None:
            self._task.cancel()
            # Shutdown is best-effort: a gateway task that errors on the way
            # down must not prevent the rest of the app from stopping.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if self._handler is None:  # pragma: no cover
            return

        if not message.content:
            # Almost always the missing privileged intent rather than an empty
            # message, and worth saying out loud because it is invisible
            # otherwise.
            log.warning(
                "discord.empty_content",
                hint="enable the Message Content intent in the Developer Portal",
                channel_id=message.channel.id,
            )
            return

        incoming = IncomingMessage(
            sender=Sender(
                platform=PLATFORM,
                user_id=str(message.author.id),
                display_name=message.author.display_name,
            ),
            route=Route(PLATFORM, str(message.channel.id)),
            text=message.content,
            received_at=message.created_at or utcnow(),
        )

        try:
            reply = await self._handler(incoming)
        except Exception:
            log.exception("discord.handler_failed", channel_id=message.channel.id)
            await message.channel.send("Something broke on my end. That is a bug, not you.")
            return

        if reply is not None:
            await message.channel.send(_truncate(reply.text))

    async def send(self, message: OutgoingMessage) -> None:
        try:
            channel_id = int(message.route.address)
        except ValueError as exc:
            raise DeliveryError(
                f"invalid discord channel id {message.route.address!r}", permanent=True
            ) from exc

        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except discord.NotFound as exc:
                raise DeliveryError(
                    f"discord channel {channel_id} not found", permanent=True
                ) from exc
            except discord.Forbidden as exc:
                raise DeliveryError(
                    f"no access to discord channel {channel_id}", permanent=True
                ) from exc
            except discord.HTTPException as exc:
                raise DeliveryError(f"discord fetch_channel failed: {exc}") from exc

        if not hasattr(channel, "send"):
            raise DeliveryError(
                f"discord channel {channel_id} cannot receive messages", permanent=True
            )

        try:
            await channel.send(_truncate(message.text))  # type: ignore[union-attr]
        except discord.Forbidden as exc:
            raise DeliveryError(f"discord forbidden: {exc}", permanent=True) from exc
        except discord.HTTPException as exc:
            raise DeliveryError(f"discord http error: {exc}") from exc


def _truncate(text: str) -> str:
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[: MAX_MESSAGE_CHARS - 1] + "…"
