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
_CLOCK = r"(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*(?P<ampm>am|pm|a\.m\.|p\.m\.)?"
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


def _clock_from_match(m: re.Match[str]) -> tuple[time, bool]:
    """Return the clock time and whether the 12/24-hour reading was ambiguous."""
    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    ampm = (m.group("ampm") or "").replace(".", "").lower()

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


def parse(text: str, *, now_utc: datetime, tz: ZoneInfo) -> ParsedReminder:
    """Parse a capture message into a reminder.

    Raises :class:`TimeParseError` when no time expression is recognised, or
    when one is recognised but resolves to the past.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    now_local = now_utc.astimezone(tz)

    body = _strip_lead_ins(text)
    if not body:
        raise TimeParseError("there is nothing here to remind you about")

    # Tail first: "call mom tomorrow at 9" is the most natural phrasing, and
    # matching the tail cannot swallow leading task text.
    for anchored, take_remainder in (
        (r"(?:^|\s+){pattern}\s*$", _remainder_before),
        (r"^{pattern}(?:\s+|$)", _remainder_after),
    ):
        for pattern, handler in _PATTERNS:
            regex = re.compile(anchored.format(pattern=pattern), re.IGNORECASE)
            m = regex.search(body)
            if m is None:
                continue
            resolution = handler(m, now_local)  # type: ignore[operator]
            task = take_remainder(body, m)
            if not task:
                raise TimeParseError(
                    "that is a time but not a reminder -- tell me what to remind you about"
                )
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


def _tidy(s: str) -> str:
    # The en and em dashes are intentional -- people type them as separators.
    s = s.strip().strip(",;:-–—")  # noqa: RUF001
    s = re.sub(r"\s+", " ", s).strip()
    # "remind me to" may have sat after the time expression.
    return _strip_lead_ins(s)
