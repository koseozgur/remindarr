"""Natural-language time parsing for reminder capture.

Deliberately a *narrow, explicit* grammar rather than a fuzzy general parser.
A general parser's failure mode is silently reading part of the task text as a
date ("call Mars about Q3" -> March), which produces a confidently wrong
reminder. A narrow grammar's failure mode is saying "I did not understand that
time", which the user can immediately fix. For a tool whose whole job is being
trusted to fire at the right moment, the second failure is far cheaper.

Supported, all resolved in the subscriber's local timezone:

    in 20 minutes / in 20m / in 1h30m / in an hour / in 3 days / in 2 weeks
    tomorrow / tomorrow at 9 / tomorrow at 09:30 / tomorrow morning
    today at 18:00 / tonight / this evening / noon / midnight
    at 9am / at 18:00 / at 9
    monday / on monday at 9am / next friday
    sep 3 / on 3 september at 10:00 / 2026-09-03 / 2026-09-03 14:00

Repeats (see the README's "Recurring reminders" for the semantics):

    every day at 9 / every 3 days / every other day / every morning
    every weekday at 8:00 / every weekend / every mon and thu at 18:00
    every 2 weeks on friday / every month on the 25th / every year on 3 sep
    every hour / every 2 hours

Bare 12-hour times without am/pm resolve to whichever reading comes soonest --
"at 9" said at 15:00 means 21:00 today, said at 22:00 means 09:00 tomorrow. The
confirmation message always echoes the resolved absolute time, so a wrong guess
is visible immediately rather than at the moment the reminder fails to fire.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .recurrence import Series, clamped_month_days
from .types import ParsedReminder


class TimeParseError(ValueError):
    """Raised when no time expression could be understood."""


# A reminder with a day but no time lands here.
DEFAULT_TIME = time(9, 0)
MORNING = time(9, 0)
AFTERNOON = time(14, 0)
EVENING = time(19, 0)
NIGHT = time(20, 0)

_LEAD_INS = [
    r"^/remind(?:me)?\b",
    r"^remind\s+me\s+to\b",
    r"^remind\s+me\b",
    r"^remind\s+us\s+to\b",
    r"^remind\b",
    r"^remember\s+to\b",
    r"^don'?t\s+let\s+me\s+forget\s+to\b",
    r"^note\s+to\s+self:?\b",
]

_UNIT_SECONDS: dict[str, int] = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "week": 604800, "weeks": 604800,
}  # fmt: skip

# Months and years are calendar arithmetic, not fixed durations.
_MONTH_UNITS = {"mo", "mon", "month", "months"}
_YEAR_UNITS = {"y", "yr", "yrs", "year", "years"}

_WEEKDAYS: dict[str, int] = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}  # fmt: skip

_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}  # fmt: skip


# A clock time: "9", "9am", "09:30", "18:00", "9.30pm"
def _clock_re(prefix: str = "") -> str:
    """A clock-time regex; ``prefix`` renames its groups so two can share a pattern."""
    return (
        rf"(?P<{prefix}hour>\d{{1,2}})(?:[:.](?P<{prefix}minute>\d{{2}}))?"
        rf"\s*(?P<{prefix}ampm>am|pm|a\.m\.|p\.m\.)?"
    )


_CLOCK = _clock_re()
_AT = r"(?:at\s+|@\s*)"
_DAYPART = r"(?P<daypart>morning|afternoon|evening|night|noon|midday|midnight)"

_WEEKDAY_ALT = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_UNIT_ALT = "|".join(sorted(set(_UNIT_SECONDS) | _MONTH_UNITS | _YEAR_UNITS, key=len, reverse=True))

# The unit is terminated by "not another letter" rather than \b, because \b does
# not fire between the "h" and the "3" of "1h30m" -- which silently dropped the
# leading chunk and produced a reminder 60 minutes early.
_UNIT_END = r"(?![a-z])"
_DURATION_CHUNK = rf"(?:\d+|an?|a\s+couple\s+of|few)\s*(?:{_UNIT_ALT}){_UNIT_END}"
_DURATION = rf"(?P<duration>{_DURATION_CHUNK}(?:\s*(?:and\s+)?{_DURATION_CHUNK})*)"


@dataclass(frozen=True, slots=True)
class _Resolution:
    when: datetime
    warnings: tuple[str, ...] = ()


def _strip_lead_ins(text: str) -> str:
    out = text.strip()
    changed = True
    while changed:
        changed = False
        for pattern in _LEAD_INS:
            new = re.sub(pattern, "", out, count=1, flags=re.IGNORECASE).strip()
            if new != out:
                out, changed = new, True
    return out


def _add_months(dt: datetime, months: int) -> datetime:
    """Calendar-correct month arithmetic, clamping day overflow.

    Jan 31 + 1 month is Feb 28 (or 29), not March 3.
    """
    total = dt.month - 1 + months
    year = dt.year + total // 12
    month = total % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _parse_count(raw: str) -> int:
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    if raw in {"a", "an"}:
        return 1
    if raw == "few":
        return 3
    return 2  # "a couple of"


def _resolve_duration(phrase: str, now_local: datetime) -> _Resolution:
    chunk_re = re.compile(
        rf"(?P<count>\d+|an?|a\s+couple\s+of|few)\s*(?P<unit>{_UNIT_ALT}){_UNIT_END}",
        re.IGNORECASE,
    )
    seconds = 0
    months = 0
    years = 0
    found = False
    for m in chunk_re.finditer(phrase):
        found = True
        count = _parse_count(m.group("count").lower())
        unit = m.group("unit").lower()
        if unit in _MONTH_UNITS:
            months += count
        elif unit in _YEAR_UNITS:
            years += count
        else:
            seconds += count * _UNIT_SECONDS[unit]
    if not found:
        raise TimeParseError(f"could not read a duration from {phrase!r}")

    when = now_local
    if years:
        when = _add_months(when, years * 12)
    if months:
        when = _add_months(when, months)
    return _Resolution(when + timedelta(seconds=seconds))


def _daypart_time(name: str) -> time:
    return {
        "morning": MORNING,
        "afternoon": AFTERNOON,
        "evening": EVENING,
        "night": NIGHT,
        "noon": time(12, 0),
        "midday": time(12, 0),
        "midnight": time(0, 0),
    }[name]


def _clock_from_match(m: re.Match[str], prefix: str = "") -> tuple[time, bool]:
    """Return the clock time and whether the 12/24-hour reading was ambiguous."""
    hour = int(m.group(f"{prefix}hour"))
    minute = int(m.group(f"{prefix}minute") or 0)
    ampm = (m.group(f"{prefix}ampm") or "").replace(".", "").lower()

    if minute > 59:
        raise TimeParseError(f"{minute} is not a valid minute")

    if ampm:
        if not 1 <= hour <= 12:
            raise TimeParseError(f"{hour}{ampm} is not a valid time")
        # 12am is 00:00 and 12pm is 12:00 -- the only two hours where the
        # 12-hour clock does not simply add twelve.
        if ampm == "am" and hour == 12:
            hour = 0
        elif ampm == "pm" and hour != 12:
            hour += 12
        return time(hour, minute), False

    if hour > 23:
        raise TimeParseError(f"{hour} is not a valid hour")

    # No am/pm. Hours 0 and 13-23 can only be read one way; 1-12 cannot.
    return time(hour, minute), 1 <= hour <= 12


def _soonest_reading(now_local: datetime, base: time, ambiguous: bool) -> _Resolution:
    """Pick the next future occurrence of a clock time.

    When the 12-hour reading is ambiguous, both readings are candidates and the
    soonest future one wins.
    """
    candidates: list[datetime] = []
    readings = [base]
    if ambiguous:
        alt_hour = (base.hour + 12) % 24
        readings.append(time(alt_hour, base.minute))

    for reading in readings:
        for day_offset in (0, 1):
            candidate = datetime.combine(
                now_local.date() + timedelta(days=day_offset), reading, tzinfo=now_local.tzinfo
            )
            if candidate > now_local:
                candidates.append(candidate)
                break

    chosen = min(candidates)
    warnings: tuple[str, ...] = ()
    if ambiguous:
        warnings = (f"read “{base.hour}” as {chosen:%H:%M}",)
    return _Resolution(chosen, warnings)


def _on_date(
    target: date, clock: time | None, now_local: datetime, *, allow_past: bool = False
) -> _Resolution:
    when = datetime.combine(target, clock or DEFAULT_TIME, tzinfo=now_local.tzinfo)
    if when <= now_local and not allow_past:
        raise TimeParseError(f"{when:%Y-%m-%d %H:%M} is in the past")
    return _Resolution(when)


def _next_weekday(
    now_local: datetime, weekday: int, clock: time | None, *, said_next: bool
) -> _Resolution:
    """Soonest future occurrence of a weekday.

    "next friday" is treated the same as "friday". English genuinely does not
    agree on whether it means the upcoming Friday or the one after, so rather
    than pick a rule and be silently wrong half the time, we resolve to the
    soonest and disclose the date in the confirmation.
    """
    days_ahead = (weekday - now_local.weekday()) % 7
    target = now_local.date() + timedelta(days=days_ahead)
    when = datetime.combine(target, clock or DEFAULT_TIME, tzinfo=now_local.tzinfo)
    if when <= now_local:
        when += timedelta(days=7)
    warnings = (f"“next” is ambiguous -- picked {when:%a %d %b}",) if said_next else ()
    return _Resolution(when, warnings)


# ---------------------------------------------------------------------------
# Pattern table. Order matters: more specific forms first.
# ---------------------------------------------------------------------------

_Handler = "object"  # documented below; concrete type is a callable


def _build_patterns() -> list[tuple[str, object]]:
    def h_iso(m: re.Match[str], now: datetime) -> _Resolution:
        target = date(int(m.group("y")), int(m.group("mo")), int(m.group("d")))
        clock = None
        if m.group("hour"):
            clock, _ = _clock_from_match(m)
        return _on_date(target, clock, now)

    def h_duration(m: re.Match[str], now: datetime) -> _Resolution:
        return _resolve_duration(m.group("duration"), now)

    def h_tomorrow(m: re.Match[str], now: datetime) -> _Resolution:
        clock = _clock_or_daypart(m)
        return _on_date(now.date() + timedelta(days=1), clock, now, allow_past=True)

    def h_day_after_tomorrow(m: re.Match[str], now: datetime) -> _Resolution:
        clock = _clock_or_daypart(m)
        return _on_date(now.date() + timedelta(days=2), clock, now, allow_past=True)

    def h_today(m: re.Match[str], now: datetime) -> _Resolution:
        clock = _clock_or_daypart(m)
        if clock is None:
            raise TimeParseError("“today” needs a time -- try “today at 18:00”")
        return _on_date(now.date(), clock, now)

    def h_tonight(m: re.Match[str], now: datetime) -> _Resolution:
        return _on_date(now.date(), NIGHT, now)

    def h_daypart_only(m: re.Match[str], now: datetime) -> _Resolution:
        return _soonest_reading(now, _daypart_time(m.group("daypart").lower()), ambiguous=False)

    def h_weekday(m: re.Match[str], now: datetime) -> _Resolution:
        clock = _clock_or_daypart(m)
        return _next_weekday(
            now,
            _WEEKDAYS[m.group("weekday").lower()],
            clock,
            said_next=bool(m.group("nextword")),
        )

    def h_next_unit(m: re.Match[str], now: datetime) -> _Resolution:
        unit = m.group("nunit").lower()
        if unit == "week":
            return _Resolution(
                datetime.combine(now.date() + timedelta(days=7), DEFAULT_TIME, tzinfo=now.tzinfo)
            )
        if unit == "month":
            return _Resolution(
                _add_months(datetime.combine(now.date(), DEFAULT_TIME, tzinfo=now.tzinfo), 1)
            )
        return _Resolution(
            _add_months(datetime.combine(now.date(), DEFAULT_TIME, tzinfo=now.tzinfo), 12)
        )

    def h_month_day(m: re.Match[str], now: datetime) -> _Resolution:
        month = _MONTHS[m.group("month").lower()]
        day = int(m.group("day"))
        clock = None
        if m.group("hour"):
            clock, _ = _clock_from_match(m)
        year = now.year
        try:
            target = date(year, month, day)
        except ValueError as exc:
            raise TimeParseError(f"{m.group('month')} {day} is not a real date") from exc
        candidate = datetime.combine(target, clock or DEFAULT_TIME, tzinfo=now.tzinfo)
        if candidate <= now:
            target = date(year + 1, month, day)
        return _on_date(target, clock, now)

    def h_at_clock(m: re.Match[str], now: datetime) -> _Resolution:
        clock, ambiguous = _clock_from_match(m)
        return _soonest_reading(now, clock, ambiguous)

    def _clock_or_daypart(m: re.Match[str]) -> time | None:
        groups = m.groupdict()
        if groups.get("daypart"):
            return _daypart_time(groups["daypart"].lower())
        if groups.get("hour"):
            clock, _ = _clock_from_match(m)
            return clock
        return None

    time_suffix = rf"(?:\s+(?:{_AT})?(?:{_CLOCK}|{_DAYPART}))?"

    return [
        # 2026-09-03 / 2026-09-03 14:00
        (rf"(?P<y>\d{{4}})-(?P<mo>\d{{2}})-(?P<d>\d{{2}})(?:\s+(?:{_AT})?{_CLOCK})?", h_iso),
        # in 20 minutes / in 1h30m
        (rf"in\s+{_DURATION}", h_duration),
        # 20 minutes from now
        (rf"{_DURATION}\s+from\s+now", h_duration),
        # day after tomorrow at 9
        (rf"(?:the\s+)?day\s+after\s+tomorrow{time_suffix}", h_day_after_tomorrow),
        (rf"tomorrow{time_suffix}", h_tomorrow),
        (rf"today{time_suffix}", h_today),
        (r"tonight", h_tonight),
        # next monday at 9 / on friday / this thursday
        (
            rf"(?:(?P<nextword>next)\s+|this\s+|on\s+|coming\s+)?(?P<weekday>{_WEEKDAY_ALT})\b{time_suffix}",
            h_weekday,
        ),
        (r"next\s+(?P<nunit>week|month|year)", h_next_unit),
        # sep 3 at 10:00 / 3 september
        (
            rf"(?:on\s+)?(?P<month>{_MONTH_ALT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:\s+(?:{_AT})?{_CLOCK})?",
            h_month_day,
        ),
        (
            rf"(?:on\s+)?(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>{_MONTH_ALT})\.?(?:\s+(?:{_AT})?{_CLOCK})?",
            h_month_day,
        ),
        # this evening / at noon
        (rf"(?:this\s+|at\s+)?{_DAYPART}", h_daypart_only),
        # at 9am -- requires "at"/"@" so trailing digits in task text are not eaten
        (rf"{_AT}{_CLOCK}", h_at_clock),
    ]


_PATTERNS = _build_patterns()


# ---------------------------------------------------------------------------
# Repeats. Tried before the one-off patterns: otherwise "call mom every monday"
# would match the one-off "monday" at the tail and leave "every" as task text.
# ---------------------------------------------------------------------------


class RepeatError(TimeParseError):
    """A repeat was recognised as one, but is outside what the grammar supports."""


REPEAT_EXAMPLES = (
    "“every day at 9”, “every weekday at 8:00”, “every mon and thu at 18:00”, "
    "“every 2 weeks on friday”, “every month on the 25th”, “every year on 3 sep” "
    "or “every hour”"
)

_BYDAY = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_DAY_SEP = r"(?:\s*,\s*(?:and\s+)?|\s+and\s+|\s*&\s*)"
_WEEKDAY_LIST = rf"(?P<days>(?:{_WEEKDAY_ALT})(?:{_DAY_SEP}(?:{_WEEKDAY_ALT}))*)\b"
_EVERY_N = r"(?:(?P<n>\d+|other)\s+)?"
# "call mom at 7pm every day": a time *before* "every" is the series' time too.
# Without this the tail match would take "every day" alone and leave "at 7pm"
# as task text, on a series firing at the 09:00 default.
_REPEAT_LEAD = rf"(?:{_AT}{_clock_re('l')}\s+)?"
_REPEAT_TRAIL = rf"(?:\s+(?:{_AT})?(?:{_CLOCK}|{_DAYPART}))?"
_ORD = r"(?:st|nd|rd|th)?"

# "every <something time-like>" that no repeat pattern took. Refusing beats
# silently setting a one-off: "water plants every 2 weeks" must not quietly
# become nothing, and "call mom every monday at 9 until june" must not become a
# one-off with "every monday at 9 until" as its text.
_REPEAT_HINT = re.compile(
    r"(?:^|\s)(?:every|each)\s+(?:\d|other\b|few\b|couple\b|"
    rf"(?:day|weekday|weekend|week|fortnight|month|year|hour|hr|minute|min|second|sec|"
    rf"morning|afternoon|evening|night|noon|midnight|{_WEEKDAY_ALT})s?\b|"
    # "every last friday", "every first monday of the month": nth-weekday
    # repeats the grammar does not do. Scoped to a following day or unit, so
    # "check every last detail tomorrow" stays a one-off.
    rf"(?:last|first|second|third|fourth|fifth|next)\s+"
    rf"(?:day|weekday|weekend|week|month|year|{_WEEKDAY_ALT})s?\b)",
    re.IGNORECASE,
)
# The same words used as a repeat: "take pills daily at 8" is a repeat the
# grammar does not speak, not a one-off reminder to "take pills daily". Only
# where the reading is unambiguous -- right beside a clock time, or ending the
# task -- so "daily standup tomorrow at 9" and "weekly report on friday" stay
# one-offs with their text intact.
_REPEAT_WORD = r"(?:daily|nightly|weekly|fortnightly|monthly|yearly|annually|hourly)"
_CLOCKISH = r"\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?"
_REPEAT_WORD_USE = re.compile(
    rf"\b{_REPEAT_WORD}\s+{_AT}\d|\b{_CLOCKISH}\s+{_REPEAT_WORD}\b|\b{_REPEAT_WORD}$",
    re.IGNORECASE,
)

# Timing words left in the task text after a match: the user said more about
# *when* than the grammar consumed, so whatever we set would be wrong -- for a
# repeat, wrong forever. "every monday at 9 until june" must not repeat
# forever with the text "until june", and "every morning at 7 run" must not
# fire at 09:00. A clock time is refused after any match. A second time-like
# "every" (``_REPEAT_HINT``) and qualifiers ("until", "except", "for a week")
# are refused only after a repeat, since "practice piano for 20 minutes
# tomorrow at 9" is a perfectly good one-off.
_LEFTOVER_CLOCK = re.compile(
    rf"(?:^|\s){_AT}\d|\b\d{{1,2}}(?:[:.]\d{{2}})?\s*(?:am|pm|a\.m\.|p\.m\.)(?![a-z])",
    re.IGNORECASE,
)
_LEFTOVER_QUALIFIER = re.compile(
    rf"\b(?:until|till|til|except|excluding|apart\s+from|starting|from\s+now)\b"
    rf"|\bfor\s+(?:\d+|an?|a\s+few|a\s+couple\s+of|the\s+next)\s+(?:{_UNIT_ALT}){_UNIT_END}"
    rf"|(?:^|\s)and\s+{_CLOCKISH}(?![a-z])",
    re.IGNORECASE,
)


def _cant_repeat(hint: str = "") -> RepeatError:
    if hint:
        return RepeatError(f"I can't repeat that yet -- {hint}")
    return RepeatError(f"I can't repeat that yet -- try {REPEAT_EXAMPLES}")


@dataclass(frozen=True, slots=True)
class _Repeat:
    freq: str
    by: str = ""
    """BY* rule parts other than the time of day."""
    interval: int = 1
    hourly: bool = False


def _interval(m: re.Match[str]) -> int:
    raw = (m.groupdict().get("n") or "1").lower()
    n = 2 if raw == "other" else int(raw)
    if n < 1:
        raise _cant_repeat(f"every {n}? Try {REPEAT_EXAMPLES}")
    return n


def _byday(raw: str) -> str:
    names = re.split(_DAY_SEP, raw.strip(), flags=re.IGNORECASE)
    days = sorted({_WEEKDAYS[name.lower()] for name in names})
    return "BYDAY=" + ",".join(_BYDAY[d] for d in days)


def _build_repeat_patterns() -> list[tuple[str, object]]:
    def h_days(m: re.Match[str]) -> _Repeat:
        return _Repeat("DAILY", interval=_interval(m))

    def h_daypart(m: re.Match[str]) -> _Repeat:
        return _Repeat("DAILY")

    def h_kind(m: re.Match[str]) -> _Repeat:
        days = "MO,TU,WE,TH,FR" if m.group("kind").lower() == "weekday" else "SA,SU"
        return _Repeat("WEEKLY", by=f"BYDAY={days}")

    def h_weekdays(m: re.Match[str]) -> _Repeat:
        return _Repeat("WEEKLY", by=_byday(m.group("days")))

    def h_weeks(m: re.Match[str]) -> _Repeat:
        if not m.group("days"):
            raise _cant_repeat("which day? Try “every week on friday”")
        return _Repeat("WEEKLY", by=_byday(m.group("days")), interval=_interval(m))

    def h_months(m: re.Match[str]) -> _Repeat:
        if not m.group("mday"):
            raise _cant_repeat("which day of the month? Try “every month on the 25th”")
        day = int(m.group("mday"))
        if not 1 <= day <= 31:
            raise TimeParseError(f"no month has a day {day}")
        return _Repeat("MONTHLY", by=clamped_month_days(day), interval=_interval(m))

    def h_years(m: re.Match[str]) -> _Repeat:
        month_name = m.group("month") or m.group("month2")
        if not month_name:
            raise _cant_repeat("which date? Try “every year on 3 sep”")
        month = _MONTHS[month_name.lower()]
        day = int(m.group("day") or m.group("day2"))
        # 2024 is a leap year, so 29 Feb counts as a real date here.
        if not 1 <= day <= calendar.monthrange(2024, month)[1]:
            raise TimeParseError(f"{month_name} {day} is not a real date")
        by = "BYMONTHDAY=28,29;BYSETPOS=-1" if (month, day) == (2, 29) else f"BYMONTHDAY={day}"
        return _Repeat("YEARLY", by=f"BYMONTH={month};{by}", interval=_interval(m))

    def h_hours(m: re.Match[str]) -> _Repeat:
        return _Repeat("HOURLY", interval=_interval(m), hourly=True)

    def h_too_often(m: re.Match[str]) -> _Repeat:
        raise _cant_repeat("I don't repeat more often than every hour")

    every = rf"{_REPEAT_LEAD}every\s+"
    trail = _REPEAT_TRAIL
    on_date = (
        rf"(?:(?P<month>{_MONTH_ALT})\.?\s+(?P<day>\d{{1,2}}){_ORD}"
        rf"|(?P<day2>\d{{1,2}}){_ORD}\s+(?:of\s+)?(?P<month2>{_MONTH_ALT})\.?)"
    )
    return [
        (rf"{every}{_EVERY_N}days?{_UNIT_END}{trail}", h_days),
        (rf"{every}{_DAYPART}", h_daypart),
        (rf"{every}(?P<kind>weekday|weekend)s?{_UNIT_END}{trail}", h_kind),
        (rf"{every}{_WEEKDAY_LIST}{trail}", h_weekdays),
        (rf"{every}{_EVERY_N}weeks?{_UNIT_END}(?:\s+on\s+{_WEEKDAY_LIST})?{trail}", h_weeks),
        (
            rf"{every}{_EVERY_N}months?{_UNIT_END}"
            rf"(?:\s+on\s+(?:the\s+)?(?P<mday>\d{{1,2}}){_ORD})?{trail}",
            h_months,
        ),
        (rf"{every}{_EVERY_N}years?{_UNIT_END}(?:\s+on\s+{on_date})?{trail}", h_years),
        (rf"{every}(?:(?P<n>\d+)\s+)?(?:hours?|hrs?){_UNIT_END}", h_hours),
        (rf"{every}(?:\d+\s+)?(?:minutes?|mins?|seconds?|secs?){_UNIT_END}", h_too_often),
    ]


_REPEAT_PATTERNS = _build_repeat_patterns()


def _repeat_clock(m: re.Match[str]) -> tuple[time | None, bool]:
    groups = m.groupdict()
    lead = groups.get("lhour")
    trail = groups.get("hour") or groups.get("daypart")
    if lead and trail:
        raise TimeParseError("that has two times in it -- give it one")
    if lead:
        return _clock_from_match(m, "l")
    if groups.get("daypart"):
        return _daypart_time(groups["daypart"].lower()), False
    if groups.get("hour"):
        return _clock_from_match(m)
    return None, False


def _resolve_repeat(
    m: re.Match[str], rep: _Repeat, now_utc: datetime, tz: ZoneInfo
) -> tuple[Series, list[str]]:
    clock, ambiguous = _repeat_clock(m)
    if rep.hourly and clock is not None:
        raise _cant_repeat("an hourly repeat starts from now, so it takes no time")
    series = build_series(
        freq=rep.freq, by=rep.by, interval=rep.interval, at=clock, now_utc=now_utc, tz=tz
    )
    warnings = []
    if ambiguous and clock is not None:
        warnings.append(f"read “{clock.hour}” as {clock:%H:%M} -- add am or pm to change it")
    return series, warnings


def build_series(
    *,
    freq: str,
    by: str = "",
    interval: int = 1,
    at: time | None = None,
    now_utc: datetime,
    tz: ZoneInfo,
) -> Series:
    """The series stored for a repeat rule, first occurrence included.

    Public so that anything else that understands a repeat -- the optional AI
    parser -- stores exactly what the grammar would for the same rule.
    ``by`` holds BY* parts other than the time of day; ``at`` is the local
    time of day (``DEFAULT_TIME`` when omitted). ``HOURLY`` ignores ``at``.
    """
    interval_part = f";INTERVAL={interval}" if interval > 1 else ""

    if freq == "HOURLY":
        start = now_utc.astimezone(UTC).replace(tzinfo=None, second=0, microsecond=0)
        start += timedelta(hours=interval)
        return Series(f"FREQ=HOURLY{interval_part}", start, ZoneInfo("UTC"), utc_timeline=True)

    # A repeat always names its days, so a bare "9" is read as written, the
    # same as "tomorrow at 9" -- not "whichever is sooner", which would make a
    # series' time depend on the moment it happened to be set.
    at = at or DEFAULT_TIME
    parts = (by, f"BYHOUR={at.hour};BYMINUTE={at.minute};BYSECOND=0")
    rule_by = ";".join(p for p in parts if p)

    # The first occurrence is the soonest match of the rule *without* its
    # interval: "every 2 weeks on friday" said on a Saturday starts the coming
    # Friday, not the one after. It then anchors the interval's phase.
    now_local = now_utc.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0)
    first = Series(f"FREQ={freq};{rule_by}", now_local, tz).next_local_after(now_utc)
    if first is None:  # pragma: no cover -- every rule built here recurs
        raise _cant_repeat()
    return Series(f"FREQ={freq}{interval_part};{rule_by}", first, tz)


def _anchored(patterns: list[tuple[str, object]]) -> list[tuple[re.Pattern[str], object, object]]:
    """Each pattern anchored at the tail, then each at the head.

    Tail first: "call mom tomorrow at 9" is the most natural phrasing, and
    matching the tail cannot swallow leading task text.
    """
    out: list[tuple[re.Pattern[str], object, object]] = []
    for anchored, take_remainder in (
        (r"(?:^|\s+){pattern}\s*$", _remainder_before),
        # A separator may end a head time: "tomorrow at 9, call mom" and
        # "every monday at 7pm: bins out". Without it, the regex backtracks to
        # "tomorrow" alone and leaves "at 9," in the task text.
        (r"^{pattern}(?:\s*[,;:\-–—]\s*|\s+|$)", _remainder_after),  # noqa: RUF001
    ):
        for pattern, handler in patterns:
            regex = re.compile(anchored.format(pattern=pattern), re.IGNORECASE)
            out.append((regex, handler, take_remainder))
    return out


_NOT_A_REMINDER = "that is a time but not a reminder -- tell me what to remind you about"


def parse(text: str, *, now_utc: datetime, tz: ZoneInfo) -> ParsedReminder:
    """Parse a capture message into a reminder.

    Raises :class:`TimeParseError` when no time expression is recognised, or
    when one is recognised but resolves to the past; :class:`RepeatError` when
    the message asks for a repeat outside the supported set.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    now_local = now_utc.astimezone(tz)

    body = _strip_lead_ins(text)
    if not body:
        raise TimeParseError("there is nothing here to remind you about")

    for regex, handler, take_remainder in _ANCHORED_REPEATS:
        m = regex.search(body)
        if m is None:
            continue
        repeat = handler(m)  # type: ignore[operator]
        task = take_remainder(body, m)  # type: ignore[operator]
        if not task:
            raise TimeParseError(_NOT_A_REMINDER)
        leftover = (
            _LEFTOVER_CLOCK.search(task)
            or _LEFTOVER_QUALIFIER.search(task)
            or _REPEAT_HINT.search(task)
        )
        if leftover:
            raise _cant_repeat(
                f"I don't understand “{leftover.group(0).strip()}” in a repeat. "
                f"Put one time right after the repeat, like “every monday at 7pm bins out”"
            )
        _check_dangling(task)
        series, warnings = _resolve_repeat(m, repeat, now_utc, tz)
        return ParsedReminder(
            text=task,
            due_at=series.first,
            matched_phrase=m.group(0).strip(),
            warnings=warnings,
            recurrence=series.serialize(),
        )

    if _REPEAT_HINT.search(body) or _REPEAT_WORD_USE.search(body):
        raise _cant_repeat()

    for regex, handler, take_remainder in _ANCHORED_ONE_OFFS:
        m = regex.search(body)
        if m is None:
            continue
        resolution = handler(m, now_local)  # type: ignore[operator]
        task = take_remainder(body, m)  # type: ignore[operator]
        if not task:
            raise TimeParseError(_NOT_A_REMINDER)
        if _REPEAT_WORD_USE.search(task):
            raise _cant_repeat()
        leftover = _LEFTOVER_CLOCK.search(task)
        if leftover:
            raise TimeParseError(
                f"that has more than one time in it (“{leftover.group(0).strip()}”) -- give it one"
            )
        _check_dangling(task)
        due_utc = resolution.when.astimezone(UTC)
        if due_utc <= now_utc:
            raise TimeParseError(f"{resolution.when:%Y-%m-%d %H:%M} is in the past")
        return ParsedReminder(
            text=task,
            due_at=due_utc,
            matched_phrase=m.group(0).strip(),
            warnings=list(resolution.warnings),
        )

    raise TimeParseError("I could not find a time in that")


