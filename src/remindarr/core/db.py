"""Engine and session wiring.

No Alembic in v0. The schema is young enough that a bad migration costs more
than a rebuild, and this project's stated preference is a clean rebuild over an
intricate state-preserving workaround. When the schema stabilises and there is
real data worth keeping, that is the moment to add migrations -- not before.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


def create_engine(database_url: str) -> AsyncEngine:
    if database_url.startswith("sqlite"):
        _ensure_sqlite_parent_dir(database_url)
    return create_async_engine(database_url, echo=False)


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """Create the directory for a file-backed SQLite database.

    Without this, a fresh container with a bind-mounted empty volume fails on
    first write with an opaque "unable to open database file".
    """
    _, _, path_part = database_url.partition(":///")
    if not path_part or path_part == ":memory:":
        return
    Path(path_part).expanduser().parent.mkdir(parents=True, exist_ok=True)


async def init_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            # WAL lets the scheduler read while an adapter writes, instead of
            # the two serialising on a single writer lock.
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.run_sync(Base.metadata.create_all)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A session that commits on success and rolls back on any exception."""
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
