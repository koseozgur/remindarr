"""Parser tests.

"Now" is pinned to **Tuesday 2026-08-18 10:30 JST** throughout, so every
expectation below can be read as an absolute date without arithmetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from remindarr.core.timeparse import TimeParseError, parse

TZ = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 8, 18, 10, 30, tzinfo=TZ).astimezone(UTC)  # Tuesday


def due_local(text: str) -> datetime:
    return parse(text, now_utc=NOW, tz=TZ).due_at.astimezone(TZ)


def task(text: str) -> str:
    return parse(text, now_utc=NOW, tz=TZ).text


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


class TestDurations:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("call mom in 20 minutes", local(2026, 8, 18, 10, 50)),
            ("call mom in 20m", local(2026, 8, 18, 10, 50)),
            ("call mom in 20 min", local(2026, 8, 18, 10, 50)),
            ("call mom in 2 hours", local(2026, 8, 18, 12, 30)),
            ("call mom in 2h", local(2026, 8, 18, 12, 30)),
            ("call mom in 1h30m", local(2026, 8, 18, 12, 0)),
            ("call mom in an hour", local(2026, 8, 18, 11, 30)),
            ("call mom in 3 days", local(2026, 8, 21, 10, 30)),
            ("call mom in 2 weeks", local(2026, 9, 1, 10, 30)),
            ("call mom in a couple of hours", local(2026, 8, 18, 12, 30)),
            ("call mom in 1 month", local(2026, 9, 18, 10, 30)),
        ],
    )
    def test_relative(self, text: str, expected: datetime) -> None:
        assert due_local(text) == expected

    def test_from_now_phrasing(self) -> None:
        assert due_local("call mom 20 minutes from now") == local(2026, 8, 18, 10, 50)

    def test_month_arithmetic_clamps_day_overflow(self) -> None:
        # 31 Jan + 1 month must be 28 Feb, not 3 March.
        jan = datetime(2026, 1, 31, 10, 0, tzinfo=TZ).astimezone(UTC)
        got = parse("x in 1 month", now_utc=jan, tz=TZ).due_at.astimezone(TZ)
        assert got == local(2026, 2, 28, 10, 0)


class TestClockTimes:
    def test_bare_hour_picks_soonest_reading(self) -> None:
        # 09:00 today has passed; 21:00 today has not.
        assert due_local("call mom at 9") == local(2026, 8, 18, 21, 0)

    def test_bare_hour_rolls_to_tomorrow_when_both_readings_passed(self) -> None:
        late = datetime(2026, 8, 18, 22, 0, tzinfo=TZ).astimezone(UTC)
        got = parse("call mom at 9", now_utc=late, tz=TZ).due_at.astimezone(TZ)
        assert got == local(2026, 8, 19, 9, 0)

    def test_ambiguous_hour_is_disclosed(self) -> None:
        assert parse("call mom at 9", now_utc=NOW, tz=TZ).warnings

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("call mom at 9am", local(2026, 8, 19, 9, 0)),  # 9am today has passed
            ("call mom at 9pm", local(2026, 8, 18, 21, 0)),
            ("call mom at 18:00", local(2026, 8, 18, 18, 0)),
            ("call mom at 12am", local(2026, 8, 19, 0, 0)),
            ("call mom at 12pm", local(2026, 8, 18, 12, 0)),
            ("call mom at 11:45", local(2026, 8, 18, 11, 45)),
        ],
    )
    def test_explicit(self, text: str, expected: datetime) -> None:
        assert due_local(text) == expected

    def test_unambiguous_hour_has_no_warning(self) -> None:
        assert not parse("call mom at 18:00", now_utc=NOW, tz=TZ).warnings

    def test_invalid_minute_rejected(self) -> None:
        with pytest.raises(TimeParseError):
            parse("call mom at 10:99", now_utc=NOW, tz=TZ)


class TestNamedDays:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("call mom tomorrow", local(2026, 8, 19, 9, 0)),
            ("call mom tomorrow at 9", local(2026, 8, 19, 9, 0)),
            ("call mom tomorrow at 09:30", local(2026, 8, 19, 9, 30)),
            ("call mom tomorrow morning", local(2026, 8, 19, 9, 0)),
            ("call mom tomorrow evening", local(2026, 8, 19, 19, 0)),
            ("call mom the day after tomorrow", local(2026, 8, 20, 9, 0)),
            ("call mom today at 18:00", local(2026, 8, 18, 18, 0)),
            ("call mom tonight", local(2026, 8, 18, 20, 0)),
            ("call mom at noon", local(2026, 8, 18, 12, 0)),
            ("call mom at midnight", local(2026, 8, 19, 0, 0)),
        ],
    )
    def test_day_words(self, text: str, expected: datetime) -> None:
        assert due_local(text) == expected

    def test_today_without_a_time_is_refused(self) -> None:
        # "today" alone is almost never what someone means.
        with pytest.raises(TimeParseError, match="needs a time"):
            parse("call mom today", now_utc=NOW, tz=TZ)

    def test_today_in_the_past_is_refused(self) -> None:
        with pytest.raises(TimeParseError, match="past"):
            parse("call mom today at 08:00", now_utc=NOW, tz=TZ)


class TestWeekdays:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("water the plants on saturday", local(2026, 8, 22, 9, 0)),
            ("water the plants on saturday morning", local(2026, 8, 22, 9, 0)),
            ("water the plants saturday at 14:00", local(2026, 8, 22, 14, 0)),
            ("standup on monday at 9am", local(2026, 8, 24, 9, 0)),
            ("call mom next friday", local(2026, 8, 21, 9, 0)),
        ],
    )
    def test_weekday(self, text: str, expected: datetime) -> None:
        assert due_local(text) == expected

    def test_same_weekday_later_today_stays_today(self) -> None:
        # Today is Tuesday 10:30; "tuesday at 18:00" means tonight.
        assert due_local("call mom tuesday at 18:00") == local(2026, 8, 18, 18, 0)

    def test_same_weekday_already_passed_rolls_a_week(self) -> None:
        assert due_local("call mom tuesday at 08:00") == local(2026, 8, 25, 8, 0)

    def test_next_is_disclosed_as_ambiguous(self) -> None:
        assert parse("call mom next friday", now_utc=NOW, tz=TZ).warnings


class TestCalendarDates:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("rent is due on 1 september", local(2026, 9, 1, 9, 0)),
            ("deploy freeze sep 3 at 10:00", local(2026, 9, 3, 10, 0)),
            ("deploy freeze on 3 september at 10:00", local(2026, 9, 3, 10, 0)),
            ("deploy freeze 2026-09-03", local(2026, 9, 3, 9, 0)),
            ("deploy freeze 2026-09-03 14:00", local(2026, 9, 3, 14, 0)),
            ("party on 3rd september", local(2026, 9, 3, 9, 0)),
        ],
    )
    def test_dates(self, text: str, expected: datetime) -> None:
        assert due_local(text) == expected

    def test_month_day_already_past_rolls_to_next_year(self) -> None:
        assert due_local("taxes on 1 february") == local(2027, 2, 1, 9, 0)

    def test_impossible_date_rejected(self) -> None:
        with pytest.raises(TimeParseError):
            parse("x on 31 february", now_utc=NOW, tz=TZ)


class TestTaskExtraction:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("call mom in 20 minutes", "call mom"),
            ("remind me to call mom in 20 minutes", "call mom"),
            ("remind me call mom in 20 minutes", "call mom"),
            ("in 20 minutes call mom", "call mom"),
            ("remind me in 20 minutes to call mom", "call mom"),
            ("/remind call mom in 20 minutes", "call mom"),
            ("don't let me forget to call mom in 20 minutes", "call mom"),
            ("call mom, in 20 minutes", "call mom"),
        ],
    )
    def test_lead_ins_and_position(self, text: str, expected: str) -> None:
        assert task(text) == expected

    def test_task_text_is_preserved_verbatim(self) -> None:
        assert task("deploy v2.1_final *really* tomorrow at 9") == "deploy v2.1_final *really*"

    def test_time_without_a_task_is_refused(self) -> None:
        with pytest.raises(TimeParseError, match="not a reminder"):
            parse("in 20 minutes", now_utc=NOW, tz=TZ)


class TestRefusals:
    @pytest.mark.parametrize(
        "text",
        [
            "call mom",
            "",
            "   ",
            "buy milk and eggs",
        ],
    )
    def test_no_time_expression(self, text: str) -> None:
        with pytest.raises(TimeParseError):
            parse(text, now_utc=NOW, tz=TZ)

    @pytest.mark.parametrize(
        "text",
        [
            "call mom about flight 320",
            "order 500 more cables",
            "check rack 42",
        ],
    )
    def test_trailing_numbers_are_not_times(self, text: str) -> None:
        """A bare trailing number must never be read as a clock time.

        This is the single most damaging failure mode: silently turning part of
        the task into a due date. Requiring "at"/"@" or am/pm is what prevents
        it.
        """
        with pytest.raises(TimeParseError):
            parse(text, now_utc=NOW, tz=TZ)

    def test_naive_now_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            parse("x in 5m", now_utc=datetime(2026, 8, 18, 10, 30), tz=TZ)


class TestTimezones:
    def test_result_is_utc(self) -> None:
        assert parse("x in 5m", now_utc=NOW, tz=TZ).due_at.tzinfo is UTC

    def test_same_wall_clock_differs_by_zone(self) -> None:
        tokyo = parse("x tomorrow at 9", now_utc=NOW, tz=ZoneInfo("Asia/Tokyo")).due_at
        london = parse("x tomorrow at 9", now_utc=NOW, tz=ZoneInfo("Europe/London")).due_at
        assert tokyo != london

    def test_tomorrow_follows_the_users_local_date_not_utc(self) -> None:
        """ "Tomorrow" is relative to the subscriber's calendar, not the server's.

        NOW is 01:30 UTC on the 18th, which is still 21:30 on the *17th* in New
        York. So a New York subscriber saying "tomorrow" means the 18th, while
        a Tokyo one means the 19th. Getting this wrong would put reminders a
        day out for anyone not sharing the server's date -- and since the whole
        point is that the owner is in Japan, that is not a hypothetical.
        """
        ny = ZoneInfo("America/New_York")
        got = parse("x tomorrow at 9", now_utc=NOW, tz=ny).due_at
        # 09:00 EDT (UTC-4) on the 18th.
        assert got == datetime(2026, 8, 18, 13, 0, tzinfo=UTC)
        assert parse("x tomorrow at 9", now_utc=NOW, tz=TZ).due_at == datetime(
            2026, 8, 19, 0, 0, tzinfo=UTC
        )  # 09:00 JST on the 19th
