"""Who's who, from the database.

The database is the source of truth for who may use the bot (an identity
linked to a user) and which identities are one person. Config only seeds it:

* **The first time there are no users at all**, ``REMINDARR_PEOPLE`` and
  ``REMINDARR_ALLOWED_USERS`` are imported, so an existing install keeps
  working unchanged. After that they are ignored: ``/invite`` and ``/remove``
  manage who's in.
* **At every start**, ``REMINDARR_ADMINS`` grants admin. An identity listed
  there that belongs to no one yet becomes a new admin user, which is how a
  fresh install gets its first user without any import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from remindarr.config import Person, Settings, valid_person_name

from .models import Subscriber, User

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Directory:
    """A snapshot of users and their identities, for one message or delivery.

    Exposes the same ``people`` / ``person_of`` / ``enabled_platforms`` that
    delivery routing reads, so routing doesn't care where people come from.
    """

    people: tuple[Person, ...]
    enabled_platforms: list[str]
    admin_names: frozenset[str]

    def person_of(self, platform: str, user_id: str) -> Person | None:
        identity = f"{platform.lower()}:{user_id}"
        return next((p for p in self.people if identity in p.identities), None)

    def is_allowed(self, platform: str, user_id: str) -> bool:
        return self.person_of(platform, user_id) is not None

    def is_admin(self, platform: str, user_id: str) -> bool:
        person = self.person_of(platform, user_id)
        return person is not None and person.name in self.admin_names


async def load(session: AsyncSession, settings: Settings) -> Directory:
    users = (await session.execute(select(User).options(selectinload(User.identities)))).scalars()
    people: list[Person] = []
    admins: set[str] = set()
    for user in users:
        if user.is_admin:
            admins.add(user.name)
        if not user.identities:
            continue
        people.append(
            Person(
                user.name,
                frozenset(f"{s.platform}:{s.platform_user_id}" for s in user.identities),
                user.deliver_on,
                user.auto_named,
            )
        )
    return Directory(tuple(people), settings.enabled_platforms, frozenset(admins))


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    users: int
    admins: int
    imported: bool


async def bootstrap(session: AsyncSession, settings: Settings, *, now: datetime) -> BootstrapResult:
    """Seed from config if there are no users yet, then apply ``admins``."""
    imported = False
    if not await _count(session, User):
        imported = await _import_seed(session, settings, now)
    await _apply_admins(session, settings, now)
    await session.flush()
    admins = (
        await session.execute(select(func.count()).select_from(User).where(User.is_admin))
    ).scalar_one()
    return BootstrapResult(await _count(session, User), admins, imported)


async def _count(session: AsyncSession, model: type[User]) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def identity_row(
    session: AsyncSession, identity: str, *, timezone: str, now: datetime
) -> Subscriber:
    """The subscriber row for ``platform:user_id``, created if needed."""
    platform, _, user_id = identity.partition(":")
    stmt = select(Subscriber).where(
        Subscriber.platform == platform, Subscriber.platform_user_id == user_id
    )
    sub = (await session.execute(stmt)).scalar_one_or_none()
    if sub is None:
        sub = Subscriber(
            platform=platform, platform_user_id=user_id, timezone=timezone, created_at=now
        )
        session.add(sub)
        await session.flush()
    return sub


async def _import_seed(session: AsyncSession, settings: Settings, now: datetime) -> bool:
    if not settings.people and not settings.allowed_users:
        return False
    taken: set[str] = set()
    covered: set[str] = set()
    for person in settings.people:
        user = User(name=person.name, deliver_on=person.deliver_on, created_at=now)
        session.add(user)
        await session.flush()
        taken.add(person.name)
        for identity in sorted(person.identities):
            sub = await identity_row(session, identity, timezone=settings.default_timezone, now=now)
            sub.user_id = user.id
            covered.add(identity)
    for identity in sorted(settings.allowed_users - covered):
        name = _free_name(_auto_name(identity), taken)
        taken.add(name)
        user = User(name=name, auto_named=True, created_at=now)
        session.add(user)
        await session.flush()
        sub = await identity_row(session, identity, timezone=settings.default_timezone, now=now)
        sub.user_id = user.id
    log.info(
        "users.imported_from_config",
        people=len(settings.people),
        identities=len(settings.allowed_users),
    )
    return True


async def _apply_admins(session: AsyncSession, settings: Settings, now: datetime) -> None:
    for entry in sorted(settings.admins):
        if ":" in entry:
            sub = await identity_row(session, entry, timezone=settings.default_timezone, now=now)
            if sub.user_id is not None:
                user = await session.get(User, sub.user_id)
                assert user is not None
            else:
                names = set((await session.execute(select(User.name))).scalars())
                user = User(name=_free_name("owner", names), created_at=now)
                session.add(user)
                await session.flush()
                sub.user_id = user.id
                log.info("users.admin_created", user=user.name, identity=entry)
            user.is_admin = True
        else:
            user = (
                await session.execute(select(User).where(User.name == entry))
            ).scalar_one_or_none()
            if user is None:
                log.warning("users.admin_unknown", name=entry)
            else:
                user.is_admin = True


def _auto_name(identity: str) -> str:
    platform, _, user_id = identity.partition(":")
    slug = re.sub(r"[^a-z0-9]", "", user_id.lower())[:8] or "user"
    return f"{platform}-{slug}"


def _free_name(base: str, taken: set[str]) -> str:
    if base not in taken and valid_person_name(base):
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"
