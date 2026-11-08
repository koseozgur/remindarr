"""LINE adapter tests.

Both sides of the wire are real HTTP over loopback: the webhook is served by
``aiohttp.test_utils.TestServer``, and the LINE API is a small fake aiohttp app
the adapter is pointed at through ``api_base``. Nothing touches the network.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from remindarr.adapters.base import CaptureAdapter, DeliveryAdapter, DeliveryError
from remindarr.adapters.line import MAX_MESSAGE_UTF16_UNITS, NO_SENDER_TEXT, LineAdapter, _truncate
from remindarr.core.types import IncomingMessage, OutgoingMessage, Reply, Route

SECRET = "test-channel-secret"
TOKEN = "test-access-token"
PATH = "/line/webhook"
RETRY_KEY = "123e4567-e89b-12d3-a456-426614174000"

Client = TestClient[web.Request, web.Application]


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def text_event(
    text: str = "call mum tomorrow at 9",
    *,
    source: dict[str, str] | None = None,
    event_id: str = "01HZZZZZZZZZZZZZZZZZZZZZZZ",
    mode: str = "active",
    reply_token: str = "reply-token-1",
) -> dict[str, Any]:
    return {
        "type": "message",
        "mode": mode,
        "timestamp": 1_762_560_000_000,
        "source": source or {"type": "user", "userId": "U1"},
        "webhookEventId": event_id,
        "deliveryContext": {"isRedelivery": False},
        "replyToken": reply_token,
        "message": {"id": "m1", "type": "text", "text": text},
    }


@dataclass
class FakeLineApi:
    """Records calls; ``responses`` queues (status, json body) for pushes."""

    requests: list[tuple[str, dict[str, str], dict[str, Any]]] = field(default_factory=list)
    push_responses: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    reply_status: int = 200

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v2/bot/message/push", self._push)
        app.router.add_post("/v2/bot/message/reply", self._reply)
        return app

    async def _record(self, request: web.Request) -> None:
        self.requests.append((request.path, dict(request.headers), await request.json()))

    async def _push(self, request: web.Request) -> web.Response:
        await self._record(request)
        status, body = self.push_responses.pop(0) if self.push_responses else (200, {})
        return web.json_response(body, status=status)

    async def _reply(self, request: web.Request) -> web.Response:
        await self._record(request)
        if self.reply_status != 200:
            return web.json_response({"message": "Invalid reply token"}, status=self.reply_status)
        return web.json_response({})

    def paths(self) -> list[str]:
        return [path for path, _, _ in self.requests]


class RecordingHandler:
    def __init__(self, reply: str | None = "ok", *, fail: bool = False) -> None:
        self.received: list[IncomingMessage] = []
        self._reply = reply
        self._fail = fail

    async def __call__(self, message: IncomingMessage) -> Reply | None:
        self.received.append(message)
        if self._fail:
            raise RuntimeError("boom")
        return Reply(self._reply) if self._reply is not None else None


@pytest.fixture
def api() -> FakeLineApi:
    return FakeLineApi()


@pytest.fixture
async def line(api: FakeLineApi) -> AsyncIterator[LineAdapter]:
    server = TestServer(api.app())
    await server.start_server()
    adapter = LineAdapter(SECRET, TOKEN, path=PATH, api_base=str(server.make_url("")))
    yield adapter
    await adapter.stop()
    await server.close()


@pytest.fixture
def handler() -> RecordingHandler:
    return RecordingHandler()


@pytest.fixture
async def webhook(line: LineAdapter, handler: RecordingHandler) -> AsyncIterator[Client]:
    client: Client = TestClient(TestServer(line.webhook_app(handler)))
    await client.start_server()
    yield client
    await client.close()


async def post(
    client: Client,
    payload: dict[str, Any],
    *,
    signature: str | None = "auto",
    raw: bytes | None = None,
) -> int:
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "auto":
        headers["X-Line-Signature"] = sign(body)
    elif signature is not None:
        headers["X-Line-Signature"] = signature
    resp = await client.post(PATH, data=body, headers=headers)
    return resp.status


def payload(*events: dict[str, Any]) -> dict[str, Any]:
    return {"destination": "Uxxxxxxxx", "events": list(events)}


def test_implements_both_protocols() -> None:
    adapter = LineAdapter(SECRET, TOKEN)
    assert isinstance(adapter, CaptureAdapter)
    assert isinstance(adapter, DeliveryAdapter)
    assert adapter.platform == "line"


class TestSignature:
    async def test_valid_signature_is_accepted(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
    ) -> None:
        assert await post(webhook, payload(text_event())) == 200
        await line.drain()
        assert len(handler.received) == 1

    async def test_missing_signature_is_rejected(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
    ) -> None:
        assert await post(webhook, payload(text_event()), signature=None) == 401
        await line.drain()
        assert handler.received == []

    async def test_wrong_secret_is_rejected(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
    ) -> None:
        body = json.dumps(payload(text_event())).encode()
        status = await post(webhook, {}, raw=body, signature=sign(body, "not-the-secret"))
        assert status == 401
        await line.drain()
        assert handler.received == []

    async def test_tampered_body_is_rejected(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
    ) -> None:
        original = json.dumps(payload(text_event("call mum"))).encode()
        tampered = original.replace(b"call mum", b"call bob")
        status = await post(webhook, {}, raw=tampered, signature=sign(original))
        assert status == 401
        await line.drain()
        assert handler.received == []

    async def test_signature_is_over_raw_bytes_not_reserialised_json(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
    ) -> None:
        """Whitespace LINE sends must be hashed as sent. Re-serialising the
        parsed JSON would change the bytes and reject every real request."""
        body = json.dumps(payload(text_event()), indent=3, ensure_ascii=False).encode()
        assert await post(webhook, {}, raw=body) == 200
        await line.drain()
        assert len(handler.received) == 1

    async def test_garbage_signature_header_is_rejected_not_crashed(
        self,
        webhook: Client,
    ) -> None:
        assert await post(webhook, payload(), signature="ÿÿ not base64") == 401

    async def test_signed_but_malformed_json_is_400(
        self,
        webhook: Client,
    ) -> None:
        assert await post(webhook, {}, raw=b"{not json") == 400
        assert await post(webhook, {}, raw=b'{"destination": "x"}') == 400


class TestWebhook:
    async def test_verify_payload_gets_200(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
        api: FakeLineApi,
    ) -> None:
        """The console's Verify button sends no events and expects a 200."""
        assert await post(webhook, payload()) == 200
        await line.drain()
        assert handler.received == []
        assert api.requests == []

    @pytest.mark.parametrize(
        ("source", "address"),
        [
            ({"type": "user", "userId": "U1"}, "U1"),
            ({"type": "group", "groupId": "G1", "userId": "U1"}, "G1"),
            ({"type": "room", "roomId": "R1", "userId": "U1"}, "R1"),
        ],
    )
    async def test_routes_to_the_conversation(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
        source: dict[str, str],
        address: str,
    ) -> None:
        await post(webhook, payload(text_event(source=source)))
        await line.drain()
        [msg] = handler.received
        assert msg.route == Route("line", address)
        assert msg.sender.platform == "line"
        assert msg.sender.user_id == "U1"
        assert msg.text == "call mum tomorrow at 9"
        assert msg.received_at.tzinfo is not None

    async def test_group_message_without_user_id_is_not_captured(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
        api: FakeLineApi,
    ) -> None:
        await post(webhook, payload(text_event(source={"type": "group", "groupId": "G1"})))
        await line.drain()
        assert handler.received == []
        # But it says why, on the free path.
        [(path, _, body)] = api.requests
        assert path == "/v2/bot/message/reply"
        assert body["messages"][0]["text"] == NO_SENDER_TEXT

    async def test_standby_events_are_skipped(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
        api: FakeLineApi,
    ) -> None:
        await post(webhook, payload(text_event(mode="standby")))
        await line.drain()
        assert handler.received == []
        assert api.requests == []

    async def test_non_text_messages_and_other_events_are_ignored(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
        api: FakeLineApi,
    ) -> None:
        sticker = text_event(event_id="E1")
        sticker["message"] = {"id": "m2", "type": "sticker", "packageId": "1", "stickerId": "1"}
        follow = {"type": "follow", "mode": "active", "webhookEventId": "E2", "replyToken": "t"}
        status = await post(webhook, payload(sticker, follow, {"type": "unfollow"}, "junk"))  # type: ignore[arg-type]
        assert status == 200
        await line.drain()
        assert handler.received == []
        assert api.requests == []

    async def test_multiple_events_in_one_request(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
    ) -> None:
        await post(
            webhook,
            payload(text_event("one in 5m", event_id="E1"), text_event("two in 5m", event_id="E2")),
        )
        await line.drain()
        assert sorted(m.text for m in handler.received) == ["one in 5m", "two in 5m"]

    async def test_redelivered_event_is_handled_once(
        self,
        webhook: Client,
        line: LineAdapter,
        handler: RecordingHandler,
    ) -> None:
        event = text_event(event_id="01HREDELIVERED0000000000000")
        await post(webhook, payload(event))
        redelivery = {**event, "deliveryContext": {"isRedelivery": True}}
        assert await post(webhook, payload(redelivery)) == 200
        await line.drain()
        assert len(handler.received) == 1