def _remainder_before(body: str, m: re.Match[str]) -> str:
    return _tidy(body[: m.start()])


def _remainder_after(body: str, m: re.Match[str]) -> str:
    rest = body[m.end() :]
    rest = re.sub(
        r"^\s*(?:to|that\s+i|i\s+should|please)\b", "", rest, count=1, flags=re.IGNORECASE
    )
    return _tidy(rest)


_DANGLING = re.compile(r"(?:^|\s)(?:and|or|&|\+)$|^(?:and|or|&|\+)(?:\s|$)", re.IGNORECASE)
"""A task that starts or ends on a joining word: the time expression was only
part of what was asked ("pills twice a week, mon and | thu at 8")."""


def _check_dangling(task: str) -> None:
    if _DANGLING.search(task):
        raise TimeParseError(
            "I could only read part of the time in that -- give it one time, like "
            "“call mom tomorrow at 9”"
        )


def _tidy(s: str) -> str:
    # The en and em dashes are intentional -- people type them as separators.
    s = s.strip().strip(",;:-–—")  # noqa: RUF001
    s = re.sub(r"\s+", " ", s).strip()
    # "remind me to" may have sat after the time expression.
    return _strip_lead_ins(s)


_ANCHORED_REPEATS = _anchored(_REPEAT_PATTERNS)
_ANCHORED_ONE_OFFS = _anchored(_PATTERNS)
