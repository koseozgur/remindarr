"""Wiring: build the pieces, start them, shut them down cleanly."""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog

from remindarr.adapters.base import CaptureAdapter, DeliveryAdapter, DeliveryRegistry
from remindarr.config import Settings
from remindarr.core.db import create_engine, create_session_factory, init_schema
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

    router = Router(settings=settings, session_factory=session_factory)

    # The protocols are runtime-checkable, so an adapter declares which halves
    # of the contract it implements simply by having the methods. A
    # delivery-only adapter (an SMS gateway, say) registers here and is
    # correctly skipped by the capture loop below.
    registry = DeliveryRegistry()
    for adapter in adapters:
        if isinstance(adapter, DeliveryAdapter):
            registry.register(adapter)

    scheduler = Scheduler(settings=settings, session_factory=session_factory, registry=registry)

    started: list[CaptureAdapter] = []
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

        scheduler_task = asyncio.create_task(scheduler.run())
        await stop_signal.wait()

        log.info("remindarr.shutting_down")
        scheduler.stop()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(scheduler_task, timeout=10)
    finally:
        for adapter in started:
            with contextlib.suppress(Exception):
                await adapter.stop()
        await engine.dispose()
        log.info("remindarr.stopped")
