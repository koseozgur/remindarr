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
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import delete, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.ai.reading import AiUnavailable, Reader, to_reminder
from remindarr.config import PLATFORM_NAMES, Settings, valid_person_name

from . import access, delivery, service
from .clock import utcnow
from .db import session_scope
from .directory import Directory, bootstrap, identity_row
from .directory import load as load_directory
from .humanize import format_next, format_when
from .models import AccessCode, CodeKind, Reminder, ReminderState, Subscriber, User
from .timeparse import TimeParseError, parse
from .types import USER_PREFIX, IncomingMessage, ParsedReminder, Reply, Route

log = structlog.get_logger(__name__)

HELP = """\
I set reminders. Tell me what and when:

  call the dentist tomorrow at 9
  remind me to check the burn-in in 45 minutes
  water the plants on saturday morning
  rent is due on 1 september
  take the bins out every monday at 7pm

Add @line, @discord or @telegram to have it delivered on that app instead.
Add @name to remind someone else.

Commands:
  /list            what is pending
  /cancel <id>     drop one
  /tz <zone>       set your timezone (e.g. /tz Asia/Tokyo)
  /deliver <app>   where reminders you set in a 1:1 chat go (or "here")
  /link            connect another app to you
  /help            this
"""

ADMIN_HELP = """
Admin:
  /invite <name>   a code for someone new to /join with
  /users           who's in
  /remove <name>   take someone out (cancels their reminders)
"""

NOT_AUTHORISED = (
    "Not authorised. If you have an invite, send /join followed by the code.\n"
    "(For the owner: {identity})"
)
BAD_CODE = "That code isn't valid. It may have expired or already been used."
"""Every kind of failure gets the same answer, so a guess learns nothing."""


