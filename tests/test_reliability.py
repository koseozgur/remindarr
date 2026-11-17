"""Backups and the health check."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.adapters.base import DeliveryRegistry
from remindarr.app import build_backups
from remindarr.config import Settings
from remindarr.core.backup import Backups, list_backups, make_backup, sqlite_path, taken_at
from remindarr.core.health import STARTUP_GRACE, Health
from remindarr.core.scheduler import Scheduler

from .conftest import NOW, MutableClock

Client = TestClient[web.Request, web.Application]


def make_db(path: Path, rows: int = 3) -> None:
    """A WAL-mode database with rows still in the WAL, like the live one."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    # Left open on purpose: the rows stay in the WAL, not the main file.
    _open_connections.append(conn)


_open_connections: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _close_connections() -> object:
    yield
    while _open_connections:
        _open_connections.pop().close()


def count_rows(path: Path) -> int:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return int(conn.execute("SELECT count(*) FROM t").fetchone()[0])


# --- sqlite_path ---------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite+aiosqlite:///data/remindarr.db", Path("data/remindarr.db")),
        ("sqlite+aiosqlite:////data/remindarr.db", Path("/data/remindarr.db")),
        ("sqlite+aiosqlite://", None),
        ("sqlite+aiosqlite:///:memory:", None),
        ("postgresql+asyncpg://u@h/db", None),
    ],
)
def test_sqlite_path(url: str, expected: Path | None) -> None:
    assert sqlite_path(url) == expected


# --- make_backup ---------------------------------------------------------


def test_backup_copies_rows_still_in_the_wal(tmp_path: Path) -> None:
    db = tmp_path / "remindarr.db"
    make_db(db, rows=5)
    out = make_backup(db, tmp_path / "backups", now=NOW, keep=3)
    assert out.name == "remindarr-20260818T013000Z.db"
    assert taken_at(out) == NOW
    assert count_rows(out) == 5
    assert not list((tmp_path / "backups").glob("*.part"))


def test_backup_prunes_to_keep(tmp_path: Path) -> None:
    db = tmp_path / "remindarr.db"
    make_db(db)
    backups = tmp_path / "backups"
    for day in range(5):
        make_backup(db, backups, now=NOW + timedelta(days=day), keep=3)
    kept = list_backups(backups)
    assert [taken_at(p) for p in kept] == [NOW + timedelta(days=d) for d in (2, 3, 4)]


def test_backup_of_a_missing_database_fails_without_creating_it(tmp_path: Path) -> None:
    db = tmp_path / "nope.db"
    with pytest.raises(sqlite3.OperationalError):
        make_backup(db, tmp_path / "backups", now=NOW, keep=3)
    assert not db.exists()
    assert list_backups(tmp_path / "backups") == []
    assert not list((tmp_path / "backups").iterdir())


def test_list_backups_ignores_other_files(tmp_path: Path) -> None:
    (tmp_path / "remindarr-20260818T013000Z.db.part").touch()
    (tmp_path / "notes.txt").touch()
    (tmp_path / "remindarr-20260817T013000Z.db").touch()
    assert [p.name for p in list_backups(tmp_path)] == ["remindarr-20260817T013000Z.db"]
    assert list_backups(tmp_path / "missing") == []


# --- Backups loop --------------------------------------------------------


def backups_for(tmp_path: Path, clock: MutableClock) -> Backups:
    db = tmp_path / "remindarr.db"
    if not db.exists():
        make_db(db)
    return Backups(
        db_path=db,
        backup_dir=tmp_path / "backups",
        interval=timedelta(hours=24),
        keep=14,
        clock=clock,
    )


async def test_backs_up_at_start_then_only_when_due(tmp_path: Path) -> None:
    clock = MutableClock()
    b = backups_for(tmp_path, clock)
    assert await b.run_once() is not None
    assert b.last_success == NOW

    clock.advance(timedelta(hours=23))
    assert await b.run_once() is None  # restart within the day: no extra copy

    clock.advance(timedelta(hours=1))
    assert await b.run_once() is not None
    assert len(list_backups(b.backup_dir)) == 2


async def test_failed_backup_is_recorded_and_retried(tmp_path: Path) -> None:
    clock = MutableClock()
    b = backups_for(tmp_path, clock)
    b.db_path = tmp_path / "gone.db"
    assert await b.run_once() is None
    assert b.last_error is not None and "OperationalError" in b.last_error

    b.db_path = tmp_path / "remindarr.db"
    assert await b.run_once() is not None
    assert b.last_error is None


