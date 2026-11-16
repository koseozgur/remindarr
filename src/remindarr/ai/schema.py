"""What a model must answer with. Provider-neutral: any reader returns this.

Every field is required (nullable where absent), which keeps the JSON schema
valid for structured outputs and leaves nothing to a model's guess about
omitted fields.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Repeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit: Literal["hour", "day", "week", "month", "year"]
    every: int
    """1 = every unit, 2 = every other, and so on."""
    weekdays: list[Weekday]
    """``week`` only (at least one); empty otherwise."""
    month_day: int | None
    """``month`` and ``year``: the day of the month, 1-31."""
    month: int | None
    """``year`` only: 1-12."""


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["reminder", "not_a_reminder", "unsupported"]
    task: str
    """What to be reminded about, in the sender's own words and language, with
    the time and repeat words taken out."""
    date: str | None
    """One-off: local date of the reminder, ``YYYY-MM-DD``."""
    time: str | None
    """Local 24-hour ``HH:MM``, or null when no time of day was given."""
    repeat: Repeat | None
    reason: str
    """Short and plain, for ``not_a_reminder`` and ``unsupported``; else empty."""
