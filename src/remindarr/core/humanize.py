"""Rendering times back to humans.

The confirmation message is the only feedback loop the user gets on whether the
parser understood them. It has to state the resolved absolute time, not just
echo their phrasing back -- "ok, tomorrow at 9" confirms nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def format_when(due_utc: datetime, tz: ZoneInfo, *, now_utc: datetime) -> str:
    """A absolute-plus-relative rendering: "tomorrow at 09:00 (in 14 hours)"."""
    local = due_utc.astimezone(tz)
    now_local = now_utc.astimezone(tz)
    days = (local.date() - now_local.date()).days

    if days == 0:
        day_part = "today"
    elif days == 1:
        day_part = "tomorrow"
    elif 2 <= days < 7:
        day_part = local.strftime("%A")
    elif local.year == now_local.year:
        day_part = local.strftime("%a %-d %b")
    else:
        day_part = local.strftime("%a %-d %b %Y")

    return f"{day_part} at {local:%H:%M} ({format_delta(due_utc - now_utc)})"


def format_delta(delta: timedelta) -> str:
    """ "in 3 hours", "in 2 days", "12 minutes ago"."""
    seconds = int(delta.total_seconds())
    past = seconds < 0
    seconds = abs(seconds)

    if seconds < 60:
        text = f"{seconds} second{_s(seconds)}"
    elif seconds < 3600:
        n = seconds // 60
        text = f"{n} minute{_s(n)}"
    elif seconds < 86400:
        n = seconds // 3600
        text = f"{n} hour{_s(n)}"
    elif seconds < 86400 * 14:
        n = seconds // 86400
        text = f"{n} day{_s(n)}"
    else:
        n = seconds // (86400 * 7)
        text = f"{n} week{_s(n)}"

    return f"{text} ago" if past else f"in {text}"


def _s(n: int) -> str:
    return "" if n == 1 else "s"