class TestReply:
    async def test_reply_uses_reply_endpoint_never_push(
        self,
        webhook: Client,
        line: LineAdapter,
        api: FakeLineApi,
    ) -> None:
        await post(webhook, payload(text_event(reply_token="tok-abc")))
        await line.drain()
        [(path, headers, body)] = api.requests
        assert path == "/v2/bot/message/reply"
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert body == {"replyToken": "tok-abc", "messages": [{"type": "text", "text": "ok"}]}

    async def test_failed_reply_does_not_fall_back_to_push(
        self,
        webhook: Client,
        line: LineAdapter,
        api: FakeLineApi,
    ) -> None:
        """An expired reply token is not worth a metered push."""
        api.reply_status = 400
        assert await post(webhook, payload(text_event())) == 200
        await line.drain()
        assert api.paths() == ["/v2/bot/message/reply"]

    async def test_no_reply_when_handler_returns_none(
        self, line: LineAdapter, api: FakeLineApi
    ) -> None:
        client: Client = TestClient(TestServer(line.webhook_app(RecordingHandler(None))))
        await client.start_server()
        try:
            await post(client, payload(text_event()))
            await line.drain()
        finally:
            await client.close()
        assert api.requests == []

    async def test_handler_crash_gets_an_apology_by_reply(
        self, line: LineAdapter, api: FakeLineApi
    ) -> None:
        client: Client = TestClient(TestServer(line.webhook_app(RecordingHandler(fail=True))))
        await client.start_server()
        try:
            assert await post(client, payload(text_event())) == 200
            await line.drain()
        finally:
            await client.close()
        [(path, _, body)] = api.requests
        assert path == "/v2/bot/message/reply"
        assert "bug" in body["messages"][0]["text"]


