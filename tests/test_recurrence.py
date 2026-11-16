"""Recurring reminders: grammar, rule expansion, scheduling, and replies.

"Now" is pinned to **Tuesday 2026-08-18 10:30 JST** unless a test says
otherwise, as in the parser tests.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.adapters.base import DeliveryError, DeliveryRegistry
from remindarr.config import Settings
from remindarr.core.models import Reminder, ReminderState
from remindarr.core.recurrence import RecurrenceError, Series, ordinal
from remindarr.core.router import Router
from remindarr.core.scheduler import Scheduler, idempotency_key
from remindarr.core.timeparse import RepeatError, TimeParseError, parse

from .conftest import NOW, FakeAdapter, MutableClock, message

TZ = ZoneInfo("Asia/Tokyo")
NY = ZoneInfo("America/New_York")


def local(y: int, mo: int, d: int, h: int, mi: int = 0, tz: ZoneInfo = TZ) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=tz)


def series(text: str, *, now: datetime = NOW, tz: ZoneInfo = TZ) -> Series:
    parsed = parse(text, now_utc=now, tz=tz)
    assert parsed.recurrence is not None, f"{text!r} parsed as a one-off"
    return Series.parse(parsed.recurrence)


def occurrences(s: Series, n: int, tz: ZoneInfo = TZ) -> list[datetime]:
    out = [s.first]
    while len(out) < n:
        nxt = s.next_after(out[-1])
        assert nxt is not None
        out.append(nxt)
    return [o.astimezone(tz) for o in out]


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


class TestGrammar:
    @pytest.mark.parametrize(
        ("text", "task", "pattern", "first"),
        [
            ("take pills every day at 9", "take pills", "every day at 09:00",
             local(2026, 8, 19, 9)),
            ("take pills every day at 11", "take pills", "every day at 11:00",
             local(2026, 8, 18, 11)),
            ("every weekday at 8:00 standup", "standup", "every weekday at 08:00",
             local(2026, 8, 19, 8)),
            ("gym every weekend at 10", "gym", "every weekend at 10:00",
             local(2026, 8, 22, 10)),
            ("bins every monday at 7pm", "bins", "every Mon at 19:00",
             local(2026, 8, 24, 19)),
            ("piano every mon and thu at 18:00", "piano", "every Mon and Thu at 18:00",
             local(2026, 8, 20, 18)),
            ("every mon, wed and fri call mom", "call mom", "every Mon, Wed and Fri at 09:00",
             local(2026, 8, 19, 9)),
            ("every tuesday at 18:00 yoga", "yoga", "every Tue at 18:00",
             local(2026, 8, 18, 18)),
            ("pay the cleaner every 2 weeks on friday", "pay the cleaner",
             "every 2 weeks on Fri at 09:00", local(2026, 8, 21, 9)),
            ("bins every other week on thursday at 7am", "bins",
             "every 2 weeks on Thu at 07:00", local(2026, 8, 20, 7)),
            ("rent every month on the 25th", "rent", "every month on the 25th at 09:00",
             local(2026, 8, 25, 9)),
            ("every month on the 1st check the meter", "check the meter",
             "every month on the 1st at 09:00", local(2026, 9, 1, 9)),
            ("insurance every 3 months on the 2nd", "insurance",
             "every 3 months on the 2nd at 09:00", local(2026, 9, 2, 9)),
            ("mum's birthday every year on 3 sep", "mum's birthday",
             "every year on 3 Sep at 09:00", local(2026, 9, 3, 9)),
            ("anniversary every year on sep 3rd at 8pm", "anniversary",
             "every year on 3 Sep at 20:00", local(2026, 9, 3, 20)),
            ("water the plants every 3 days", "water the plants", "every 3 days at 09:00",
             local(2026, 8, 19, 9)),
            ("run every other day", "run", "every 2 days at 09:00", local(2026, 8, 19, 9)),
            ("water the plants every morning", "water the plants", "every day at 09:00",
             local(2026, 8, 19, 9)),
            ("drinks every friday evening", "drinks", "every Fri at 19:00",
             local(2026, 8, 21, 19)),
            ("cleaner every week on friday", "cleaner", "every Fri at 09:00",
             local(2026, 8, 21, 9)),
            ("tea every afternoon", "tea", "every day at 14:00", local(2026, 8, 18, 14)),
            ("bins every weekdays at 7am", "bins", "every weekday at 07:00",
             local(2026, 8, 19, 7)),
            ("stretch every hour", "stretch", "every hour", local(2026, 8, 18, 11, 30)),
            ("stretch every 2 hours", "stretch", "every 2 hours", local(2026, 8, 18, 12, 30)),
            # A time before "every" is the series' time, not task text.
            ("call mom at 7pm every day", "call mom", "every day at 19:00",
             local(2026, 8, 18, 19)),
            # Lead-ins work as for one-offs, with the repeat at the head.
            ("remind me every monday at 9 to call mom", "call mom", "every Mon at 09:00",
             local(2026, 8, 24, 9)),
        ],
    )  # fmt: skip
    def test_supported_forms(self, text: str, task: str, pattern: str, first: datetime) -> None:
        parsed = parse(text, now_utc=NOW, tz=TZ)
        assert parsed.text == task
        assert parsed.recurrence is not None
        s = Series.parse(parsed.recurrence)
        assert s.describe() == pattern
        assert parsed.due_at == first
        assert s.first == first

    def test_bare_hour_is_read_as_written_and_disclosed(self) -> None:
        # Said at 15:00, "at 9" one-off means 21:00 -- but a series' time must
        # not depend on when it was set, so it is 09:00, with a note.
        afternoon = local(2026, 8, 18, 15).astimezone(UTC)
        parsed = parse("pills every day at 9", now_utc=afternoon, tz=TZ)
        assert parsed.due_at == local(2026, 8, 19, 9)
        assert any("09:00" in w and "am or pm" in w for w in parsed.warnings)

    def test_am_pm_needs_no_note(self) -> None:
        assert parse("pills every day at 9pm", now_utc=NOW, tz=TZ).warnings == []

    def test_one_offs_still_have_no_recurrence(self) -> None:
        assert parse("call mom tomorrow at 9", now_utc=NOW, tz=TZ).recurrence is None

    @pytest.mark.parametrize(
        ("text", "task"),
        [
            # A hyphenated "every-day" is task text, not a repeat.
            ("call the every-day helpline tomorrow", "call the every-day helpline"),
            ("check every room tomorrow at 9", "check every room"),
            ("read the daily mail tomorrow", "read the daily mail"),
            ("everyday carry check tomorrow", "everyday carry check"),
        ],
    )
    def test_never_eats_task_text(self, text: str, task: str) -> None:
        parsed = parse(text, now_utc=NOW, tz=TZ)
        assert parsed.recurrence is None
        assert parsed.text == task

    @pytest.mark.parametrize(
        "text",
        [
            "water plants every 2 weeks",  # which day?
            "x every week",
            "x every month",  # which day of the month?
            "x every year",  # which date?
            "blink every 5 minutes",  # more often than hourly
            "blink every minute",
            "take pills daily at 8",  # "daily" next to a one-off time
            "take pills at 8 daily",
            "call mom every monday at 9 until june",  # trailing words
            "x each monday",
            "pay rent every 25th",
            "x every hour at 9",
            "x every few days",
            "x every fortnight",
        ],
    )
    def test_refuses_what_it_cannot_repeat(self, text: str) -> None:
        with pytest.raises(RepeatError, match="can't repeat"):
            parse(text, now_utc=NOW, tz=TZ)

    @pytest.mark.parametrize(
        "text",
        [
            "x every month on the 32nd",
            "x every year on 30 feb",
            "x at 3 every day at 9",  # two times
            "every day at 9",  # no task
        ],
    )
    def test_rejects_nonsense(self, text: str) -> None:
        with pytest.raises(TimeParseError):
            parse(text, now_utc=NOW, tz=TZ)


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


class TestExpansion:
    def test_weekdays_skip_the_weekend(self) -> None:
        got = occurrences(series("standup every weekday at 8am"), 6)
        assert [d.strftime("%a %d") for d in got] == [
            "Wed 19", "Thu 20", "Fri 21", "Mon 24", "Tue 25", "Wed 26",
        ]  # fmt: skip
        assert {d.hour for d in got} == {8}

    def test_every_two_weeks_keeps_its_phase(self) -> None:
        got = occurrences(series("cleaner every 2 weeks on mon and thu at 10am"), 4)
        assert [d.date().isoformat() for d in got] == [
            "2026-08-20", "2026-08-31", "2026-09-03", "2026-09-14",
        ]  # fmt: skip

    def test_every_two_weeks_starts_at_the_coming_day(self) -> None:
        # Said on a Saturday: the coming Friday, not the one after.
        saturday = local(2026, 8, 22, 12).astimezone(UTC)
        got = occurrences(series("cleaner every 2 weeks on friday", now=saturday), 2)
        assert [d.date().isoformat() for d in got] == ["2026-08-28", "2026-09-11"]

    def test_31st_falls_back_to_the_last_day_of_shorter_months(self) -> None:
        got = occurrences(series("rent every month on the 31st"), 7)
        assert [d.date().isoformat() for d in got] == [
            "2026-08-31", "2026-09-30", "2026-10-31", "2026-11-30",
            "2026-12-31", "2027-01-31", "2027-02-28",
        ]  # fmt: skip
        assert series("rent every month on the 31st").describe() == (
            "every month on the 31st (or the last day) at 09:00"
        )

    def test_30th_in_a_leap_february(self) -> None:
        jan = local(2028, 1, 15, 12).astimezone(UTC)
        got = occurrences(series("x every month on the 30th", now=jan), 3)
        assert [d.date().isoformat() for d in got] == ["2028-01-30", "2028-02-29", "2028-03-30"]

    def test_29_feb_yearly_falls_back_to_28_feb(self) -> None:
        got = occurrences(series("x every year on 29 feb"), 3)
        assert [d.date().isoformat() for d in got] == ["2027-02-28", "2028-02-29", "2029-02-28"]

    def test_yearly(self) -> None:
        got = occurrences(series("x every year on 3 sep"), 2)
        assert [d.date().isoformat() for d in got] == ["2026-09-03", "2027-09-03"]


class TestDst:
    """America/New_York: clocks go back 2026-11-01 02:00 and forward 2027-03-14."""

    def test_daily_keeps_wall_clock_time_across_fall_back(self) -> None:
        now = local(2026, 10, 30, 12, tz=NY).astimezone(UTC)
        got = occurrences(series("pills every day at 8am", now=now, tz=NY), 4, NY)
        assert [(d.day, d.hour) for d in got] == [(31, 8), (1, 8), (2, 8), (3, 8)]
        # Same wall clock, different UTC offset either side of the change.
        assert got[0].utcoffset() == timedelta(hours=-4)
        assert got[1].utcoffset() == timedelta(hours=-5)

    def test_weekly_keeps_wall_clock_time_across_spring_forward(self) -> None:
        now = local(2027, 3, 1, 12, tz=NY).astimezone(UTC)
        got = occurrences(series("x every monday at 9am", now=now, tz=NY), 3, NY)
        assert [(d.day, d.hour) for d in got] == [(8, 9), (15, 9), (22, 9)]

    def test_hourly_stays_an_hour_apart_across_fall_back(self) -> None:
        now = local(2026, 11, 1, 0, 0, tz=NY).astimezone(UTC)
        s = series("x every hour", now=now, tz=NY)
        utc = [o.astimezone(UTC) for o in occurrences(s, 4, NY)]
        assert all(b - a == timedelta(hours=1) for a, b in itertools.pairwise(utc))

    def test_time_in_the_spring_forward_gap_still_fires_that_day(self) -> None:
        # 02:30 does not exist on 2027-03-14 in New York; fold=0 maps it to
        # 03:30 EDT, and the series returns to 02:30 the next day.
        now = local(2027, 3, 13, 12, tz=NY).astimezone(UTC)
        got = occurrences(series("x every day at 2:30am", now=now, tz=NY), 3, NY)
        assert [(d.day, d.hour, d.minute) for d in got] == [(14, 3, 30), (15, 2, 30), (16, 2, 30)]

    def test_repeated_hour_at_fall_back_fires_once(self) -> None:
        now = local(2026, 10, 31, 12, tz=NY).astimezone(UTC)
        got = occurrences(series("x every day at 1:30am", now=now, tz=NY), 3, NY)
        utc = [o.astimezone(UTC) for o in got]
        assert [d.day for d in got] == [1, 2, 3]
        assert utc[1] - utc[0] == timedelta(hours=25)


class TestSeriesStorage:
    def test_round_trip(self) -> None:
        s = series("piano every mon and thu at 18:00")
        assert Series.parse(s.serialize()) == s
        assert s.serialize().startswith("DTSTART;TZID=Asia/Tokyo:20260820T180000\nRRULE:")

    def test_hourly_is_stored_on_utc(self) -> None:
        assert series("x every hour").serialize().startswith("DTSTART:20260818T023000Z\n")

    def test_fits_the_column(self) -> None:
        longest = series(
            "x every 52 weeks on mon, tue, wed, thu, fri, sat and sun at 23:59",
            tz=ZoneInfo("America/Argentina/ComodRivadavia"),
        )
        assert len(longest.serialize()) <= 256

    @pytest.mark.parametrize(
        "stored",
        [
            "FREQ=DAILY",
            "DTSTART;TZID=Nowhere/Land:20260101T090000\nRRULE:FREQ=DAILY",
            "DTSTART:2026\nRRULE:FREQ=DAILY",
            "DTSTART:20260101T090000Z\nRRULE:FREQ=SOMETIMES",
        ],
    )
    def test_unreadable_rules_are_rejected(self, stored: str) -> None:
        with pytest.raises(RecurrenceError):
            Series.parse(stored)

    @pytest.mark.parametrize(
        ("n", "text"),
        [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"),
         (13, "13th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (31, "31st")],
    )  # fmt: skip
    def test_ordinals(self, n: int, text: str) -> None:
        assert ordinal(n) == text


# ---------------------------------------------------------------------------
# Scheduling, through the router and the scheduler together
# ---------------------------------------------------------------------------


async def only_row(factory: async_sessionmaker[AsyncSession]) -> Reminder:
    async with factory() as s:
        return (await s.execute(select(Reminder))).scalar_one()


class Harness:
    def __init__(
        self,
        settings: Settings,
        factory: async_sessionmaker[AsyncSession],
        registry: DeliveryRegistry,
    ) -> None:
        self.clock = MutableClock()
        self.factory = factory
        self.router = Router(settings=settings, session_factory=factory, clock=self.clock)
        self.scheduler = Scheduler(
            settings=settings, session_factory=factory, registry=registry, clock=self.clock
        )

    async def say(self, text: str) -> str:
        reply = await self.router.handle(message(text))
        assert reply is not None
        return reply.text

    def at(self, when: datetime) -> None:
        self.clock.now = when.astimezone(UTC)

    async def tick_at(self, when: datetime) -> int:
        self.at(when)
        return await self.scheduler.tick()


@pytest.fixture
def h(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
) -> Harness:
    return Harness(settings, session_factory, registry)


class TestScheduling:
    async def test_delivers_then_schedules_the_next(self, h: Harness, adapter: FakeAdapter) -> None:
        await h.say("take pills every day at 9pm")
        assert await h.tick_at(local(2026, 8, 18, 21)) == 1
        assert adapter.texts == ["⏰ take pills\n(repeats every day at 21:00)"]

        row = await only_row(h.factory)
        assert row.state is ReminderState.PENDING
        assert row.due_at == local(2026, 8, 19, 21)
        assert row.next_attempt_at == row.due_at
        assert row.attempts == 0
        assert row.sent_at == local(2026, 8, 18, 21)

        # Not again until tomorrow, then exactly once.
        assert await h.tick_at(local(2026, 8, 19, 20, 59)) == 0
        assert await h.tick_at(local(2026, 8, 19, 21)) == 1
        assert await h.tick_at(local(2026, 8, 19, 21, 1)) == 0
        assert len(adapter.sent) == 2

    async def test_permanent_failure_does_not_end_the_series(
        self, h: Harness, adapter: FakeAdapter
    ) -> None:
        await h.say("take pills every day at 9pm")
        adapter.fail_with = DeliveryError("blocked", permanent=True)
        adapter.fail_times = 1
        await h.tick_at(local(2026, 8, 18, 21))
        row = await only_row(h.factory)
        assert row.state is ReminderState.PENDING
        assert row.due_at == local(2026, 8, 19, 21)
        assert row.last_error == "blocked"

        await h.tick_at(local(2026, 8, 19, 21))
        assert adapter.texts == ["⏰ take pills\n(repeats every day at 21:00)"]
        assert (await only_row(h.factory)).last_error is None

    async def test_giving_up_on_retries_moves_on_to_the_next(
        self, h: Harness, adapter: FakeAdapter, settings: Settings
    ) -> None:
        await h.say("stretch every day at 9pm")
        adapter.fail_with = DeliveryError("network blip")
        adapter.fail_times = -1  # always
        when = local(2026, 8, 18, 21)
        for _ in range(settings.max_delivery_attempts):
            await h.tick_at(when)
            when += timedelta(minutes=10)
        row = await only_row(h.factory)
        assert row.state is ReminderState.PENDING
        assert row.due_at == local(2026, 8, 19, 21)
        assert row.attempts == 0

    async def test_retries_keep_one_key_and_occurrences_get_new_ones(
        self, h: Harness, adapter: FakeAdapter
    ) -> None:
        await h.say("stretch every day at 9pm")
        adapter.fail_with = DeliveryError("network blip")
        adapter.fail_times = 1
        first_key = idempotency_key(await only_row(h.factory))
        await h.tick_at(local(2026, 8, 18, 21))  # fails, retry scheduled
        assert idempotency_key(await only_row(h.factory)) == first_key
        await h.tick_at(local(2026, 8, 18, 22))  # retry lands
        await h.tick_at(local(2026, 8, 19, 21))  # next occurrence
        keys = [m.idempotency_key for m in adapter.sent]
        assert keys[0] == first_key
        assert len(set(keys)) == 2

    async def test_downtime_delivers_one_catch_up_then_resumes(
        self, h: Harness, adapter: FakeAdapter
    ) -> None:
        await h.say("take pills every day at 9pm")
        # Down from before the first occurrence until three days later.
        assert await h.tick_at(local(2026, 8, 21, 22, 30)) == 1
        assert await h.tick_at(local(2026, 8, 21, 22, 31)) == 0
        assert len(adapter.sent) == 1
        # The catch-up is for the latest missed occurrence, an hour ago.
        assert adapter.texts[0] == (
            "⏰ take pills\n(repeats every day at 21:00)\n"
            "(this was due 1 hour ago — delivered late)"
        )
        row = await only_row(h.factory)
        assert row.due_at == local(2026, 8, 22, 21)

        await h.tick_at(local(2026, 8, 22, 21))
        assert len(adapter.sent) == 2
        assert "late" not in adapter.texts[1]

    async def test_cancel_stops_the_series(self, h: Harness, adapter: FakeAdapter) -> None:
        await h.say("take pills every day at 9pm")
        await h.tick_at(local(2026, 8, 18, 21))
        reply = await h.say(f"/cancel {(await only_row(h.factory)).id}")
        assert reply == "Cancelled “take pills” (every day at 21:00) — no more repeats."
        for day in range(19, 25):
            await h.tick_at(local(2026, 8, day, 21))
        assert len(adapter.sent) == 1
        assert (await only_row(h.factory)).state is ReminderState.CANCELLED

    async def test_unreadable_rule_delivers_once_and_ends(
        self, h: Harness, adapter: FakeAdapter
    ) -> None:
        await h.say("take pills every day at 9pm")
        async with h.factory() as s:
            row = (await s.execute(select(Reminder))).scalar_one()
            row.recurrence = "garbage"
            await s.commit()
        await h.tick_at(local(2026, 8, 18, 21))
        await h.tick_at(local(2026, 8, 19, 21))
        assert adapter.texts == ["⏰ take pills"]
        assert (await only_row(h.factory)).state is ReminderState.SENT


class TestReplies:
    async def test_confirmation_states_pattern_and_next(self, h: Harness) -> None:
        reply = await h.say("standup every weekday at 8:00")
        assert reply.startswith(
            "Got it — “standup” every weekday at 08:00, next: Wed 19 Aug 08:00 (in 21 hours)."
        )
        assert "note: read “8” as 08:00" in reply

    async def test_confirmation_names_the_year_when_it_differs(self, h: Harness) -> None:
        reply = await h.say("x every year on 29 feb")
        assert "next: Sun 28 Feb 2027 09:00" in reply

    async def test_list_marks_repeats(self, h: Harness) -> None:
        await h.say("call the dentist tomorrow at 9")
        await h.say("standup every weekday at 8:00")
        reply = await h.say("/list")
        lines = reply.splitlines()
        assert lines[1].endswith(
            "🔁 standup — every weekday at 08:00, next Wed 19 Aug 08:00 (in 21 hours)"
        )
        assert "🔁" not in lines[2]
        assert "call the dentist — tomorrow at 09:00" in lines[2]

    async def test_list_shows_the_next_occurrence_after_a_delivery(
        self, h: Harness, adapter: FakeAdapter
    ) -> None:
        await h.say("stretch every day at 9pm")
        await h.tick_at(local(2026, 8, 18, 21))
        assert "next Wed 19 Aug 21:00 (in 1 day)" in await h.say("/list")

    async def test_refusal_is_explained(self, h: Harness) -> None:
        reply = await h.say("blink every 5 minutes")
        assert reply.startswith("I can't repeat that yet")
        async with h.factory() as s:
            assert (await s.execute(select(Reminder))).first() is None


def test_now_is_the_documented_tuesday() -> None:
    assert NOW.astimezone(TZ) == local(2026, 8, 18, 10, 30)
