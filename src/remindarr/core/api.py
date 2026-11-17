"""The app API (v1): JSON over HTTP, one bearer token per paired device.

Deny by default. Every route except ``POST /api/v1/pair`` needs
``Authorization: Bearer <device token>``, and every answer is scoped to that
token's user; anything else is a 401 that says nothing about why. Unknown
paths are 404s.

It runs on a listener of its own (``REMINDARR_API_PORT``), like ``/healthz``,
so it works whether or not LINE is enabled, and it's off unless that port is
set. TLS is the deployment's job, as for the LINE webhook: in production a
reverse proxy forwards ``/api/*`` here.

Reminders are created through :meth:`Router.create`, the same path a chat
message takes, so the app reads "call mum tomorrow at 9" exactly as the bot
does: grammar first, the optional AI reader second, and the same ``@app`` and
``@person`` routing. The app is not a delivery surface (yet): a reminder set
in the app arrives on the user's chat app, as if they'd sent it in a 1:1 chat
there. :func:`home_identity` says which one.
"""

from __future__ import annotations

import json
import zoneinfo
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.config import Settings

from . import access, devices, phone, service
from .clock import utcnow
from .db import session_scope
from .directory import load as load_directory
from .humanize import format_next, format_when
from .models import Device, Reminder, ReminderState, Subscriber, User
from .router import TRY_THIS, Refused, Router, choose_chat_app, describe_delivery
from .types import IncomingMessage, Route, Sender

log = structlog.get_logger(__name__)

PREFIX = "/api/v1"
MAX_BODY_BYTES = 4096
MAX_TEXT_CHARS = 2000
"""Discord's own message limit, so anything settable in chat fits here too."""

UPCOMING = 10
"""How many occurrences of a series ``upcoming`` lists. The app schedules phone
notifications from them, and iOS keeps at most 64 pending across all of them."""

HOME_ORDER = ("discord", "telegram", "line")
"""Which linked app an app-set reminder goes to when the user has no default.
LINE comes last because its pushes count against a small monthly quota."""

UNAUTHORIZED = {"error": "unauthorized"}
BAD_PAIR_CODE = {"error": "That code isn't valid. It may have expired or already been used."}