def outgoing(key: str | None = RETRY_KEY, text: str = "⏰ take the bins out") -> OutgoingMessage:
    return OutgoingMessage(route=Route("line", "U1"), text=text, idempotency_key=key)


class TestPush:
    async def test_push_success(self, line: LineAdapter, api: FakeLineApi) -> None:
        await line.send(outgoing())
        [(path, headers, body)] = api.requests
        assert path == "/v2/bot/message/push"
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        assert headers["X-Line-Retry-Key"] == RETRY_KEY
        assert body == {"to": "U1", "messages": [{"type": "text", "text": "⏰ take the bins out"}]}

    async def test_no_retry_key_header_without_idempotency_key(
        self, line: LineAdapter, api: FakeLineApi
    ) -> None:
        await line.send(outgoing(key=None))
        [(_, headers, _)] = api.requests
        assert "X-Line-Retry-Key" not in headers

    async def test_409_with_retry_key_means_already_delivered(
        self, line: LineAdapter, api: FakeLineApi
    ) -> None:
        api.push_responses.append((409, {"message": "The retry key is already accepted"}))
        await line.send(outgoing())  # does not raise

    async def test_409_without_retry_key_is_an_error(
        self, line: LineAdapter, api: FakeLineApi
    ) -> None:
        api.push_responses.append((409, {"message": "Conflict"}))
        with pytest.raises(DeliveryError):
            await line.send(outgoing(key=None))

    async def test_monthly_limit_is_permanent(self, line: LineAdapter, api: FakeLineApi) -> None:
        api.push_responses.append((429, {"message": "You have reached your monthly limit."}))
        with pytest.raises(DeliveryError, match="monthly message limit") as exc:
            await line.send(outgoing())
        assert exc.value.permanent

    async def test_rate_limit_429_is_transient(self, line: LineAdapter, api: FakeLineApi) -> None:
        api.push_responses.append(
            (429, {"message": "The API rate limit has been exceeded. Try again later."})
        )
        with pytest.raises(DeliveryError) as exc:
            await line.send(outgoing())
        assert not exc.value.permanent

    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_5xx_is_transient(self, line: LineAdapter, api: FakeLineApi, status: int) -> None:
        api.push_responses.append((status, {"message": "Internal Server Error"}))
        with pytest.raises(DeliveryError) as exc:
            await line.send(outgoing())
        assert not exc.value.permanent

    async def test_401_is_permanent(self, line: LineAdapter, api: FakeLineApi) -> None:
        api.push_responses.append((401, {"message": "Authentication failed."}))
        with pytest.raises(DeliveryError, match="access token") as exc:
            await line.send(outgoing())
        assert exc.value.permanent

    async def test_invalid_recipient_is_permanent(
        self, line: LineAdapter, api: FakeLineApi
    ) -> None:
        api.push_responses.append(
            (400, {"message": "Failed to send messages", "details": [{"property": "to"}]})
        )
        with pytest.raises(DeliveryError, match="Failed to send messages") as exc:
            await line.send(outgoing())
        assert exc.value.permanent

    async def test_network_error_is_transient(self) -> None:
        # Port 1 on loopback: nothing listens, so the connection is refused.
        adapter = LineAdapter(SECRET, TOKEN, api_base="http://127.0.0.1:1")
        try:
            with pytest.raises(DeliveryError, match="network") as exc:
                await adapter.send(outgoing())
            assert not exc.value.permanent
        finally:
            await adapter.stop()

    async def test_long_text_is_truncated_before_sending(
        self, line: LineAdapter, api: FakeLineApi
    ) -> None:
        await line.send(outgoing(text="x" * 6000))
        [(_, _, body)] = api.requests
        assert len(body["messages"][0]["text"]) == MAX_MESSAGE_UTF16_UNITS


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class TestTruncate:
    def test_short_text_untouched(self) -> None:
        assert _truncate("hello 🍎") == "hello 🍎"

    def test_exactly_at_limit_untouched(self) -> None:
        text = "🍎" * (MAX_MESSAGE_UTF16_UNITS // 2)
        assert _truncate(text) == text

    def test_counts_utf16_units_not_code_points(self) -> None:
        """3000 apples are 3000 code points but 6000 UTF-16 units -- over the
        limit, although len() says otherwise."""
        text = "🍎" * 3000
        out = _truncate(text)
        assert utf16_units(out) <= MAX_MESSAGE_UTF16_UNITS
        assert out.endswith("…")

    def test_never_splits_a_surrogate_pair(self) -> None:
        text = "a" + "🍎" * 3000  # odd offset, so a naive cut lands mid-pair
        out = _truncate(text)
        assert utf16_units(out) <= MAX_MESSAGE_UTF16_UNITS
        out.encode("utf-8")  # a lone surrogate would raise here
        assert out[:-1].endswith("🍎")


async def test_start_serves_the_webhook_on_the_configured_port() -> None:
    """The path production takes: start() binds a real socket."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    adapter = LineAdapter(SECRET, TOKEN, host="127.0.0.1", port=port, path=PATH)
    await adapter.start(RecordingHandler())
    try:
        body = json.dumps(payload()).encode()
        async with aiohttp.ClientSession() as http:
            ok = await http.post(
                f"http://127.0.0.1:{port}{PATH}",
                data=body,
                headers={"X-Line-Signature": sign(body)},
            )
            unsigned = await http.post(f"http://127.0.0.1:{port}{PATH}", data=body)
            assert (ok.status, unsigned.status) == (200, 401)
    finally:
        await adapter.stop()
