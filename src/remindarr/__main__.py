"""Entry point: ``remindarr`` or ``python -m remindarr``."""

from __future__ import annotations

import asyncio
import sys

from pydantic import ValidationError

from remindarr import logging as log_setup
from remindarr.app import NoAdaptersConfigured, run
from remindarr.config import Settings


def main() -> int:
    try:
        settings = Settings()
    except ValidationError as exc:
        # Only loc + msg: str(exc) includes each error's input value, which for
        # a cross-field check is the whole input -- secrets and all.
        print("error: invalid configuration", file=sys.stderr)
        for err in exc.errors():
            where = ".".join(str(part) for part in err["loc"]) or "settings"
            print(f"  {where}: {err['msg']}", file=sys.stderr)
        return 2
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
