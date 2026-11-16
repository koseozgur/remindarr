"""The optional AI reader: its checks, the router fallback, the Claude call.

Nothing here talks to a real API. A fake reader returns canned readings, and
the Claude reader is driven with its SDK call replaced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.ai.claude import ClaudeReader
from remindarr.ai.reading import AI_NOTE, AiUnavailable, Unreadable, to_reminder
from remindarr.ai.schema import Reading, Repeat
from remindarr.config import Settings
from remindarr.core.models import Reminder
from remindarr.core.router import Router
from remindarr.core.timeparse import parse

from .conftest import NOW, message

TZ = ZoneInfo("Asia/Tokyo")  # NOW is Tue 2026-08-18 10:30 JST


def reading(**kw: Any) -> Reading:
    base: dict[str, Any] = {
        "outcome": "reminder",
        "task": "call mum",
        "date": "2026-08-19",
        "time": "09:00",
        "repeat": None,
        "reason": "",
    }
    base.update(kw)
    return Reading(**base)


def weekly(*days: str, every: int = 1) -> Repeat:
    return Repeat(unit="week", every=every, weekdays=list(days), month_day=None, month=None)


class FakeReader:
    provider = "fake"
    model = "fake-1"

    def __init__(self, answer: Reading | Exception) -> None:
        self.answer = answer
        self.calls: list[tuple[str, datetime]] = []

    async def read(self, text: str, *, now_local: datetime) -> Reading:
        self.calls.append((text, now_local))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class TestToReminder:
    def test_one_off(self) -> None:
        parsed = to_reminder(reading(), now_utc=NOW, tz=TZ)
        assert parsed.text == "call mum"
        assert parsed.due_at == datetime(2026, 8, 19, 9, 0, tzinfo=TZ).astimezone(UTC)
        assert parsed.recurrence is None
        assert parsed.warnings == [AI_NOTE]

    def test_missing_time_uses_the_default(self) -> None:
        parsed = to_reminder(reading(time=None), now_utc=NOW, tz=TZ)
        assert parsed.due_at.astimezone(TZ).hour == 9

    def test_text_keeps_its_language(self) -> None:
        parsed = to_reminder(reading(task="母に電話する"), now_utc=NOW, tz=TZ)
        assert parsed.text == "母に電話する"

    def test_a_repeat_is_stored_exactly_as_the_grammar_would(self) -> None:
        """The same rule from either path must serialise identically."""
        ai = to_reminder(
            reading(
                task="standup",
                date=None,
                time="08:00",
                repeat=weekly(*["mon", "tue", "wed", "thu", "fri"]),
            ),
            now_utc=NOW,
            tz=TZ,
        )
        grammar = parse("standup every weekday at 8:00", now_utc=NOW, tz=TZ)
        assert ai.recurrence == grammar.recurrence
        assert ai.due_at == grammar.due_at

    def test_monthly_and_yearly_match_the_grammar(self) -> None:
        month = Repeat(unit="month", every=1, weekdays=[], month_day=31, month=None)
        assert (
            to_reminder(
                reading(date=None, time="09:00", repeat=month), now_utc=NOW, tz=TZ
            ).recurrence
            == parse("rent every month on the 31st at 9:00", now_utc=NOW, tz=TZ).recurrence
        )
        leap = Repeat(unit="year", every=1, weekdays=[], month_day=29, month=2)
        assert (
            to_reminder(
                reading(date=None, time="09:00", repeat=leap), now_utc=NOW, tz=TZ
            ).recurrence
            == parse("cake every year on 29 feb at 9:00", now_utc=NOW, tz=TZ).recurrence
        )

    @pytest.mark.parametrize(
        ("kw", "match"),
        [
            ({"outcome": "unsupported", "reason": "I can't do end dates"}, "end dates"),
            ({"outcome": "not_a_reminder", "reason": ""}, "could not find a time"),
            ({"task": "   "}, "not a reminder"),
            ({"task": "x" * 301}, "too long"),
            ({"date": "2026-08-17"}, "in the past"),
            ({"date": "2026-08-18", "time": "08:00"}, "in the past"),
            ({"date": "2040-01-01"}, "ten years"),
            ({"date": "next tuesday"}, "read the date"),
            ({"date": None}, "find a day"),
            ({"time": "9am"}, "read the time"),
            ({"time": "25:00"}, "read the time"),
            ({"repeat": weekly()}, "which day of the week"),
            ({"repeat": weekly("mon", every=0)}, "can't repeat every 0"),
            ({"repeat": weekly("mon", every=99)}, "can't repeat every 99"),
            (
                {"repeat": Repeat(unit="month", every=1, weekdays=[], month_day=32, month=None)},
                "day of the month",
            ),
            (
                {"repeat": Repeat(unit="year", every=1, weekdays=[], month_day=30, month=2)},
                "not a real date",
            ),
        ],
    )
    def test_anything_off_is_refused_not_repaired(self, kw: dict[str, Any], match: str) -> None:
        with pytest.raises(Unreadable, match=match):
            to_reminder(reading(**kw), now_utc=NOW, tz=TZ)


@pytest.fixture
def make_router(settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> Any:
    def make(reader: FakeReader | None) -> Router:
        return Router(
            settings=settings, session_factory=session_factory, clock=lambda: NOW, reader=reader
        )

    return make


async def reminders(factory: async_sessionmaker[AsyncSession]) -> list[Reminder]:
    async with factory() as s:
        return list((await s.execute(select(Reminder))).scalars())


class TestRouter:
    async def test_grammar_first_ai_untouched(self, make_router: Any) -> None:
        fake = FakeReader(reading())
        reply = await make_router(fake).handle(message("call mum tomorrow at 9"))
        assert reply is not None and "call mum" in reply.text
        assert fake.calls == []
        assert AI_NOTE not in reply.text

    async def test_ai_reads_what_the_grammar_cannot(
        self, make_router: Any, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        fake = FakeReader(reading(task="母に電話する", date="2026-08-19", time="15:00"))
        reply = await make_router(fake).handle(message("明日の午後3時に母に電話する"))
        assert reply is not None
        assert "母に電話する" in reply.text and "15:00" in reply.text
        assert AI_NOTE in reply.text
        [text_sent, now_local] = fake.calls[0]
        assert text_sent == "明日の午後3時に母に電話する"
        assert now_local.tzinfo is not None and now_local.utcoffset() == TZ.utcoffset(now_local)
        [row] = await reminders(session_factory)
        assert row.text == "母に電話する"

    async def test_ai_repeat_is_described_like_a_grammar_repeat(self, make_router: Any) -> None:
        fake = FakeReader(
            reading(task="薬を飲む", date=None, time="08:00", repeat=weekly("mon", "thu"))
        )
        reply = await make_router(fake).handle(message("毎週月曜と木曜の8時に薬を飲む"))
        assert reply is not None and "every Mon and Thu at 08:00" in reply.text
        assert "薬を飲む" in reply.text

    async def test_a_half_read_time_goes_to_the_ai_not_into_the_task(
        self, make_router: Any
    ) -> None:
        """Regression: the grammar used to take "thu at 8" as a one-off and
        leave "pills twice a week, mon and" as the task."""
        fake = FakeReader(
            reading(task="pills", date=None, time="08:00", repeat=weekly("mon", "thu"))
        )
        reply = await make_router(fake).handle(message("pills twice a week, mon and thu at 8"))
        assert reply is not None and "every Mon and Thu at 08:00" in reply.text
        assert len(fake.calls) == 1

    @pytest.mark.parametrize(
        "answer",
        [
            AiUnavailable("timed out"),
            reading(outcome="unsupported", reason="no end dates"),
            reading(date="2026-08-01"),
        ],
    )
    async def test_ai_failure_falls_back_to_the_grammar_reply(
        self,
        make_router: Any,
        session_factory: async_sessionmaker[AsyncSession],
        answer: Reading | Exception,
    ) -> None:
        reply = await make_router(FakeReader(answer)).handle(message("buy milk"))
        assert reply is not None and reply.text.startswith("I could not find a time in that")
        assert await reminders(session_factory) == []

    async def test_without_a_reader_nothing_changes(self, make_router: Any) -> None:
        reply = await make_router(None).handle(message("buy milk"))
        assert reply is not None and reply.text.startswith("I could not find a time in that")


class TestClaudeReader:
    def reader(self) -> ClaudeReader:
        return ClaudeReader("sk-test", model="claude-opus-5-5", effort="low", timeout=20)

    async def test_call_shape_and_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        r = self.reader()
        seen: dict[str, Any] = {}

        async def fake_parse(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(
                parsed_output=reading(),
                stop_reason="end_turn",
                model="claude-opus-5-5",
                usage=SimpleNamespace(input_tokens=400, output_tokens=60),
            )

        monkeypatch.setattr(r._client.beta.messages, "parse", fake_parse)
        now_local = NOW.astimezone(TZ)
        assert await r.read("call mum tomorrow", now_local=now_local) == reading()
        assert seen["model"] == "claude-opus-5-5"
        assert seen["output_format"] is Reading
        assert seen["output_config"] == {"effort": "low"}
        assert seen["fallbacks"] == "default"
        assert seen["betas"] == ["server-side-fallback-2026-07-01"]
        [context, msg] = seen["messages"][0]["content"]
        assert "Tuesday 2026-08-18 10:30" in context["text"]
        assert msg["text"].endswith("call mum tomorrow")

    async def test_refusal_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        r = self.reader()

        async def fake_parse(**kwargs: Any) -> Any:
            return SimpleNamespace(
                parsed_output=None,
                stop_reason="refusal",
                model="claude-opus-5-5",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

        monkeypatch.setattr(r._client.beta.messages, "parse", fake_parse)
        with pytest.raises(AiUnavailable, match="declined"):
            await r.read("x", now_local=NOW.astimezone(TZ))

    @pytest.mark.parametrize(
        ("error", "match"),
        [
            (
                lambda req, resp: anthropic.AuthenticationError(
                    "bad", response=resp(401), body=None
                ),
                "API key",
            ),
            (
                lambda req, resp: anthropic.RateLimitError("slow", response=resp(429), body=None),
                "rate limited",
            ),
            (lambda req, resp: anthropic.APITimeoutError(request=req), "timed out"),
            (lambda req, resp: anthropic.APIConnectionError(request=req), "unreachable"),
            (
                lambda req, resp: anthropic.InternalServerError(
                    "boom", response=resp(500), body=None
                ),
                "API error 500",
            ),
        ],
    )
    async def test_sdk_errors_become_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, error: Any, match: str
    ) -> None:
        r = self.reader()
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

        def response(status: int) -> httpx2.Response:
            return httpx2.Response(status, request=request)

        async def fake_parse(**kwargs: Any) -> Any:
            raise error(request, response)

        monkeypatch.setattr(r._client.beta.messages, "parse", fake_parse)
        with pytest.raises(AiUnavailable, match=match):
            await r.read("x", now_local=NOW.astimezone(TZ))


class TestConfig:
    def test_blank_key_means_no_ai(self) -> None:
        from remindarr.app import build_reader

        assert build_reader(Settings(_env_file=None, anthropic_api_key="  ")) is None

    def test_key_enables_claude(self) -> None:
        from remindarr.app import build_reader

        reader = build_reader(Settings(_env_file=None, anthropic_api_key="sk-test"))
        assert isinstance(reader, ClaudeReader) and reader.model == "claude-opus-5-5"