@dataclass(slots=True)
class Router:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    clock: Callable[[], datetime] = utcnow
    """Injected so tests can pin "now" without patching the module."""
    limiter: access.AttemptLimiter = field(default_factory=access.AttemptLimiter)
    reader: Reader | None = None
    """Optional AI reader, asked only when the grammar can't parse a message."""
    _bootstrapped: bool = field(default=False, init=False)

    async def handle(self, message: IncomingMessage) -> Reply | None:
        text = message.text.strip()
        if not text:
            return None

        now = self.clock()
        sender = message.sender
        command, rest = _split_command(text)
        async with session_scope(self.session_factory) as session:
            if not self._bootstrapped:
                await bootstrap(session, self.settings, now=now)
                self._bootstrapped = True
            people = await load_directory(session, self.settings)

            # A stranger can redeem a code and nothing else: no subscriber row,
            # no reminder, no command, not even /help. The refusal is a reply,
            # which on LINE is free, so it can't be used to burn push quota.
            if not people.is_allowed(sender.platform, sender.user_id):
                if command in ("join", "link") and rest:
                    return await self._redeem(session, message, command, rest, now)
                identity = f"{sender.platform}:{sender.user_id}"
                log.info("access.denied", user=identity, route=str(message.route))
                return Reply(NOT_AUTHORISED.format(identity=identity))

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
            if command is None:
                return await self._create(session, people, subscriber, message, text, tz, now)

            is_admin = people.is_admin(sender.platform, sender.user_id)
            match command:
                case "start" | "help" | "h":
                    return Reply(HELP + (ADMIN_HELP if is_admin else ""))
                case "list" | "ls":
                    return await self._list(session, subscriber.id, message.route, tz, now)
                case "cancel" | "rm" | "del":
                    return await self._cancel(session, subscriber.id, rest, tz, now)
                case "tz" | "timezone":
                    return await self._timezone(session, subscriber, rest, now)
                case "remind" | "r":
                    # "/remind ..." is a reminder, not a command with a body.
                    return await self._create(session, people, subscriber, message, text, tz, now)
                case "join":
                    return Reply(f"You're already in, as {_name_of(people, message)}.")
                case "link":
                    if rest:
                        return Reply(
                            f"This account is already linked to {_name_of(people, message)}."
                        )
                    return await self._issue_link(session, subscriber, people, message, now)
                case "deliver":
                    return await self._deliver(session, subscriber, people, message, rest)
                case "invite" | "users" | "remove" if not is_admin:
                    return Reply(f"Only an admin can /{command}.")
                case "invite":
                    return await self._invite(session, subscriber, rest, now)
                case "users":
                    return await self._users(session)
                case "remove":
                    return await self._remove(session, people, message, rest)
                case _:
                    return Reply(f"Unknown command /{command}. Try /help.")

    async def _create(
        self,
        session: AsyncSession,
        people: Directory,
        subscriber: Subscriber,
        message: IncomingMessage,
        text: str,
        tz: ZoneInfo,
        now: datetime,
    ) -> Reply:
        # The @app override comes out before time parsing, so "@line" can never
        # be read as part of the task or the time.
        target = delivery.resolve(message, text, people)
        if isinstance(target, delivery.Refusal):
            return Reply(target.reason)

        try:
            parsed = parse(target.text, now_utc=now, tz=tz)
        except TimeParseError as exc:
            ai_parsed = await self._ask_ai(target.text, now, tz)
            if ai_parsed is None:
                return Reply(f"{exc}.\n\nTry something like “call mom tomorrow at 9”, or /help.")
            parsed = ai_parsed

        reminder = await service.create_reminder(
            session,
            subscriber=subscriber,
            text=parsed.text,
            due_at=parsed.due_at,
            route=target.route,
            fallback=target.fallback,
            recipient=target.recipient,
            now=now,
            recurrence=parsed.recurrence,
        )
        series = service.series_of(reminder)
        if series is not None:
            nxt = format_next(parsed.due_at, tz, now_utc=now)
            when = f"{series.describe()}, next: {nxt}"
        else:
            when = format_when(parsed.due_at, tz, now_utc=now)
        lines = [f"Got it — “{parsed.text}” {when}.", f"id {reminder.id}"]
        where = delivery.disclosure(target, message.route, people, message.sender)
        if where is not None:
            lines.append(where)
        lines.extend(f"note: {w}" for w in parsed.warnings)
        return Reply("\n".join(lines))

    async def _ask_ai(self, text: str, now: datetime, tz: ZoneInfo) -> ParsedReminder | None:
        """The AI's reading, or None to fall back on the grammar's refusal.

        Grammar first, always: it's instant, free and predictable. The AI only
        sees what the grammar gave up on, and its answer must pass the same
        checks a grammar result would (future, real date, supported repeat).
        """
        if self.reader is None:
            return None
        try:
            reading = await self.reader.read(text, now_local=now.astimezone(tz))
            return to_reminder(reading, now_utc=now, tz=tz)
        except AiUnavailable as exc:
            log.info("ai.unavailable", reason=str(exc))
        except TimeParseError as exc:
            log.info("ai.unreadable", reason=str(exc))
        return None

    async def _list(
        self,
        session: AsyncSession,
        subscriber_id: int,
        here: Route,
        tz: ZoneInfo,
        now: datetime,
    ) -> Reply:
        pending = await service.list_pending(session, subscriber_id=subscriber_id)
        if not pending:
            return Reply("Nothing pending.")
        lines = ["Pending:"]
        for r in pending:
            where = delivery.label(
                Route(r.delivery_platform, r.delivery_address), here, r.recipient
            )
            # "#3", not "3.": Discord renders "3. text" as a markdown ordered
            # list (renumbered, with extra spacing). "#3" with no space after
            # it is plain text everywhere.
            series = service.series_of(r)
            if series is None:
                when = format_when(r.due_at, tz, now_utc=now)
                lines.append(f"  #{r.id} {r.text} — {when}{where}")
            else:
                # Pattern and next occurrence in the same zone: the series'
                # own, named when a later /tz has made it differ from yours.
                zone, pattern = tz, series.describe()
                if not series.utc_timeline and series.tz.key != tz.key:
                    zone, pattern = series.tz, f"{pattern} {series.tz.key} time"
                nxt = format_next(r.due_at, zone, now_utc=now)
                lines.append(f"  #{r.id} 🔁 {r.text} — {pattern}, next {nxt}{where}")
        return Reply("\n".join(lines))

    async def _cancel(
        self, session: AsyncSession, subscriber_id: int, arg: str, tz: ZoneInfo, now: datetime
    ) -> Reply:
        arg = arg.removeprefix("#")  # /list shows "#3", so "/cancel #3" must work
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
        series = service.series_of(reminder)
        if series is not None:
            # /cancel stops the whole series: one row carries every occurrence.
            return Reply(f"Cancelled “{reminder.text}” ({series.describe()}) — no more repeats.")
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
        # One person, one clock: keep every app they've linked in step.
        if subscriber.user_id is not None:
            others = await session.execute(
                select(Subscriber).where(Subscriber.user_id == subscriber.user_id)
            )
            for other in others.scalars():
                other.timezone = arg
        local = now.astimezone(ZoneInfo(arg))
        return Reply(f"Timezone set to {arg} (currently {local:%H:%M}).")

    async def _redeem(
        self, session: AsyncSession, message: IncomingMessage, command: str, raw: str, now: datetime
    ) -> Reply:
        """``/join CODE`` or ``/link CODE`` from an identity that isn't in yet."""
        sender = message.sender
        identity = f"{sender.platform}:{sender.user_id}"
        if self.limiter.blocked(identity, now):
            return Reply("Too many wrong codes. Try again in an hour.")

        kind = CodeKind.INVITE if command == "join" else CodeKind.LINK
        code = access.normalize(raw)
        row = None
        if code is not None:
            row = (
                await session.execute(
                    select(AccessCode).where(
                        AccessCode.code_hash == access.code_hash(code), AccessCode.kind == kind
                    )
                )
            ).scalar_one_or_none()
        if row is None or row.used_at is not None or row.expires_at <= now:
            self.limiter.record_failure(identity, now)
            log.info("access.bad_code", user=identity, kind=kind.value)
            return Reply(BAD_CODE)

        timezone = self.settings.default_timezone
        if kind is CodeKind.INVITE:
            assert row.invite_name is not None
            taken = await session.execute(select(User).where(User.name == row.invite_name))
            if taken.scalar_one_or_none() is not None:
                # Invited twice and the other code got there first.
                self.limiter.record_failure(identity, now)
                return Reply(BAD_CODE)
            user = User(name=row.invite_name, created_at=now)
            session.add(user)
            await session.flush()
        else:
            linked_to = await session.get(User, row.user_id) if row.user_id else None
            if linked_to is None:
                self.limiter.record_failure(identity, now)
                return Reply(BAD_CODE)
            user = linked_to
            existing = (
                (await session.execute(select(Subscriber).where(Subscriber.user_id == user.id)))
                .scalars()
                .all()
            )
            platform_name = PLATFORM_NAMES.get(sender.platform, sender.platform)
            if any(s.platform == sender.platform for s in existing):
                return Reply(
                    f"{user.name} already has a {platform_name} account linked. One per app."
                )
            if existing:
                timezone = existing[0].timezone  # one person, one clock

        row.used_at = now
        sub = await identity_row(session, identity, timezone=timezone, now=now)
        sub.user_id = user.id
        sub.display_name = sender.display_name or sub.display_name
        log.info("access.redeemed", user=user.name, identity=identity, kind=kind.value)
        if kind is CodeKind.INVITE:
            return Reply(
                f"Welcome, {user.name}! You're in.\n\n"
                "Tell me what to remind you about, e.g. “call the dentist tomorrow at 9”. "
                "/help shows everything."
            )
        return Reply(
            f"Linked: this {PLATFORM_NAMES.get(sender.platform, sender.platform)} account "
            f"is now part of {user.name}."
        )

    async def _issue_link(
        self,
        session: AsyncSession,
        subscriber: Subscriber,
        people: Directory,
        message: IncomingMessage,
        now: datetime,
    ) -> Reply:
        assert subscriber.user_id is not None  # allowed means linked
        code = access.new_code()
        session.add(
            AccessCode(
                kind=CodeKind.LINK,
                code_hash=access.code_hash(code),
                user_id=subscriber.user_id,
                created_by=subscriber.user_id,
                created_at=now,
                expires_at=now + access.LINK_TTL,
            )
        )
        minutes = int(access.LINK_TTL.total_seconds() // 60)
        return Reply(
            f"Send this from your other app within {minutes} minutes:\n/link {access.display(code)}"
        )

    async def _deliver(
        self,
        session: AsyncSession,
        subscriber: Subscriber,
        people: Directory,
        message: IncomingMessage,
        arg: str,
    ) -> Reply:
        user = await session.get(User, subscriber.user_id) if subscriber.user_id else None
        assert user is not None
        choice = arg.strip().lower()
        if not choice:
            current = (
                f"on {PLATFORM_NAMES.get(user.deliver_on, user.deliver_on)}"
                if user.deliver_on
                else "wherever you set them"
            )
            return Reply(
                f"Reminders you set in a 1:1 chat arrive {current}. "
                "Change it with /deliver line, /deliver discord or /deliver here."
            )
        if choice in ("here", "off", "same"):
            user.deliver_on = None
            return Reply("Done: reminders arrive wherever you set them.")
        if choice not in PLATFORM_NAMES:
            return Reply(f"I don't know the app “{arg.strip()}”. Try line, discord or here.")
        name = PLATFORM_NAMES[choice]
        if choice not in people.enabled_platforms:
            return Reply(f"{name} isn't set up here.")
        person = people.person_of(message.sender.platform, message.sender.user_id)
        if person is None or person.user_id_on(choice) is None:
            return Reply(
                f"You haven't linked {name} yet. Send /link here, then send the code from {name}."
            )
        user.deliver_on = choice
        return Reply(f"Done: reminders you set in a 1:1 chat now arrive on {name}.")

    async def _invite(
        self, session: AsyncSession, subscriber: Subscriber, arg: str, now: datetime
    ) -> Reply:
        name = arg.strip().lower()
        if not name:
            return Reply("Who for? /invite <name>, e.g. /invite mum")
        if not valid_person_name(name):
            return Reply(
                "Names are lowercase letters, digits, - and _ (up to 32), and can't be "
                "an app's name."
            )
        if (await session.execute(select(User).where(User.name == name))).scalar_one_or_none():
            return Reply(f"There's already someone called {name}.")
        code = access.new_code()
        session.add(
            AccessCode(
                kind=CodeKind.INVITE,
                code_hash=access.code_hash(code),
                invite_name=name,
                created_by=subscriber.user_id,
                created_at=now,
                expires_at=now + access.INVITE_TTL,
            )
        )
        apps = " or ".join(PLATFORM_NAMES[p] for p in self.settings.enabled_platforms)
        hours = int(access.INVITE_TTL.total_seconds() // 3600)
        log.info("access.invited", name=name)
        return Reply(
            f"Ask {name} to send me this on {apps} within {hours} hours:\n"
            f"/join {access.display(code)}"
        )

    async def _users(self, session: AsyncSession) -> Reply:
        users = (await session.execute(select(User).order_by(User.name))).scalars().all()
        if not users:
            return Reply("Nobody yet.")
        lines = ["Users:"]
        for user in users:
            subs = (
                (await session.execute(select(Subscriber).where(Subscriber.user_id == user.id)))
                .scalars()
                .all()
            )
            apps = ", ".join(sorted(PLATFORM_NAMES.get(s.platform, s.platform) for s in subs))
            tags = [t for t, on in (("admin", user.is_admin), ("no apps linked", not subs)) if on]
            if user.deliver_on:
                tags.append(f"delivers on {PLATFORM_NAMES.get(user.deliver_on, user.deliver_on)}")
            suffix = f" ({'; '.join(tags)})" if tags else ""
            lines.append(f"  {user.name}: {apps or '-'}{suffix}")
        return Reply("\n".join(lines))

    async def _remove(
        self, session: AsyncSession, people: Directory, message: IncomingMessage, arg: str
    ) -> Reply:
        name = arg.strip().lower()
        if not name:
            return Reply("Who? /remove <name>. /users lists everyone.")
        user = (await session.execute(select(User).where(User.name == name))).scalar_one_or_none()
        if user is None:
            return Reply(f"No user named {name}.")
        if name == _name_of(people, message):
            return Reply("You can't remove yourself.")

        subs = (
            (await session.execute(select(Subscriber).where(Subscriber.user_id == user.id)))
            .scalars()
            .all()
        )
        # Their own reminders, and reminders others set *for* them: either way
        # nothing should reach someone who has been taken out.
        direct = [(s.platform, f"{USER_PREFIX}{s.platform_user_id}") for s in subs]
        conditions = [Reminder.subscriber_id.in_([s.id for s in subs])]
        if direct:
            conditions.append(
                tuple_(Reminder.delivery_platform, Reminder.delivery_address).in_(direct)
            )
        pending = (
            (
                await session.execute(
                    select(Reminder).where(
                        Reminder.state == ReminderState.PENDING, or_(*conditions)
                    )
                )
            )
            .scalars()
            .all()
        )
        for reminder in pending:
            reminder.state = ReminderState.CANCELLED
        for sub in subs:
            sub.user_id = None
        await session.execute(
            delete(AccessCode).where(
                or_(AccessCode.user_id == user.id, AccessCode.created_by == user.id)
            )
        )
        await session.delete(user)
        log.info("access.removed", user=name, cancelled=len(pending))
        return Reply(f"Removed {name}. Cancelled {len(pending)} pending reminder(s).")


def _split_command(text: str) -> tuple[str | None, str]:
    """``"/list@mybot extra"`` -> ``("list", "extra")``; not a command -> ``(None, text)``."""
    if not text.startswith("/"):
        return None, text
    head, _, rest = text[1:].partition(" ")
    # Telegram addresses commands as "/list@mybot" in group chats.
    return head.split("@", 1)[0].lower(), rest.strip()


def _name_of(people: Directory, message: IncomingMessage) -> str:
    person = people.person_of(message.sender.platform, message.sender.user_id)
    return person.name if person is not None else "someone"


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
