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

Reminders set in a server channel are delivered by DM to whoever set them --
a reminder is personal, and a DM always notifies. If the DM is refused (DMs
from server members turned off), it falls back to the channel it was set in,
mentioning the setter so they are still notified.

Everything sent is plain text in the sense the router means it: markdown is
escaped (a reminder saying ``deploy *really*`` arrives as written, not in
italics), and **no text can ping anyone**. Every send passes an explicit
``allowed_mentions``: none at all, except the fallback's mention of the setter.
A reminder reading ``@everyone`` or ``<@&role>`` is inert.
"""

from __future__ import annotations

import asyncio
import contextlib
import re

import discord
import structlog

from remindarr.core.clock import utcnow
from remindarr.core.types import USER_PREFIX, IncomingMessage, OutgoingMessage, Route, Sender

from .base import DeliveryError
from .base import MessageHandler as CoreHandler

log = structlog.get_logger(__name__)

PLATFORM = "discord"

# Discord rejects messages over 2000 characters -- half Telegram's limit, which
# is why truncation lives per-adapter rather than in the core.
MAX_MESSAGE_CHARS = 2000

NO_MENTIONS = discord.AllowedMentions.none()


def only_mention(user_id: int) -> discord.AllowedMentions:
    """Allow exactly this user to be pinged -- never everyone, roles or others."""
    return discord.AllowedMentions(
        everyone=False, roles=False, users=[discord.Object(id=user_id)], replied_user=False
    )


class DiscordAdapter:
    def __init__(self, token: str, *, client: discord.Client | None = None) -> None:
        self._token = token
        if client is None:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.dm_messages = True
            client = discord.Client(intents=intents)
        self._client = client
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
            direct=isinstance(message.channel, discord.DMChannel),
            deliver_privately=not isinstance(message.channel, discord.DMChannel),
        )

        try:
            reply = await self._handler(incoming)
        except Exception:
            log.exception("discord.handler_failed", channel_id=message.channel.id)
            await message.channel.send(
                "Something broke on my end. That is a bug, not you.", allowed_mentions=NO_MENTIONS
            )
            return

        if reply is not None:
            await message.channel.send(_render(reply.text), allowed_mentions=NO_MENTIONS)

    async def send(self, message: OutgoingMessage) -> None:
        address = message.route.address
        if not address.startswith(USER_PREFIX):
            await self._send_to_channel(address, _render(message.text), NO_MENTIONS)
            return

        raw_user_id = address.removeprefix(USER_PREFIX)
        dm = await self._dm_channel(raw_user_id)
        try:
            await dm.send(_render(message.text), allowed_mentions=NO_MENTIONS)
            return
        except discord.Forbidden as exc:
            # 50007: the user shares no server with the bot, or has turned off
            # DMs from server members. Retrying cannot change either.
            refused = exc
        except discord.HTTPException as exc:
            raise DeliveryError(f"discord http error: {exc}") from exc

        if message.fallback is None:
            raise DeliveryError(
                f"discord user {raw_user_id} cannot be DMed "
                "(DMs closed, or no server shared with the bot)",
                permanent=True,
            ) from refused

        # The one place a mention is ever sent: the setter, so they still get
        # notified in a channel whose notifications they may have muted.
        user_id = int(raw_user_id)
        log.info("discord.dm_refused_fallback", user_id=user_id)
        try:
            await self._send_to_channel(
                message.fallback.address,
                _truncate(f"<@{user_id}> {_escape_markdown(message.text)}"),
                only_mention(user_id),
            )
        except DeliveryError as exc:
            raise DeliveryError(
                f"discord user {raw_user_id} cannot be DMed, and the fallback channel "
                f"failed too: {exc}",
                permanent=True,
            ) from exc

    async def _send_to_channel(
        self, address: str, text: str, mentions: discord.AllowedMentions
    ) -> None:
        try:
            channel_id = int(address)
        except ValueError as exc:
            raise DeliveryError(f"invalid discord channel id {address!r}", permanent=True) from exc

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
            await channel.send(text, allowed_mentions=mentions)  # type: ignore[union-attr]
        except discord.Forbidden as exc:
            raise DeliveryError(f"discord forbidden: {exc}", permanent=True) from exc
        except discord.HTTPException as exc:
            raise DeliveryError(f"discord http error: {exc}") from exc

    async def _dm_channel(self, raw_user_id: str) -> discord.DMChannel:
        try:
            user_id = int(raw_user_id)
        except ValueError as exc:
            raise DeliveryError(f"invalid discord user id {raw_user_id!r}", permanent=True) from exc

        user = self._client.get_user(user_id)
        if user is None:
            try:
                user = await self._client.fetch_user(user_id)
            except discord.NotFound as exc:
                raise DeliveryError(f"discord user {user_id} not found", permanent=True) from exc
            except discord.HTTPException as exc:
                raise DeliveryError(f"discord fetch_user failed: {exc}") from exc

        if user.dm_channel is not None:
            return user.dm_channel
        try:
            return await user.create_dm()
        except discord.HTTPException as exc:
            raise DeliveryError(f"discord create_dm failed: {exc}") from exc


_URL = re.compile(r"https?://\S+")
_INLINE_MARKDOWN = re.compile(r"([\\*_~`|])")
# Block syntax Discord honours at the start of a line: headings, quotes,
# bullets, and numbered lists ("3. text" renders as a renumbered list item).
# A backslash only escapes punctuation, so "3." becomes "3\." rather than "\3.".
_LINE_MARKER = re.compile(r"^(\s*)(#{1,3}|>{1,3}|-)(?=\s)", re.MULTILINE)
_LINE_NUMBER = re.compile(r"^(\s*\d+)\.(?=\s)", re.MULTILINE)


def _escape_markdown(text: str) -> str:
    """Make Discord show ``text`` exactly as written.

    Not ``discord.utils.escape_markdown``: its link-preserving mode trips a
    DeprecationWarning on Python 3.13, and its other mode escapes inside URLs,
    which breaks links in reminder text.
    """
    out: list[str] = []
    last = 0
    for url in _URL.finditer(text):
        out.append(_INLINE_MARKDOWN.sub(r"\\\1", text[last : url.start()]))
        out.append(url.group())
        last = url.end()
    out.append(_INLINE_MARKDOWN.sub(r"\\\1", text[last:]))
    escaped = _LINE_MARKER.sub(r"\1\\\2", "".join(out))
    return _LINE_NUMBER.sub(r"\1\\.", escaped)


def _render(text: str) -> str:
    # Escape first: escaping adds characters, and the limit applies to what is sent.
    return _truncate(_escape_markdown(text))


def _truncate(text: str) -> str:
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[: MAX_MESSAGE_CHARS - 1] + "…"
