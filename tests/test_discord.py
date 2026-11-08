"""Discord adapter tests, against a fake client -- no gateway, no network."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from remindarr.adapters.base import DeliveryError
from remindarr.adapters.discord import DiscordAdapter
from remindarr.core.types import IncomingMessage, OutgoingMessage, Reply, Route
from remindarr.logging import configure

USER_ID = 123456789012345678


def http_error(cls: type[discord.HTTPException], status: int, code: int) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="nope")
    return cls(response, {"code": code, "message": "nope"})


class FakeChannel:
    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.fail = fail

    async def send(self, text: str, *, allowed_mentions: Any = None) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append((text, allowed_mentions))


class FakeUser:
    def __init__(self, dm: FakeChannel, *, has_dm: bool = False) -> None:
        self.dm_channel = dm if has_dm else None
        self._dm = dm
        self.created = 0

    async def create_dm(self) -> FakeChannel:
        self.created += 1
        return self._dm


class FakeClient:
    def __init__(self) -> None:
        self.users: dict[int, FakeUser] = {}
        self.channels: dict[int, FakeChannel] = {}
        self.fetched: list[int] = []
        self.fetch_error: Exception | None = None

    def event(self, coro: Any) -> Any:
        return coro

    def get_user(self, user_id: int) -> FakeUser | None:
        return None  # not cached: force the fetch path

    async def fetch_user(self, user_id: int) -> FakeUser:
        self.fetched.append(user_id)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.users[user_id]

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)


def adapter_with(client: FakeClient) -> DiscordAdapter:
    return DiscordAdapter("token", client=client)  # type: ignore[arg-type]


def dm_message(text: str = "⏰ take the bins out") -> OutgoingMessage:
    return OutgoingMessage(route=Route.direct_to("discord", str(USER_ID)), text=text)


class TestDirectMessages:
    async def test_user_address_opens_a_dm(self) -> None:
        client = FakeClient()
        dm = FakeChannel()
        client.users[USER_ID] = FakeUser(dm)
        await adapter_with(client).send(dm_message())
        assert client.fetched == [USER_ID]
        [(text, mentions)] = dm.sent
        assert text == "⏰ take the bins out"
        assert mentions.to_dict() == discord.AllowedMentions.none().to_dict()

    async def test_existing_dm_channel_is_reused(self) -> None:
        client = FakeClient()
        dm = FakeChannel()
        user = FakeUser(dm, has_dm=True)
        client.users[USER_ID] = user
        await adapter_with(client).send(dm_message())
        assert user.created == 0 and len(dm.sent) == 1

    async def test_dms_closed_is_permanent(self) -> None:
        client = FakeClient()
        client.users[USER_ID] = FakeUser(
            FakeChannel(fail=http_error(discord.Forbidden, 403, 50007))
        )
        with pytest.raises(DeliveryError, match="cannot be DMed") as exc:
            await adapter_with(client).send(dm_message())
        assert exc.value.permanent

    async def test_unknown_user_is_permanent(self) -> None:
        client = FakeClient()
        client.fetch_error = http_error(discord.NotFound, 404, 10013)
        with pytest.raises(DeliveryError, match="not found") as exc:
            await adapter_with(client).send(dm_message())
        assert exc.value.permanent

    async def test_server_error_is_transient(self) -> None:
        client = FakeClient()
        client.fetch_error = http_error(discord.DiscordServerError, 503, 0)
        with pytest.raises(DeliveryError) as exc:
            await adapter_with(client).send(dm_message())
        assert not exc.value.permanent

    async def test_malformed_user_id_is_permanent(self) -> None:
        msg = OutgoingMessage(route=Route("discord", "user:not-a-number"), text="x")
        with pytest.raises(DeliveryError, match="invalid discord user id") as exc:
            await adapter_with(FakeClient()).send(msg)
        assert exc.value.permanent


NOTHING_PINGS = discord.AllowedMentions.none().to_dict()
PINGY = "standup @everyone @here <@&555> <@999> <@!999>"


def with_fallback(text: str = PINGY) -> OutgoingMessage:
    return OutgoingMessage(
        route=Route.direct_to("discord", str(USER_ID)),
        text=text,
        fallback=Route("discord", "42"),
    )


class TestFallback:
    async def test_dm_is_used_when_it_works_and_nothing_pings(self) -> None:
        client = FakeClient()
        dm, chan = FakeChannel(), FakeChannel()
        client.users[USER_ID] = FakeUser(dm)
        client.channels[42] = chan
        await adapter_with(client).send(with_fallback())
        [(text, mentions)] = dm.sent
        assert chan.sent == []
        assert "<@" + str(USER_ID) not in text  # no setter mention in a DM
        assert mentions.to_dict() == NOTHING_PINGS

    async def test_refused_dm_falls_back_to_the_channel_with_one_setter_mention(self) -> None:
        client = FakeClient()
        client.users[USER_ID] = FakeUser(
            FakeChannel(fail=http_error(discord.Forbidden, 403, 50007))
        )
        chan = FakeChannel()
        client.channels[42] = chan
        await adapter_with(client).send(with_fallback())
        [(text, mentions)] = chan.sent
        assert text.startswith(f"<@{USER_ID}> ")
        assert text.count(f"<@{USER_ID}>") == 1
        # Only the setter may be pinged: not everyone/here, not the role, not
        # the other user whose mention syntax is in the reminder text.
        assert mentions.everyone is False and mentions.roles is False
        assert mentions.replied_user is False
        assert [u.id for u in mentions.users] == [USER_ID]
        assert mentions.to_dict() == {"parse": [], "users": [USER_ID]}

    async def test_both_failing_is_permanent(self) -> None:
        client = FakeClient()
        client.users[USER_ID] = FakeUser(
            FakeChannel(fail=http_error(discord.Forbidden, 403, 50007))
        )
        client.channels[42] = FakeChannel(fail=http_error(discord.Forbidden, 403, 50013))
        with pytest.raises(DeliveryError, match="fallback channel failed too") as exc:
            await adapter_with(client).send(with_fallback("x"))
        assert exc.value.permanent

    async def test_transient_dm_error_does_not_fall_back(self) -> None:
        """Only a refusal falls back; a 5xx is retried later, still as a DM."""
        client = FakeClient()
        client.users[USER_ID] = FakeUser(
            FakeChannel(fail=http_error(discord.DiscordServerError, 503, 0))
        )
        chan = FakeChannel()
        client.channels[42] = chan
        with pytest.raises(DeliveryError) as exc:
            await adapter_with(client).send(with_fallback("x"))
        assert not exc.value.permanent and chan.sent == []


class TestPlainText:
    async def test_channel_send_escapes_markdown_and_disables_mentions(self) -> None:
        client = FakeClient()
        chan = FakeChannel()
        client.channels[42] = chan
        await adapter_with(client).send(
            OutgoingMessage(route=Route("discord", "42"), text="deploy *really* @everyone")
        )
        [(text, mentions)] = chan.sent
        assert text == r"deploy \*really\* @everyone"
        assert mentions.to_dict() == discord.AllowedMentions.none().to_dict()

    @pytest.mark.parametrize(
        ("text", "sent"),
        [
            ("v2.1_final_build", r"v2.1\_final\_build"),
            ("read https://x.com/a_b_c then_go", r"read https://x.com/a_b_c then\_go"),
            ("3. third thing", r"3\. third thing"),
            (
                "# not a heading\n> not a quote\n- not a bullet",
                "\\# not a heading\n\\> not a quote\n\\- not a bullet",
            ),
            ("||spoiler|| ~~strike~~ `code`", r"\|\|spoiler\|\| \~\~strike\~\~ \`code\`"),
            ("  #1 bins — tomorrow at 09:00", "  #1 bins — tomorrow at 09:00"),
            ("x 1.5 kg", "x 1.5 kg"),
        ],
    )
    async def test_text_arrives_as_written(self, text: str, sent: str) -> None:
        client = FakeClient()
        chan = FakeChannel()
        client.channels[42] = chan
        await adapter_with(client).send(OutgoingMessage(route=Route("discord", "42"), text=text))
        assert chan.sent[0][0] == sent


class FakeDMChannel(discord.DMChannel):
    """Real DMChannel subclass so isinstance() sees a DM; no state needed."""

    def __init__(self) -> None:
        self.id = 9001
        self.sent: list[str] = []

    async def send(self, content: Any = None, **_: Any) -> Any:
        self.sent.append(content)


async def test_dm_is_captured_as_direct_and_a_channel_asks_for_private_delivery() -> None:
    received: list[IncomingMessage] = []

    async def handler(m: IncomingMessage) -> Reply | None:
        received.append(m)
        return Reply("ok")

    adapter = adapter_with(FakeClient())
    adapter._handler = handler
    author = SimpleNamespace(bot=False, id=USER_ID, display_name="o")
    dm = FakeDMChannel()
    group = FakeChannel()
    group.id = 77  # type: ignore[attr-defined]
    for channel in (dm, group):
        message = SimpleNamespace(
            author=author, content="x in 5m", channel=channel, created_at=None
        )
        await adapter.on_message(message)  # type: ignore[arg-type]

    assert [m.direct for m in received] == [True, False]
    assert [m.deliver_privately for m in received] == [False, True]
    assert [m.route.address for m in received] == ["9001", "77"]
    assert dm.sent == ["ok"]


def test_voice_warnings_are_silenced_but_others_are_not(caplog: pytest.LogCaptureFixture) -> None:
    configure()
    log = logging.getLogger("discord.client")
    with caplog.at_level(logging.WARNING, logger="discord.client"):
        log.warning("PyNaCl is not installed, voice will NOT be supported")
        log.warning("davey is not installed, voice will NOT be supported")
        log.warning("something that actually matters")
    assert [r.getMessage() for r in caplog.records] == ["something that actually matters"]


async def test_replies_never_ping() -> None:
    """Replies echo reminder text, so they get the same treatment."""

    async def handler(m: IncomingMessage) -> Reply | None:
        return Reply(f"Got it — “{m.text}”")

    adapter = adapter_with(FakeClient())
    adapter._handler = handler
    chan = FakeChannel()
    chan.id = 77  # type: ignore[attr-defined]
    author = SimpleNamespace(bot=False, id=USER_ID, display_name="o")
    message = SimpleNamespace(author=author, content=PINGY, channel=chan, created_at=None)
    await adapter.on_message(message)  # type: ignore[arg-type]
    [(_, mentions)] = chan.sent
    assert mentions.to_dict() == NOTHING_PINGS
