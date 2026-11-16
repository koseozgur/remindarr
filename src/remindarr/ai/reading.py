"""Checking a model's :class:`Reading` and turning it into a reminder.

The model is never trusted to be right, only to be structured. Everything it
says is checked here: real dates and times, in the future, within sane
bounds, a repeat the scheduler supports. Repeats go through the grammar's own
``build_series``, so a series the AI understood is stored exactly as one the
grammar understood would be. Anything off is refused, not repaired.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Awaitable
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from remindarr.core.recurrence import Series, clamped_month_days
from remindarr.core.timeparse import DEFAULT_TIME, TimeParseError, build_series
from remindarr.core.types import ParsedReminder

from .schema import Reading, Repeat

AI_NOTE = "I read this one with AI -- check the time above"
"""Added to every AI-read confirmation. The disclosure rule: the user should
know when a guess, however good, was involved."""

MAX_TASK = 300
MAX_AHEAD = timedelta(days=366 * 10)
MAX_EVERY = {"hour": 24, "day": 365, "week": 52, "month": 24, "year": 10}

_BYDAY = {"mon": "MO", "tue": "TU", "wed": "WE", "thu": "TH", "fri": "FR", "sat": "SA", "sun": "SU"}
_ORDER = list(_BYDAY)
_FREQ = {"hour": "HOURLY", "day": "DAILY", "week": "WEEKLY", "month": "MONTHLY", "year": "YEARLY"}
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class AiUnavailable(RuntimeError):
    """The provider couldn't answer: network, timeout, quota, refusal, bad key."""


class Reader(Protocol):
    """Anything that can read a message into a :class:`Reading`."""

    provider: str
    model: str

    def read(self, text: str, *, now_local: datetime) -> Awaitable[Reading]: ...


class Unreadable(TimeParseError):
    """The model's answer was a refusal or failed the checks.

    A ``TimeParseError`` so callers treat it exactly like a grammar refusal.
    """


def to_reminder(reading: Reading, *, now_utc: datetime, tz: ZoneInfo) -> ParsedReminder:
    if reading.outcome != "reminder":
        raise Unreadable(reading.reason.strip() or "I could not find a time in that")

    task = " ".join(reading.task.split())
    if not task:
        raise Unreadable("that is a time but not a reminder -- tell me what to remind you about")
    if len(task) > MAX_TASK:
        raise Unreadable("that reminder is too long")

    at = _clock(reading.time)
    if reading.repeat is not None:
        series = _series(reading.repeat, at, now_utc=now_utc, tz=tz)
        return ParsedReminder(
            text=task,
            due_at=series.first,
            matched_phrase="",
            warnings=[AI_NOTE],
            recurrence=series.serialize(),
        )

    if reading.date is None:
        raise Unreadable("I could not find a day in that")
    try:
        day = date.fromisoformat(reading.date)
    except ValueError as exc:
        raise Unreadable("I could not read the date") from exc
    due = datetime.combine(day, at or DEFAULT_TIME, tzinfo=tz).astimezone(UTC)
    if due <= now_utc:
        raise Unreadable(f"{due.astimezone(tz):%Y-%m-%d %H:%M} is in the past")
    if due - now_utc > MAX_AHEAD:
        raise Unreadable("that is more than ten years away")
    return ParsedReminder(text=task, due_at=due, matched_phrase="", warnings=[AI_NOTE])


def _clock(raw: str | None) -> time | None:
    if raw is None or not raw.strip():
        return None
    m = _HHMM.match(raw.strip())
    if m is None:
        raise Unreadable("I could not read the time")
    return time(int(m.group(1)), int(m.group(2)))


def _series(rep: Repeat, at: time | None, *, now_utc: datetime, tz: ZoneInfo) -> Series:
    if not 1 <= rep.every <= MAX_EVERY[rep.unit]:
        raise Unreadable(f"I can't repeat every {rep.every} {rep.unit}s")
    by = ""
    if rep.unit == "week":
        if not rep.weekdays:
            raise Unreadable("which day of the week?")
        days = sorted(set(rep.weekdays), key=_ORDER.index)
        by = "BYDAY=" + ",".join(_BYDAY[d] for d in days)
    elif rep.unit == "month":
        if rep.month_day is None or not 1 <= rep.month_day <= 31:
            raise Unreadable("which day of the month?")
        by = clamped_month_days(rep.month_day)
    elif rep.unit == "year":
        if rep.month is None or not 1 <= rep.month <= 12 or rep.month_day is None:
            raise Unreadable("which date?")
        # 2024 is a leap year, so 29 Feb counts as a real date here.
        if not 1 <= rep.month_day <= calendar.monthrange(2024, rep.month)[1]:
            raise Unreadable("that is not a real date")
        day = (
            "BYMONTHDAY=28,29;BYSETPOS=-1"
            if (rep.month, rep.month_day) == (2, 29)
            else f"BYMONTHDAY={rep.month_day}"
        )
        by = f"BYMONTH={rep.month};{day}"
    return build_series(
        freq=_FREQ[rep.unit],
        by=by,
        interval=rep.every,
        at=None if rep.unit == "hour" else at,
        now_utc=now_utc,
        tz=tz,
    )
