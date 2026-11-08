"""Telegram adapter -- capture and delivery.

Uses **long polling**, not webhooks. Long polling is an outbound connection, so
this runs behind NAT on a home NAS with no port forwarding, no reverse proxy and
no TLS certificate. Webhooks would be marginally more efficient and would matter
at scale; at family scale they only add an attack surface and a certificate to
renew. Telegram supports both officially, so this is a deployment choice rather
than a compromise.
"""

from __future__ import annotations

import structlog
from telegram import Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, MessageHandler, filters

from remindarr.core.clock import utcnow
from remindarr.core.types import USER_PREFIX, IncomingMessage, OutgoingMessage, Route, Sender

from .base import DeliveryError
from .base import MessageHandler as CoreHandler

log = structlog.get_logger(__name__)

PLATFORM = "telegram"

# Telegram rejects messages over 4096 characters outright.
MAX_MESSAGE_CHARS = 4096


class TelegramAdapter:
    def __init__(self, token: str) -> None:
        self._app: Application = Application.builder().token(token).build()  # type: ignore[type-arg]
        self._handler: CoreHandler | None = None
        self._started = False

    @property
    def platform(self) -> str:
        return PLATFORM

    async def start(self, handler: CoreHandler) -> None:
        self._handler = handler
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))
        self._app.add_handler(MessageHandler(filters.COMMAND, self._on_message))

        await self._app.initialize()
        await self._app.start()
        if self._app.updater is None:  # pragma: no cover - only when built without an updater
            raise RuntimeError("telegram application has no updater")
        await self._app.updater.start_polling(drop_pending_updates=True)
        self._started = True

        me = await self._app.bot.get_me()
        log.info("telegram.connected", username=me.username)

    async def stop(self) -> None:
        if not self._started:
            return
        if self._app.updater is not None:
            await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        self._started = False

    async def _on_message(self, update: Update, _context: object) -> None:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None or not message.text:
            return
        if user.is_bot:
            return
        if self._handler is None:  # pragma: no cover - start() sets this
            return

        incoming = IncomingMessage(
            sender=Sender(
                platform=PLATFORM,
                user_id=str(user.id),
                display_name=user.full_name or user.username or str(user.id),
            ),
            route=Route(PLATFORM, str(chat.id)),
            text=message.text,
            received_at=message.date or utcnow(),
            direct=chat.type == "private",
        )

        try:
            reply = await self._handler(incoming)
        except Exception:
            log.exception("telegram.handler_failed", chat_id=chat.id)
            await message.reply_text("Something broke on my end. That is a bug, not you.")
            return

        if reply is not None:
            await message.reply_text(_truncate(reply.text))

    async def send(self, message: OutgoingMessage) -> None:
        try:
            # A private chat's id is the user's id, so "user:<id>" needs no lookup.
            address = message.route.address.removeprefix(USER_PREFIX)
            await self._app.bot.send_message(chat_id=int(address), text=_truncate(message.text))
        except Forbidden as exc:
            # The user blocked the bot or deleted the chat. Retrying cannot help.
            raise DeliveryError(f"telegram forbidden: {exc}", permanent=True) from exc
        except BadRequest as exc:
            raise DeliveryError(f"telegram rejected the message: {exc}", permanent=True) from exc
        except RetryAfter as exc:
            raise DeliveryError(f"telegram rate limit, retry after {exc.retry_after}s") from exc
        except (NetworkError, TimedOut) as exc:
            raise DeliveryError(f"telegram network error: {exc}") from exc
        except ValueError as exc:
            raise DeliveryError(
                f"invalid telegram chat id {message.route.address!r}", permanent=True
            ) from exc


def _truncate(text: str) -> str:
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[: MAX_MESSAGE_CHARS - 1] + "…"
