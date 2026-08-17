"""Turns an incoming message into a reply.

All replies are **plain text**. Telegram's MarkdownV2 requires escaping a dozen
characters, Discord's markdown escapes a different set, and reminder text is
arbitrary user input that will eventually contain every one of them. Sending
plain text means a reminder that says ``deploy v2.1_final *really*`` renders as
written on both platforms instead of erroring or silently mangling.
"""

from __future__ import annotations

import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.config import Settings

from . import service
from .clock import utcnow
from .db import session_scope
from .humanize import format_when
from .models import ReminderState, Subscriber
from .timeparse import TimeParseError, parse
from .types import IncomingMessage, Reply, Route

HELP = """\
I set reminders. Tell me what and when:

  call the dentist tomorrow at 9
  remind me to check the burn-in in 45 minutes
  water the plants on saturday morning
  rent is due on 1 september

Commands:
  /list          what is pending
  /cancel <id>   drop one
  /tz <zone>     set your timezone (e.g. /tz Asia/Tokyo)
  /help          this
"""


@dataclass(slots=True)
class Router:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    clock: Callable[[], datetime] = utcnow
    """Injected so tests can pin "now" without patching the module."""

    async def handle(self, message: IncomingMessage) -> Reply | None:
        text = message.text.strip()
        if not text:
            return None

        now = self.clock()
        async with session_scope(self.session_factory) as session:
            subscriber = await service.get_or_create_subscriber(
                session,
                message.sender,
                default_timezone=self.settings.default_timezone,
                now=now,
            )
            tz = _safe_zone(subscriber.timezone, self.settings.default_timezone)

            # A command *must* start with "/". Otherwise "list the groceries
            # tomorrow" would silently become a /list and the reminder would be
            # thrown away.
            if not text.startswith("/"):
                return await self._create(session, subscriber, message.route, text, tz, now)

            head, _, rest = text[1:].partition(" ")
            # Telegram addresses commands as "/list@mybot" in group chats.
            command = head.split("@", 1)[0].lower()
            rest = rest.strip()

            match command:
                case "start" | "help" | "h":
                    return Reply(HELP)
                case "list" | "ls":
                    return await self._list(session, subscriber.id, tz, now)
                case "cancel" | "rm" | "del":
                    return await self._cancel(session, subscriber.id, rest, tz, now)
                case "tz" | "timezone":
                    return await self._timezone(session, subscriber, rest, now)
                case "remind" | "r":
                    # "/remind ..." is a reminder, not a command with a body.
                    return await self._create(session, subscriber, message.route, text, tz, now)
                case _:
                    return Reply(f"Unknown command /{command}. Try /help.")

    async def _create(
        self,
        session: AsyncSession,
        subscriber: Subscriber,
        route: Route,
        text: str,
        tz: ZoneInfo,
        now: datetime,
    ) -> Reply:
        try:
            parsed = parse(text, now_utc=now, tz=tz)
        except TimeParseError as exc:
            return Reply(f"{exc}.\n\nTry something like “call mom tomorrow at 9”, or /help.")

        reminder = await service.create_reminder(
            session,
            subscriber=subscriber,
            text=parsed.text,
            due_at=parsed.due_at,
            route=route,
            now=now,
        )
        when = format_when(parsed.due_at, tz, now_utc=now)
        lines = [f"Got it — “{parsed.text}” {when}.", f"id {reminder.id}"]
        lines.extend(f"note: {w}" for w in parsed.warnings)
        return Reply("\n".join(lines))

    async def _list(
        self, session: AsyncSession, subscriber_id: int, tz: ZoneInfo, now: datetime
    ) -> Reply:
        pending = await service.list_pending(session, subscriber_id=subscriber_id)
        if not pending:
            return Reply("Nothing pending.")
        lines = ["Pending:"]
        lines += [f"  {r.id}. {r.text} — {format_when(r.due_at, tz, now_utc=now)}" for r in pending]
        return Reply("\n".join(lines))

    async def _cancel(
        self, session: AsyncSession, subscriber_id: int, arg: str, tz: ZoneInfo, now: datetime
    ) -> Reply:
        if not arg.isdigit():
            return Reply("Which one? Use /cancel <id> — /list shows the ids.")
        reminder = await service.get_reminder(
            session, subscriber_id=subscriber_id, reminder_id=int(arg)
        )
        if reminder is None:
            return Reply(f"No reminder with id {arg}.")
        if reminder.state is not ReminderState.PENDING:
            return Reply(f"Reminder {arg} is already {reminder.state}.")
        await service.cancel_reminder(session, reminder=reminder)
        return Reply(f"Cancelled “{reminder.text}”.")

    async def _timezone(
        self, session: AsyncSession, subscriber: Subscriber, arg: str, now: datetime
    ) -> Reply:
        if not arg:
            local = now.astimezone(_safe_zone(subscriber.timezone, self.settings.default_timezone))
            return Reply(f"Your timezone is {subscriber.timezone} (currently {local:%H:%M}).")
        try:
            await service.set_timezone(session, subscriber=subscriber, tz=arg)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            return Reply(f"I do not know the timezone “{arg}”. Use an IANA name like Asia/Tokyo.")
        local = now.astimezone(ZoneInfo(arg))
        return Reply(f"Timezone set to {arg} (currently {local:%H:%M}).")


def _safe_zone(name: str, fallback: str) -> ZoneInfo:
    """Never let a bad stored timezone take the bot down.

    The value is validated on write, but a hand-edited database or a tzdata
    downgrade that drops a zone should degrade to the default, not crash every
    message this subscriber sends.
    """
    try:
        return ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        return ZoneInfo(fallback)
