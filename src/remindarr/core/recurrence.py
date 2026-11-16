"""Recurring reminders: the stored rule and how it expands.

A series is stored in ``Reminder.recurrence`` as a two-line RFC 5545 fragment::

    DTSTART;TZID=America/New_York:20261118T080000
    RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0;BYSECOND=0

Expansion is ``dateutil.rrule``; this module only adds the two things it does
not do for us:

* **Wall-clock time across DST.** Day-or-coarser rules expand on the *local*
  naive timeline of the zone they were set in, then convert to UTC, so "every
  day at 08:00" stays at 08:00 local when the offset changes. A local time that
  does not exist that day (the spring-forward gap) resolves with ``fold=0``,
  i.e. an hour later on the wall clock; a repeated one (fall-back) fires once,
  on its first pass. Hourly rules expand on the UTC timeline instead, so "every
  hour" is always 60 minutes apart and never doubles up or skips at a change.
* **The zone travels with the rule.** A series keeps the timezone it was set
  in, even if the subscriber later runs ``/tz``: "take pills at 08:00" set in
  Tokyo keeps meaning 08:00 Tokyo. A new series picks up the new zone.

``DTSTART`` is the series' first occurrence and never moves. It anchors the
phase of interval rules ("every 2 weeks" counts from it), and dateutil iterates
from it, which is cheap at the granularities allowed here (hourly at most).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dateutil.rrule import rrule, rrulestr

_DTSTART_RE = re.compile(r"^DTSTART(?:;TZID=(?P<tzid>[^:]+))?:(?P<value>\d{8}T\d{6})(?P<z>Z?)$")
_STAMP = "%Y%m%dT%H%M%S"

_DAY_NAMES = {
    "MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun",
}  # fmt: skip
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR")
_WEEKEND = ("SA", "SU")
_MONTH_NAMES = (
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip


class RecurrenceError(ValueError):
    """A stored recurrence string that cannot be read back."""


@dataclass(frozen=True, slots=True)
class Series:
    rule: str
    """The RRULE value, without the ``RRULE:`` prefix."""

    dtstart: datetime
    """Naive: local wall-clock time in ``tz``, or UTC when ``utc_timeline``."""

    tz: ZoneInfo
    utc_timeline: bool = False
    """Expand on the UTC timeline (hourly rules) rather than local wall clock."""

    # -- storage ------------------------------------------------------------

    def serialize(self) -> str:
        if self.utc_timeline:
            start = f"DTSTART:{self.dtstart:{_STAMP}}Z"
        else:
            start = f"DTSTART;TZID={self.tz.key}:{self.dtstart:{_STAMP}}"
        return f"{start}\nRRULE:{self.rule}"

    @classmethod
    def parse(cls, stored: str) -> Series:
        lines = stored.strip().splitlines()
        if len(lines) != 2 or not lines[1].startswith("RRULE:"):
            raise RecurrenceError(f"not a DTSTART+RRULE pair: {stored!r}")
        m = _DTSTART_RE.match(lines[0].strip())
        if m is None:
            raise RecurrenceError(f"unreadable DTSTART: {lines[0]!r}")
        try:
            dtstart = datetime.strptime(m.group("value"), _STAMP)
            utc = bool(m.group("z"))
            tz = ZoneInfo("UTC") if utc else ZoneInfo(m.group("tzid") or "UTC")
        except (ValueError, KeyError) as exc:  # bad stamp, unknown zone
            raise RecurrenceError(str(exc)) from exc
        rule = lines[1].removeprefix("RRULE:").strip()
        series = cls(rule=rule, dtstart=dtstart, tz=tz, utc_timeline=utc)
        series._rrule()  # validate now, not at the first delivery
        return series

    # -- expansion ----------------------------------------------------------

    def _rrule(self) -> rrule:
        try:
            parsed = rrulestr(self.rule, dtstart=self.dtstart, cache=False)
        except (ValueError, TypeError) as exc:
            raise RecurrenceError(f"unreadable RRULE {self.rule!r}: {exc}") from exc
        if not isinstance(parsed, rrule):
            raise RecurrenceError(f"expected a single RRULE, got {self.rule!r}")
        return parsed

    def _to_timeline(self, when_utc: datetime) -> datetime:
        if self.utc_timeline:
            return when_utc.astimezone(UTC).replace(tzinfo=None)
        return when_utc.astimezone(self.tz).replace(tzinfo=None)

    def _to_utc(self, naive: datetime) -> datetime:
        zone = UTC if self.utc_timeline else self.tz
        return naive.replace(tzinfo=zone).astimezone(UTC)

    @property
    def first(self) -> datetime:
        """The first occurrence, as aware UTC."""
        return self._to_utc(self.dtstart)

    def next_after(self, when_utc: datetime) -> datetime | None:
        """The first occurrence strictly after ``when_utc``, as aware UTC.

        ``None`` only for a rule with an end (COUNT/UNTIL), which the grammar
        never produces -- but a hand-written one might.
        """
        candidate = self.next_local_after(when_utc)
        return None if candidate is None else self._to_utc(candidate)

    def next_local_after(self, when_utc: datetime) -> datetime | None:
        """As :meth:`next_after`, but the naive occurrence on the rule's timeline.

        Kept naive so a time in the spring-forward gap stays what the rule
        says (02:30), rather than round-tripping through UTC into 03:30.
        """
        rule = self._rrule()
        candidate = rule.after(self._to_timeline(when_utc), inc=False)
        # The naive local timeline repeats an hour at fall-back, so a naive
        # "after" can land on a wall-clock time that is already past in UTC.
        while candidate is not None and self._to_utc(candidate) <= when_utc:
            candidate = rule.after(candidate, inc=False)
        return candidate

    def latest_at_or_before(self, when_utc: datetime) -> datetime | None:
        """The most recent occurrence at or before ``when_utc``, as aware UTC."""
        rule = self._rrule()
        candidate = rule.before(self._to_timeline(when_utc), inc=True)
        while candidate is not None and self._to_utc(candidate) > when_utc:
            candidate = rule.before(candidate, inc=False)
        return None if candidate is None else self._to_utc(candidate)

    # -- humans -------------------------------------------------------------

    def describe(self) -> str:
        """ "every weekday at 08:00", "every 2 weeks on Fri at 09:00", ..."""
        parts = dict(p.split("=", 1) for p in self.rule.split(";") if "=" in p)
        freq = parts.get("FREQ", "")
        n = int(parts.get("INTERVAL", "1"))
        at = ""
        if "BYHOUR" in parts:
            at = f" at {int(parts['BYHOUR']):02d}:{int(parts.get('BYMINUTE', '0')):02d}"

        def every(unit: str) -> str:
            return f"every {unit}" if n == 1 else f"every {n} {unit}s"

        if freq == "HOURLY":
            return every("hour")
        if freq == "DAILY":
            return every("day") + at
        if freq == "WEEKLY":
            days = tuple(parts.get("BYDAY", "").split(","))
            if days == _WEEKDAYS and n == 1:
                return "every weekday" + at
            if days == _WEEKEND and n == 1:
                return "every weekend" + at
            listed = _join([_DAY_NAMES.get(d, d) for d in days])
            return (f"every {listed}" if n == 1 else f"every {n} weeks on {listed}") + at
        if freq == "MONTHLY":
            return f"{every('month')} on the {_month_day(parts)}{at}"
        if freq == "YEARLY":
            month = _MONTH_NAMES[int(parts.get("BYMONTH", "1"))]
            return f"{every('year')} on {_year_day(parts, month)}{at}"
        return "repeating"  # a hand-written rule outside the grammar


def _days(parts: dict[str, str]) -> list[int]:
    return [int(d) for d in parts.get("BYMONTHDAY", "1").split(",")]


def _month_day(parts: dict[str, str]) -> str:
    day = max(_days(parts))
    text = ordinal(day)
    if "BYSETPOS" in parts:  # the "or the last day" clamp, see clamped_month_days
        text += " (or the last day)"
    return text


def _year_day(parts: dict[str, str], month: str) -> str:
    day = max(_days(parts))
    text = f"{day} {month}"
    if "BYSETPOS" in parts:
        text += f" (or the last day of {month})"
    return text


def clamped_month_days(day: int) -> str:
    """BYMONTHDAY/BYSETPOS for "the Nth, or the month's last day if shorter".

    Plain ``BYMONTHDAY=31`` follows RFC 5545 and *skips* months without a 31st,
    so "every month on the 31st" would go silent through February to April.
    For a reminder ("pay rent on the 31st") the last day is the useful reading.
    ``BYMONTHDAY=28,29,30,31;BYSETPOS=-1`` picks the latest of those that exist.
    """
    if day <= 28:
        return f"BYMONTHDAY={day}"
    return f"BYMONTHDAY={','.join(str(d) for d in range(28, day + 1))};BYSETPOS=-1"


def ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
