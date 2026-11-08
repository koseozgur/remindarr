"""LINE adapter -- capture (webhook) and delivery (push).

Unlike Telegram and Discord, LINE cannot be long-polled: it delivers messages
by POSTing to a public HTTPS webhook. This is the project's first **inbound**
endpoint. The adapter only listens on ``host:port``; how LINE reaches it (a
reverse proxy, a tunnel, a small VM) is a deployment decision it does not
know about.

LINE does not publish the addresses its webhooks come from, so the
``x-line-signature`` check is the *entire* security boundary of an endpoint
that will be found by scanners within hours. It runs over the raw body bytes
before anything is parsed, and a request that fails it touches nothing.

The two directions have different economics, which is why the core separates
:class:`Reply` from :class:`OutgoingMessage`:

* **Reply** (to a message the user just sent) is free and unmetered, but the
  reply token is single-use and lives about a minute.
* **Push** (a scheduled reminder) counts against the monthly quota -- 200
  messages on the free plan in Japan, a hard cap. A push to a group counts
  once per member.

So a failed reply is logged and dropped, never retried as a push: that would
quietly spend quota on the cheap direction.

Talks to the API with plain ``aiohttp`` rather than ``line-bot-sdk``. The
surface used here is two POST endpoints and one header, and aiohttp is already
in the dependency tree.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

import aiohttp
import structlog
from aiohttp import web

from remindarr.core.clock import utcnow
from remindarr.core.types import IncomingMessage, OutgoingMessage, Route, Sender

from .base import DeliveryError
from .base import MessageHandler as CoreHandler

log = structlog.get_logger(__name__)

PLATFORM = "line"

DEFAULT_API_BASE = "https://api.line.me"

# LINE's limit is 5000 characters counted in UTF-16 code units, so an emoji
# outside the BMP (🍎) costs two. Python's len() counts code points, which would
# let an emoji-heavy message through our check and get it rejected by LINE.
MAX_MESSAGE_UTF16_UNITS = 5000

DEDUPE_CAPACITY = 10_000
"""How many recent ``webhookEventId`` values to remember.

