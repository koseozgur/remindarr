"""Logging setup.

Console-friendly by default because the first audience is someone watching
``docker logs`` during setup. JSON is a flag away for when this lands next to
the rest of a homelab's log shipping.
"""

from __future__ import annotations

import logging
import sys

import structlog

VOICE_UNSUPPORTED = "voice will NOT be supported"
"""discord.py warns at startup that PyNaCl and davey are missing. remindarr
never uses voice, so the warning is noise -- and a warning that is always
there teaches people to ignore warnings."""


def _not_voice_warning(record: logging.LogRecord) -> bool:
    return VOICE_UNSUPPORTED not in record.getMessage()


def configure(*, level: str = "INFO", json: bool = False) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())

    # These two are chatty at INFO and drown out anything useful.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)
    # Only these two messages; every other discord.client warning still shows.
    discord_client = logging.getLogger("discord.client")
    if _not_voice_warning not in discord_client.filters:
        discord_client.addFilter(_not_voice_warning)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        cache_logger_on_first_use=True,
    )
