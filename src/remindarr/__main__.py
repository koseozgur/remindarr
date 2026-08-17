"""Entry point: ``remindarr`` or ``python -m remindarr``."""

from __future__ import annotations

import asyncio
import sys

from remindarr import logging as log_setup
from remindarr.app import NoAdaptersConfigured, run
from remindarr.config import Settings


def main() -> int:
    settings = Settings()
    log_setup.configure(level=settings.log_level, json=settings.log_json)
    try:
        asyncio.run(run(settings))
    except NoAdaptersConfigured as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
