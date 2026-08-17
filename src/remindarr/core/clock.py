"""The single source of "now".

Everything that reasons about time takes ``now`` as an explicit argument, so the
tests never have to patch a clock. This module is only for the edges -- the
places that genuinely have to ask the operating system what time it is.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Timezone-aware current time in UTC.

    Never ``datetime.utcnow()``: that returns a naive datetime that *looks* like
    UTC and silently compares wrong against aware ones.
    """
    return datetime.now(UTC)