Handler = Callable[[web.Request, AsyncSession, Device, User], Awaitable[web.Response]]


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _zone(name: str, fallback: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        return ZoneInfo(fallback)


async def _identities(session: AsyncSession, user_id: int) -> list[Subscriber]:
    stmt = select(Subscriber).where(Subscriber.user_id == user_id).order_by(Subscriber.id)
    return list((await session.execute(stmt)).scalars())


def home_identity(
    user: User, identities: list[Subscriber], enabled: list[str]
) -> Subscriber | None:
    """The chat identity an app-set reminder is set "from".

    The user's default app if they have one (``/deliver``), else the first
    of :data:`HOME_ORDER` they're linked on, among apps that are running.
    """
    live = [s for s in identities if s.platform in enabled]
    if user.deliver_on is not None:
        for sub in live:
            if sub.platform == user.deliver_on:
                return sub
    for platform in HOME_ORDER:
        for sub in live:
            if sub.platform == platform:
                return sub
    return live[0] if live else None


def upcoming(reminder: Reminder, limit: int = UPCOMING) -> list[datetime]:
    """The next occurrences, starting with ``due_at``: one for a one-off."""
    series = service.series_of(reminder)
    times = [reminder.due_at]
    while series is not None and len(times) < limit:
        nxt = series.next_after(times[-1])
        if nxt is None:
            break
        times.append(nxt)
    return times


def reminder_json(reminder: Reminder, tz: ZoneInfo, now: datetime) -> dict[str, Any]:
    series = service.series_of(reminder)
    if series is None:
        when = format_when(reminder.due_at, tz, now_utc=now)
    else:
        when = f"{series.describe()}, next {format_next(reminder.due_at, tz, now_utc=now)}"
    return {
        "id": reminder.id,
        "text": reminder.text,
        "due_at": _iso(reminder.due_at),
        "upcoming": [_iso(t) for t in upcoming(reminder)],
        "when": when,
        "recurrence": series.describe() if series is not None else None,
        "app": reminder.delivery_platform,
        "for": reminder.recipient,
    }


class Api:
    def __init__(
        self,
        *,
        router: Router,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime] = utcnow,
        limiter: access.AttemptLimiter | None = None,
    ) -> None:
        self._router = router
        self._settings = settings
        self._sessions = session_factory
        self._clock = clock
        self._limiter = limiter or access.AttemptLimiter()

    def app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_post(f"{PREFIX}/pair", self.pair)
        app.router.add_get(f"{PREFIX}/me", self._authed(self.me))
        app.router.add_get(f"{PREFIX}/reminders", self._authed(self.list_reminders))
        app.router.add_post(f"{PREFIX}/reminders", self._authed(self.create_reminder))
        app.router.add_delete(f"{PREFIX}/reminders/{{id}}", self._authed(self.cancel_reminder))
        app.router.add_delete(f"{PREFIX}/devices/self", self._authed(self.sign_out))
        app.router.add_post(f"{PREFIX}/devices/self/sync", self._authed(self.sync))
        app.router.add_get(f"{PREFIX}/settings", self._authed(self.get_settings))
        app.router.add_put(f"{PREFIX}/settings", self._authed(self.put_settings))
        return app

    # -- plumbing -----------------------------------------------------------

    def _authed(self, handler: Handler) -> Callable[[web.Request], Awaitable[web.Response]]:
        async def wrapped(request: web.Request) -> web.Response:
            scheme, _, token = request.headers.get("Authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                return web.json_response(UNAUTHORIZED, status=401)
            async with session_scope(self._sessions) as session:
                found = await devices.authenticate(session, token=token.strip(), now=self._clock())
                if found is None:
                    log.info("api.unauthorized", remote=request.remote, path=request.path)
                    return web.json_response(UNAUTHORIZED, status=401)
                return await handler(request, session, *found)

        return wrapped

    @staticmethod
    async def _json_body(request: web.Request) -> dict[str, Any] | web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(400, "body must be JSON")
        except web.HTTPRequestEntityTooLarge:
            return _error(413, "body too large")
        if not isinstance(body, dict):
            return _error(400, "body must be a JSON object")
        return body

    def _user_zone(self, identities: list[Subscriber]) -> ZoneInfo:
        default = self._settings.default_timezone
        return _zone(identities[0].timezone if identities else default, default)

    # -- endpoints ----------------------------------------------------------

    async def pair(self, request: web.Request) -> web.Response:
        """``{"code": "4F8K-29QW", "name": "Oz's iPhone"}`` -> a device token.

        The only unauthenticated call. Failures are rate-limited per remote
        address and overall (behind a proxy the address is the proxy's, so in
        practice it's the overall cap that bites).
        """
        now = self._clock()
        remote = f"ip:{request.remote or '?'}"
        if self._limiter.blocked(remote, now):
            return _error(429, "Too many wrong codes. Try again in an hour.")
        body = await self._json_body(request)
        if isinstance(body, web.Response):
            return body
        code, name = body.get("code"), body.get("name", "")
        if not isinstance(code, str) or not isinstance(name, str):
            return _error(400, "code and name must be strings")

        async with session_scope(self._sessions) as session:
            paired = await devices.redeem_pair_code(
                session, raw_code=code, device_name=name, now=now
            )
            if paired is None:
                self._limiter.record_failure(remote, now)
                log.info("api.bad_pair_code", remote=request.remote)
                return web.json_response(BAD_PAIR_CODE, status=401)
            log.info("api.paired", user=paired.user.name, device_id=paired.device.id)
            return web.json_response(
                {
                    "token": paired.token,
                    "device": {"id": paired.device.id, "name": paired.device.name},
                    "user": {"name": paired.user.name},
                },
                status=201,
            )

    async def me(
        self, request: web.Request, session: AsyncSession, device: Device, user: User
    ) -> web.Response:
        identities = await _identities(session, user.id)
        return web.json_response(
            {
                "name": user.name,
                "timezone": str(self._user_zone(identities)),
                "apps": sorted({s.platform for s in identities}),
                "deliver_on": user.deliver_on,
                "device": {"id": device.id, "name": device.name},
            }
        )

    async def list_reminders(
        self, request: web.Request, session: AsyncSession, device: Device, user: User
    ) -> web.Response:
        now = self._clock()
        tz = self._user_zone(await _identities(session, user.id))
        pending = await service.list_pending_for_user(session, user_id=user.id)
        return web.json_response(
            {
                "reminders": [reminder_json(r, tz, now) for r in pending],
                # Echoed back by the app's sync report: "I scheduled from this".
                "as_of": _iso(now),
            }
        )

    async def create_reminder(
        self, request: web.Request, session: AsyncSession, device: Device, user: User
    ) -> web.Response:
        body = await self._json_body(request)
        if isinstance(body, web.Response):
            return body
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return _error(400, "text must be a non-empty string")
        text = text.strip()
        if len(text) > MAX_TEXT_CHARS:
            return _error(400, f"text is longer than {MAX_TEXT_CHARS} characters")
        if text.startswith("/"):
            return _error(422, "Commands are for chat; send just the reminder.")

        now = self._clock()
        people = await load_directory(session, self._settings)
        identities = await _identities(session, user.id)
        home = home_identity(user, identities, people.enabled_platforms)
        if home is None:
            return _error(409, "None of your linked chat apps are running here.")
        tz = _zone(home.timezone, self._settings.default_timezone)
        message = IncomingMessage(
            sender=Sender(home.platform, home.platform_user_id, home.display_name),
            route=Route.direct_to(home.platform, home.platform_user_id),
            text=text,
            received_at=now,
            direct=True,
        )
        result = await self._router.create(session, people, home, message, text, tz, now)
        if isinstance(result, Refused):
            reason = (
                f"{result.reason}\n\n{TRY_THIS}."
                if result.unreadable and not result.localized
                else result.reason
            )
            return _error(422, reason)
        log.info("api.reminder_created", user=user.name, reminder_id=result.reminder.id)
        return web.json_response(
            {
                "reminder": reminder_json(result.reminder, tz, now),
                "confirmation": result.summary,
                "notes": list(result.notes),
            },
            status=201,
        )

    async def cancel_reminder(
        self, request: web.Request, session: AsyncSession, device: Device, user: User
    ) -> web.Response:
        raw = request.match_info["id"]
        if not raw.isdigit():
            return _error(404, "no such reminder")
        reminder = await service.get_reminder_for_user(
            session, user_id=user.id, reminder_id=int(raw)
        )
        if reminder is None:
            return _error(404, "no such reminder")
        if reminder.state is not ReminderState.PENDING:
            return _error(409, f"already {reminder.state}")
        await service.cancel_reminder(session, reminder=reminder)
        log.info("api.reminder_cancelled", user=user.name, reminder_id=reminder.id)
        return web.Response(status=204)

    async def sign_out(
        self, request: web.Request, session: AsyncSession, device: Device, user: User
    ) -> web.Response:
        devices.revoke(device, now=self._clock())
        log.info("api.signed_out", user=user.name, device_id=device.id)
        return web.Response(status=204)

    async def sync(
        self, request: web.Request, session: AsyncSession, device: Device, user: User
    ) -> web.Response:
        """``{"as_of": <from GET /reminders>, "notifications": bool, "complete":
        bool}``: what the app just scheduled. "Phone only" relies on it."""
        body = await self._json_body(request)
        if isinstance(body, web.Response):
            return body
        raw, notifications, complete = (
            body.get("as_of"),
            body.get("notifications"),
            body.get("complete"),
        )
        if not (
            isinstance(raw, str) and isinstance(notifications, bool) and isinstance(complete, bool)
        ):
            return _error(400, "as_of must be a string; notifications and complete booleans")
        try:
            as_of = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return _error(400, "as_of must be an ISO 8601 time")
        if as_of.tzinfo is None:
            return _error(400, "as_of needs a timezone")
        await phone.record_sync(
            session,
            device,
            as_of=as_of.astimezone(UTC),
            notifications=notifications,
            complete=complete,
            now=self._clock(),
        )
        return web.Response(status=204)

    async def _settings_json(self, session: AsyncSession, user: User) -> dict[str, Any]:
        now = self._clock()
        people = await load_directory(session, self._settings)
        identities = await _identities(session, user.id)
        return {
            "delivery": await phone.mode_of(session, user.id),
            "chat_app": user.deliver_on,
            "apps": sorted(
                {s.platform for s in identities if s.platform in people.enabled_platforms}
            ),
            "phone_ready": await phone.phone_ready(session, user.id, now),
            "summary": await describe_delivery(session, user, now),
        }

    async def get_settings(
        self, request: web.Request, session: AsyncSession, device: Device, user: User
    ) -> web.Response:
        return web.json_response(await self._settings_json(session, user))

    async def put_settings(
        self, request: web.Request, session: AsyncSession, device: Device, user: User
    ) -> web.Response:
        """``{"delivery": "chat"|"both"|"phone", "chat_app": "discord"|...|null}``,
        either or both. The same setting as ``/deliver`` in chat."""
        body = await self._json_body(request)
        if isinstance(body, web.Response):
            return body
        mode = body.get("delivery")
        if mode is not None and mode not in phone.MODES:
            return _error(400, f"delivery must be one of {', '.join(phone.MODES)}")
        if "chat_app" in body:
            app = body["chat_app"]
            if app is not None and not isinstance(app, str):
                return _error(400, "chat_app must be a string or null")
            people = await load_directory(session, self._settings)
            identities = await _identities(session, user.id)
            if not identities:
                return _error(409, "You have no linked chat apps.")
            me = identities[0]
            error = choose_chat_app(
                user,
                (app or "here").lower(),
                people,
                Sender(me.platform, me.platform_user_id, me.display_name),
            )
            if error is not None:
                return _error(422, error)
        if mode is not None:
            await phone.set_mode(session, user.id, mode, self._clock())
        log.info("api.settings", user=user.name, delivery=mode, chat_app=user.deliver_on)
        return web.json_response(await self._settings_json(session, user))


class ApiServer:
    """The API's own listener, started and stopped with the app."""

    def __init__(self, api: Api, *, host: str, port: int) -> None:
        self._api = api
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._api.app(), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        log.info("api.listening", host=self._host, port=self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
