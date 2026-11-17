"""Wiring: build the pieces, start them, shut them down cleanly."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import timedelta

import structlog

from remindarr.adapters.base import CaptureAdapter, DeliveryAdapter, DeliveryRegistry
from remindarr.ai.reading import Reader
from remindarr.config import Settings
from remindarr.core.api import Api, ApiServer
from remindarr.core.backup import Backups, sqlite_path
from remindarr.core.clock import utcnow
from remindarr.core.db import create_engine, create_session_factory, init_schema, session_scope
from remindarr.core.directory import BootstrapResult, bootstrap
from remindarr.core.health import Health, HealthServer
from remindarr.core.router import Router
from remindarr.core.scheduler import Scheduler

log = structlog.get_logger(__name__)


class NoAdaptersConfigured(RuntimeError):
    pass


def build_adapters(settings: Settings) -> list[object]:
    """Instantiate an adapter for every platform that has a token.

    Imports are local so that a missing or broken optional dependency only
    breaks the platform that needs it, rather than preventing startup entirely.
    """
    adapters: list[object] = []

    if settings.telegram_token is not None:
        from remindarr.adapters.telegram import TelegramAdapter

        adapters.append(TelegramAdapter(settings.telegram_token.get_secret_value()))

    if settings.discord_token is not None:
        from remindarr.adapters.discord import DiscordAdapter

        adapters.append(DiscordAdapter(settings.discord_token.get_secret_value()))

    if settings.line_channel_secret is not None and settings.line_channel_access_token is not None:
        from remindarr.adapters.line import LineAdapter

        adapters.append(
            LineAdapter(
                settings.line_channel_secret.get_secret_value(),
                settings.line_channel_access_token.get_secret_value(),
                host=settings.webhook_host,
                port=settings.webhook_port,
                path=settings.line_webhook_path,
            )
        )

    return adapters


def build_reader(settings: Settings) -> Reader | None:
    """The optional AI reader, if a provider key is configured."""
    if settings.anthropic_api_key is None:
        return None
    from remindarr.ai.claude import ClaudeReader

    log.info("ai.enabled", provider="claude", model=settings.ai_model, effort=settings.ai_effort)
    return ClaudeReader(
        settings.anthropic_api_key.get_secret_value(),
        model=settings.ai_model,
        effort=settings.ai_effort,
        timeout=settings.ai_timeout_seconds,
    )


def build_backups(settings: Settings) -> Backups | None:
    """Nightly copies, for a SQLite file database only."""
    db_path = sqlite_path(settings.database_url)
    if db_path is None:
        log.info("backup.disabled", reason="not a SQLite file database")
        return None
    return Backups(
        db_path=db_path,
        backup_dir=settings.backup_dir or db_path.parent / "backups",
        interval=timedelta(hours=settings.backup_interval_hours),
        keep=settings.backup_keep,
    )


def warn_if_nobody_allowed(result: BootstrapResult) -> None:
    """Deny-by-default would otherwise make a fresh install look broken."""
    if result.users == 0:
        log.warning(
            "access.allow_list_empty",
            hint=(
                "Nobody is in yet, so the bot will refuse everyone. Message it once: the "
                "refusal shows your platform:user_id. Put it in REMINDARR_ADMINS and restart."
            ),
        )
    elif result.admins == 0:
        log.warning(
            "access.no_admin",
            hint="No admin, so nobody can /invite. Set REMINDARR_ADMINS to a user's name.",
        )


async def run(settings: Settings) -> None:
    adapters = build_adapters(settings)
    if not adapters:
        raise NoAdaptersConfigured(
            "No platform tokens set. Set REMINDARR_TELEGRAM_TOKEN, "
            "REMINDARR_DISCORD_TOKEN, and/or both REMINDARR_LINE_CHANNEL_SECRET and "
            "REMINDARR_LINE_CHANNEL_ACCESS_TOKEN (see .env.example)."
        )

    engine = create_engine(settings.database_url)
    await init_schema(engine)
    session_factory = create_session_factory(engine)

    async with session_scope(session_factory) as session:
        result = await bootstrap(session, settings, now=utcnow())
    warn_if_nobody_allowed(result)

    router = Router(
        settings=settings, session_factory=session_factory, reader=build_reader(settings)
    )
    router._bootstrapped = True

    # The protocols are runtime-checkable, so an adapter declares which halves
    # of the contract it implements simply by having the methods. A
    # delivery-only adapter (an SMS gateway, say) registers here and is
    # correctly skipped by the capture loop below.
    registry = DeliveryRegistry()
    for adapter in adapters:
        if isinstance(adapter, DeliveryAdapter):
            registry.register(adapter)

    scheduler = Scheduler(settings=settings, session_factory=session_factory, registry=registry)

    backups = build_backups(settings)
    health_server: HealthServer | None = None
    if settings.health_port is not None:
        health = Health(
            scheduler=scheduler,
            backups=backups,
            tick_seconds=settings.tick_seconds,
            started_at=utcnow(),
        )
        health_server = HealthServer(health, host=settings.health_host, port=settings.health_port)
    api_server: ApiServer | None = None
    if settings.api_port is not None:
        api = Api(router=router, settings=settings, session_factory=session_factory)
        api_server = ApiServer(api, host=settings.api_host, port=settings.api_port)

    started: list[CaptureAdapter] = []
    tasks: list[asyncio.Task[None]] = []
    stop_signal = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not available on all platforms
            loop.add_signal_handler(sig, stop_signal.set)

    try:
        for adapter in adapters:
            if isinstance(adapter, CaptureAdapter):
                await adapter.start(router.handle)
                started.append(adapter)

        log.info(
            "remindarr.ready",
            capture=[a.platform for a in started],
            delivery=registry.platforms(),
            timezone=settings.default_timezone,
        )

        tasks.append(asyncio.create_task(scheduler.run()))
        if backups is not None:
            tasks.append(asyncio.create_task(backups.run()))
        if health_server is not None:
            await health_server.start()
        if api_server is not None:
            await api_server.start()
        await stop_signal.wait()

        log.info("remindarr.shutting_down")
        scheduler.stop()
        if backups is not None:
            backups.stop()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    finally:
        if api_server is not None:
            with contextlib.suppress(Exception):
                await api_server.stop()
        if health_server is not None:
            with contextlib.suppress(Exception):
                await health_server.stop()
        for adapter in started:
            with contextlib.suppress(Exception):
                await adapter.stop()
        await engine.dispose()
        log.info("remindarr.stopped")
