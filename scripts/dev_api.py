"""A local API server for developing the phone app. Touches no real service.

    uv run python scripts/dev_api.py serve      # API on http://127.0.0.1:8787
    uv run python scripts/dev_api.py pair       # print a pairing code
    uv run python scripts/dev_api.py chat TEXT  # say TEXT to the bot as the dev user

* Never reads ``.env`` (which may hold the live tokens) and never starts a chat
  adapter, so nothing reaches LINE or Discord and no LINE push quota is spent.
* Its own database, ``data/dev/api-dev.db`` (git-ignored). Delete it for a
  fresh start.
* One user, ``dev``, as the fake identity ``discord:dev``. "Discord" counts as
  set up so routing works, but a due reminder is printed here instead of sent.

The iOS simulator reaches this as http://localhost:8787.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from remindarr import logging as log_setup
from remindarr.adapters.base import DeliveryRegistry
from remindarr.config import Settings
from remindarr.core import access, devices
from remindarr.core.api import Api, ApiServer
from remindarr.core.clock import utcnow
from remindarr.core.db import create_engine, create_session_factory, init_schema, session_scope
from remindarr.core.directory import bootstrap
from remindarr.core.models import Subscriber
from remindarr.core.router import Router
from remindarr.core.scheduler import Scheduler
from remindarr.core.types import IncomingMessage, OutgoingMessage, Route, Sender

DB = Path(__file__).resolve().parent.parent / "data" / "dev" / "api-dev.db"
DEV_USER_ID = "dev"


def dev_settings(port: int) -> Settings:
    return Settings(
        _env_file=None,  # never the real .env
        database_url=f"sqlite+aiosqlite:///{DB}",
        default_timezone="Asia/Tokyo",
        # Makes "discord" an enabled platform for routing. No Discord adapter
        # is ever built from it; see ConsoleDelivery.
        discord_token="dev-placeholder-never-used",
        admins=f"discord:{DEV_USER_ID}",
        api_host="127.0.0.1",
        api_port=port,
        tick_seconds=2.0,
    )


class ConsoleDelivery:
    """Stands in for Discord: prints what would have been sent."""

    platform = "discord"

    async def send(self, message: OutgoingMessage) -> None:
        print(f"\n[delivered to {message.route}] {message.text}\n", flush=True)


async def _setup(
    settings: Settings,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_engine(settings.database_url)
    await init_schema(engine)
    factory = create_session_factory(engine)
    async with session_scope(factory) as session:
        await bootstrap(session, settings, now=utcnow())
    return engine, factory


async def pair(settings: Settings) -> None:
    engine, factory = await _setup(settings)
    async with session_scope(factory) as session:
        sub = (
            await session.execute(
                select(Subscriber).where(Subscriber.platform_user_id == DEV_USER_ID)
            )
        ).scalar_one()
        assert sub.user_id is not None
        code = await devices.issue_pair_code(session, user_id=sub.user_id, now=utcnow())
    await engine.dispose()
    print(access.display(code))


async def chat(settings: Settings, text: str) -> None:
    engine, factory = await _setup(settings)
    router = Router(settings=settings, session_factory=factory)
    reply = await router.handle(
        IncomingMessage(
            sender=Sender("discord", DEV_USER_ID, "dev"),
            route=Route.direct_to("discord", DEV_USER_ID),
            text=text,
            received_at=utcnow(),
            direct=True,
        )
    )
    await engine.dispose()
    print(reply.text if reply else "(no reply)")


async def serve(settings: Settings) -> None:
    engine, factory = await _setup(settings)
    router = Router(settings=settings, session_factory=factory)
    registry = DeliveryRegistry()
    registry.register(ConsoleDelivery())
    scheduler = Scheduler(settings=settings, session_factory=factory, registry=registry)
    assert settings.api_port is not None
    server = ApiServer(
        Api(router=router, settings=settings, session_factory=factory),
        host=settings.api_host,
        port=settings.api_port,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await server.start()
    task = asyncio.create_task(scheduler.run())
    print(f"dev API on http://{settings.api_host}:{settings.api_port}  (db: {DB})", flush=True)
    print("pairing code: uv run python scripts/dev_api.py pair", flush=True)
    try:
        await stop.wait()
    finally:
        scheduler.stop()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(task, timeout=5)
        await server.stop()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8787)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("pair")
    chat_p = sub.add_parser("chat")
    chat_p.add_argument("text", nargs="+")
    args = parser.parse_args()

    settings = dev_settings(args.port)
    log_setup.configure(level="INFO", json=False)
    if args.cmd == "serve":
        asyncio.run(serve(settings))
    elif args.cmd == "pair":
        asyncio.run(pair(settings))
    else:
        asyncio.run(chat(settings, " ".join(args.text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
