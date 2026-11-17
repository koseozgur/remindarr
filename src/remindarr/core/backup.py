"""Nightly copies of the SQLite database.

Uses SQLite's own online-backup API, which copies a consistent snapshot while
the bot keeps running and writing; copying the file would race the WAL. Each
copy is integrity-checked before it gets its final name, so a half-written or
corrupt backup never sits among the good ones.

Copies land next to the database by default (``<db dir>/backups``). That
guards against a bad deploy or a mistaken ``/remove``, not against losing the
disk -- getting copies off the machine is the deployment's job (see
``docs/deploy-gcp.md``), and it can simply pick up the newest file here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from .clock import utcnow

log = structlog.get_logger(__name__)

PREFIX = "remindarr-"
SUFFIX = ".db"
STAMP = "%Y%m%dT%H%M%SZ"


def sqlite_path(database_url: str) -> Path | None:
    """The database file behind a ``sqlite+aiosqlite:///…`` URL, or None for
    anything else (another engine, or in-memory)."""
    if not database_url.startswith("sqlite"):
        return None
    _, _, path = database_url.partition(":///")
    if not path or path == ":memory:":
        return None
    return Path(path).expanduser()


def list_backups(backup_dir: Path) -> list[Path]:
    """Finished backups, oldest first. Names sort by time."""
    if not backup_dir.is_dir():
        return []
    return sorted(
        p for p in backup_dir.iterdir() if p.name.startswith(PREFIX) and p.suffix == SUFFIX
    )


def taken_at(path: Path) -> datetime:
    """The time encoded in a backup's name (UTC)."""
    stamp = path.name.removeprefix(PREFIX).removesuffix(SUFFIX)
    return datetime.strptime(stamp, STAMP).replace(tzinfo=UTC)


def make_backup(db_path: Path, backup_dir: Path, *, now: datetime, keep: int) -> Path:
    """Copy ``db_path`` to a new, verified backup and prune old ones."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    final = backup_dir / f"{PREFIX}{now.astimezone(UTC):{STAMP}}{SUFFIX}"
    partial = final.with_name(final.name + ".part")
    partial.unlink(missing_ok=True)

    # Read-only, so a wrong path fails loudly instead of backing up a new,
    # empty database.
    source = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    with contextlib.closing(source) as src, contextlib.closing(sqlite3.connect(partial)) as dst:
        src.backup(dst)
    with contextlib.closing(sqlite3.connect(partial)) as check:
        result = check.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed its integrity check: {result!r}")
    os.replace(partial, final)

    for old in list_backups(backup_dir)[:-keep]:
        old.unlink(missing_ok=True)
    return final


@dataclass(slots=True)
class Backups:
    """Runs alongside the scheduler: a backup now if none is recent, then one
    every ``interval``. Checks hourly rather than sleeping for a day, so a
    restart never pushes the next backup a whole interval away."""

    db_path: Path
    backup_dir: Path
    interval: timedelta
    keep: int
    clock: Callable[[], datetime] = utcnow
    check_every: float = 3600.0
    last_success: datetime | None = None
    last_error: str | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    def newest(self) -> datetime | None:
        """When the newest backup on disk was taken, from its name."""
        backups = list_backups(self.backup_dir)
        return taken_at(backups[-1]) if backups else None

    async def run_once(self) -> Path | None:
        now = self.clock()
        newest = self.newest()
        if newest is not None and now - newest < self.interval:
            return None
        try:
            path = await asyncio.to_thread(
                make_backup, self.db_path, self.backup_dir, now=now, keep=self.keep
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("backup.failed")
            return None
        self.last_success, self.last_error = now, None
        log.info("backup.done", path=str(path), size=path.stat().st_size)
        return path

    async def run(self) -> None:
        hours = self.interval / timedelta(hours=1)
        log.info("backup.start", dir=str(self.backup_dir), every_hours=hours)
        while not self._stop.is_set():
            await self.run_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.check_every)

    def stop(self) -> None:
        self._stop.set()