In memory, so a redelivery that arrives after a restart is not recognised.
Redelivery is off by default in the LINE console; if it is turned on, that gap
is the trade for not adding a table for it."""

HTTP_TIMEOUT_SECONDS = 10.0

HANDLER_FAILED_TEXT = "Something broke on my end. That is a bug, not you."
NO_SENDER_TEXT = (
    "LINE didn't tell me who sent that, so I can't set a reminder for it. "
    "Try messaging me directly."
)


class LineAdapter:
    def __init__(
        self,
        channel_secret: str,
        channel_access_token: str,
        *,
        host: str = "0.0.0.0",
        port: int = 8080,
        path: str = "/line/webhook",
        api_base: str = DEFAULT_API_BASE,
    ) -> None:
        self._secret = channel_secret.encode()
        self._token = channel_access_token
        self._host = host
        self._port = port
        self._path = path
        self._api_base = api_base.rstrip("/")

        self._handler: CoreHandler | None = None
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._seen_events: OrderedDict[str, None] = OrderedDict()

    @property
    def platform(self) -> str:
        return PLATFORM

    # --- lifecycle -----------------------------------------------------------

    def webhook_app(self, handler: CoreHandler) -> web.Application:
        """The webhook as an aiohttp app, without binding a port.

        :meth:`start` serves this; tests mount it on a test server instead.
        """
        self._handler = handler
        app = web.Application()
        app.router.add_post(self._path, self._on_webhook)
        return app

    async def start(self, handler: CoreHandler) -> None:
        runner = web.AppRunner(self.webhook_app(handler), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        self._runner = runner
        log.info(
            "line.listening",
            host=self._host,
            port=self._port,
            path=self._path,
            hint="LINE must reach this over public HTTPS; point the console's webhook URL here",
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        await self.drain()
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def drain(self) -> None:
        """Wait for in-flight webhook events to finish processing."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _http(self) -> aiohttp.ClientSession:
        # Created lazily: a ClientSession must be built inside a running loop.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._api_base,
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
                headers={"Authorization": f"Bearer {self._token}"},
            )
        return self._session

    # --- capture -------------------------------------------------------------

    async def _on_webhook(self, request: web.Request) -> web.Response:
        body = await request.read()

        # Before anything else, and over the raw bytes: re-serialised JSON
        # would not hash the same.
        if not self._signature_ok(body, request.headers.get("x-line-signature")):
            log.warning("line.bad_signature", remote=request.remote)
            return web.Response(status=401)

        try:
            payload = json.loads(body)
            events = payload["events"]
            if not isinstance(events, list):
                raise TypeError("events is not a list")
        except (ValueError, KeyError, TypeError):
            log.warning("line.malformed_payload")
            return web.Response(status=400)

        # LINE wants a prompt 200, so events are handled after the response.
        # The console's Verify button sends an empty list and lands here too.
        for event in events:
            if not isinstance(event, dict) or self._is_duplicate(event):
                continue
            task = asyncio.create_task(self._handle_event(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return web.Response(status=200)

    def _signature_ok(self, body: bytes, signature: str | None) -> bool:
        if not signature:
            return False
        expected = base64.b64encode(hmac.new(self._secret, body, hashlib.sha256).digest())
        return hmac.compare_digest(expected, signature.encode("utf-8", "replace"))

    def _is_duplicate(self, event: dict[str, Any]) -> bool:
        event_id = event.get("webhookEventId")
        if not isinstance(event_id, str):
            return False
        if event_id in self._seen_events:
            log.info("line.duplicate_event", webhook_event_id=event_id)
            return True
        self._seen_events[event_id] = None
        if len(self._seen_events) > DEDUPE_CAPACITY:
            self._seen_events.popitem(last=False)
        return False

    async def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            await self._process_event(event)
        except Exception:
            # Nothing above us to catch this: the HTTP response is long gone.
            log.exception("line.event_failed", webhook_event_id=event.get("webhookEventId"))

    async def _process_event(self, event: dict[str, Any]) -> None:
        if event.get("mode") == "standby":
            # Another channel is active for this chat; there is no reply token.
            return
        if event.get("type") != "message":
            return
        message = event.get("message")
        if not isinstance(message, dict) or message.get("type") != "text":
            return
        text = message.get("text")
        if not isinstance(text, str) or not text:
            return
        if self._handler is None:  # pragma: no cover - webhook_app() sets this
            return

        reply_token = event.get("replyToken")
        route = _route_of(event.get("source"))
        if route is None:
            log.warning("line.unroutable_source", source_type=_source_type(event))
            return

        user_id = _user_id_of(event.get("source"))
        if user_id is None:
            # In groups LINE omits userId for some clients. Without it there is
            # no subscriber to own the reminder (or its timezone).
            log.info("line.no_sender", route=str(route))
            await self._reply(reply_token, NO_SENDER_TEXT)
            return

        incoming = IncomingMessage(
            sender=Sender(platform=PLATFORM, user_id=user_id, display_name=""),
            route=route,
            text=text,
            received_at=_timestamp_of(event),
        )

        try:
            reply = await self._handler(incoming)
        except Exception:
            log.exception("line.handler_failed", route=str(route))
            await self._reply(reply_token, HANDLER_FAILED_TEXT)
            return

        if reply is not None:
            await self._reply(reply_token, reply.text)

    async def _reply(self, reply_token: object, text: str) -> None:
        """Answer via the free reply endpoint, or not at all.

        Deliberately never falls back to push: an expired reply token is not
        worth a metered message.
        """
        if not isinstance(reply_token, str) or not reply_token:
            log.warning("line.no_reply_token")
            return
        body = {"replyToken": reply_token, "messages": [_text_message(text)]}
        try:
            async with self._http().post("/v2/bot/message/reply", json=body) as resp:
                if resp.status == 200:
                    return
                log.warning(
                    "line.reply_failed", status=resp.status, error=await _error_message(resp)
                )
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("line.reply_failed", error=f"{type(exc).__name__}: {exc}")

    # --- delivery ------------------------------------------------------------

    async def send(self, message: OutgoingMessage) -> None:
        body = {"to": message.route.address, "messages": [_text_message(message.text)]}
        headers = {}
        if message.idempotency_key is not None:
            # Lets the scheduler's retry of a push whose response was lost come
            # back as 409 instead of a second, separately metered message.
            headers["X-Line-Retry-Key"] = message.idempotency_key

        try:
            async with self._http().post(
                "/v2/bot/message/push", json=body, headers=headers
            ) as resp:
                if resp.status == 200:
                    # Also what LINE returns when the user has blocked the
                    # account: the message is silently dropped, and there is
                    # no way to tell from here.
                    return
                error = await _error_message(resp)
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise DeliveryError(f"line network error: {type(exc).__name__}: {exc}") from exc

        if status == 409 and message.idempotency_key is not None:
            log.info("line.push_already_accepted", route=str(message.route))
            return
        if status == 429:
            if "monthly limit" in error.lower():
                raise DeliveryError(
                    "LINE monthly message limit reached; pushes resume next month",
                    permanent=True,
                )
            raise DeliveryError(f"line rate limited: {error}")
        if status == 401:
            raise DeliveryError(
                "LINE rejected the channel access token (401); check it has not been reissued",
                permanent=True,
            )
        if status >= 500:
            raise DeliveryError(f"line server error {status}: {error}")
        raise DeliveryError(f"line rejected the push ({status}): {error}", permanent=True)


def _route_of(source: object) -> Route | None:
    if not isinstance(source, dict):
        return None
    key = {"user": "userId", "group": "groupId", "room": "roomId"}.get(source.get("type", ""))
    if key is None:
        return None
    address = source.get(key)
    if not isinstance(address, str) or not address:
        return None
    return Route(PLATFORM, address)


def _user_id_of(source: object) -> str | None:
    if not isinstance(source, dict):
        return None
    user_id = source.get("userId")
    return user_id if isinstance(user_id, str) and user_id else None


def _source_type(event: dict[str, Any]) -> object:
    source = event.get("source")
    return source.get("type") if isinstance(source, dict) else None


def _timestamp_of(event: dict[str, Any]) -> datetime:
    ts = event.get("timestamp")
    if isinstance(ts, int) and not isinstance(ts, bool):
        with contextlib.suppress(OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(ts / 1000, tz=UTC)
    return utcnow()


def _text_message(text: str) -> dict[str, str]:
    return {"type": "text", "text": _truncate(text)}


async def _error_message(resp: aiohttp.ClientResponse) -> str:
    """LINE's own error text (``{"message": ...}``), never our request body."""
    try:
        data = await resp.json(content_type=None)
    except (ValueError, aiohttp.ClientError):
        return f"HTTP {resp.status}"
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return str(data["message"])
    return f"HTTP {resp.status}"


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _truncate(text: str) -> str:
    if _utf16_len(text) <= MAX_MESSAGE_UTF16_UNITS:
        return text
    budget = MAX_MESSAGE_UTF16_UNITS - 1  # room for the ellipsis (one unit)
    out = []
    used = 0
    for ch in text:
        cost = 2 if ord(ch) > 0xFFFF else 1
        if used + cost > budget:
            break
        out.append(ch)
        used += cost
    return "".join(out) + "…"