async def test_run_loop_stops(tmp_path: Path) -> None:
    b = backups_for(tmp_path, MutableClock())
    b.check_every = 0.01
    task = asyncio.create_task(b.run())
    await asyncio.sleep(0.05)
    b.stop()
    await asyncio.wait_for(task, timeout=1)
    assert len(list_backups(b.backup_dir)) == 1


def test_build_backups_defaults_next_to_the_database(settings: Settings, tmp_path: Path) -> None:
    assert build_backups(settings) is None  # in-memory in tests

    settings.database_url = f"sqlite+aiosqlite:///{tmp_path}/remindarr.db"
    b = build_backups(settings)
    assert b is not None
    assert b.backup_dir == tmp_path / "backups"
    assert (b.keep, b.interval) == (14, timedelta(hours=24))

    settings.backup_dir = tmp_path / "elsewhere"
    b = build_backups(settings)
    assert b is not None and b.backup_dir == tmp_path / "elsewhere"


# --- Scheduler.last_tick -------------------------------------------------


class FlakyScheduler(Scheduler):
    broken = False

    async def tick(self) -> int:
        if self.broken:
            raise RuntimeError("database is gone")
        return await super().tick()


async def test_last_tick_only_records_successful_ticks(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
) -> None:
    clock = MutableClock()
    scheduler = FlakyScheduler(
        settings=settings, session_factory=session_factory, registry=registry, clock=clock
    )
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    assert scheduler.last_tick == NOW

    scheduler.broken = True
    await asyncio.sleep(0.05)  # let a tick already in flight finish first
    clock.advance(timedelta(minutes=5))
    await asyncio.sleep(0.05)
    scheduler.stop()
    await asyncio.wait_for(task, timeout=1)
    assert scheduler.last_tick == NOW


# --- Health --------------------------------------------------------------


def health_for(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
    clock: MutableClock,
    backups: Backups | None = None,
) -> Health:
    scheduler = Scheduler(
        settings=settings, session_factory=session_factory, registry=registry, clock=clock
    )
    return Health(
        scheduler=scheduler,
        backups=backups,
        tick_seconds=15,
        started_at=clock(),
        clock=clock,
    )


async def test_health_startup_grace_then_needs_a_tick(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
) -> None:
    clock = MutableClock()
    h = health_for(settings, session_factory, registry, clock)
    assert h.check()[0]

    clock.advance(STARTUP_GRACE + timedelta(seconds=1))
    ok, body = h.check()
    assert not ok
    assert body["problems"] == ["scheduler has not ticked since start"]


async def test_health_stale_tick(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
) -> None:
    clock = MutableClock()
    h = health_for(settings, session_factory, registry, clock)
    h.scheduler.last_tick = clock()
    clock.advance(timedelta(seconds=59))
    assert h.check() == (True, {"ok": True, "scheduler_age_s": 59})

    clock.advance(timedelta(seconds=2))
    ok, body = h.check()
    assert not ok and body["problems"] == ["scheduler is not ticking"]


async def test_health_overdue_backups(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    backups = backups_for(tmp_path, clock)
    h = health_for(settings, session_factory, registry, clock, backups)

    clock.advance(timedelta(hours=1, seconds=1))
    h.scheduler.last_tick = clock()
    ok, body = h.check()
    assert not ok and body["problems"] == ["no backup yet"]

    await backups.run_once()
    ok, body = h.check()
    assert ok and body["backup_age_s"] == 0

    # One missed nightly run is tolerated; the second isn't.
    clock.advance(timedelta(hours=49))
    h.scheduler.last_tick = clock()
    assert h.check()[0]
    clock.advance(timedelta(hours=1))
    h.scheduler.last_tick = clock()
    ok, body = h.check()
    assert not ok and body["problems"] == ["backups are overdue"]


async def test_health_endpoint(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: DeliveryRegistry,
) -> None:
    clock = MutableClock()
    h = health_for(settings, session_factory, registry, clock)
    h.scheduler.last_tick = clock()
    client: Client = TestClient(TestServer(h.app()))
    await client.start_server()
    try:
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "scheduler_age_s": 0}

        clock.advance(timedelta(minutes=10))
        resp = await client.get("/healthz")
        assert resp.status == 503
        assert (await resp.json())["ok"] is False

        assert (await client.get("/")).status == 404
    finally:
        await client.close()


def test_health_settings() -> None:
    s = Settings(_env_file=None, health_port=8081, backup_keep=7)
    assert (s.health_port, s.health_host, s.backup_keep) == (8081, "0.0.0.0", 7)
    assert Settings(_env_file=None).health_port is None
