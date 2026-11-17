"""Pairing the app: ``/pair`` codes, device tokens, and checking them.

A user sends ``/pair`` in chat and gets a code (the same 40-bit, hashed,
single-use kind ``/link`` uses, valid ten minutes). The app sends that code to
``POST /api/v1/pair`` and gets back a long-lived device token, which is the
only credential the app ever holds. The chat app is the identity provider:
whoever can read the bot's reply on LINE or Discord is that user.

Only hashes are stored -- of the code and of the token -- so a copy of the
database can't be used to sign in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import access
from .models import AccessCode, CodeKind, Device, User

MAX_DEVICE_NAME = 64


async def issue_pair_code(session: AsyncSession, *, user_id: int, now: datetime) -> str:
    """A fresh pairing code for ``user_id``, in canonical (undisplayed) form."""
    code = access.new_code()
    session.add(
        AccessCode(
            kind=CodeKind.PAIR,
            code_hash=access.code_hash(code),
            user_id=user_id,
            created_by=user_id,
            created_at=now,
            expires_at=now + access.PAIR_TTL,
        )
    )
    await session.flush()
    return code


@dataclass(frozen=True, slots=True)
class Paired:
    device: Device
    user: User
    token: str
    """The plaintext token. Exists only here, on its way to the app."""


async def redeem_pair_code(
    session: AsyncSession, *, raw_code: str, device_name: str, now: datetime
) -> Paired | None:
    """Swap a pairing code for a device token, or ``None`` for any failure.

    Wrong, expired, used, or for a user who has since been removed: all
    ``None``, so the caller can't tell them apart either.
    """
    code = access.normalize(raw_code)
    if code is None:
        return None
    row = (
        await session.execute(
            select(AccessCode).where(
                AccessCode.code_hash == access.code_hash(code), AccessCode.kind == CodeKind.PAIR
            )
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= now or row.user_id is None:
        return None
    user = await session.get(User, row.user_id)
    if user is None:
        return None

    row.used_at = now
    token = access.new_token()
    device = Device(
        user_id=user.id,
        name=clean_device_name(device_name),
        token_hash=access.token_hash(token),
        created_at=now,
        last_seen_at=now,
    )
    session.add(device)
    await session.flush()
    return Paired(device, user, token)


def clean_device_name(raw: str) -> str:
    """One line, trimmed, capped; "app" if nothing usable was sent."""
    name = " ".join(raw.split())[:MAX_DEVICE_NAME]
    return name or "app"


async def authenticate(
    session: AsyncSession, *, token: str, now: datetime
) -> tuple[Device, User] | None:
    """The live device and its user for a bearer token, or ``None``."""
    if not token:
        return None
    device = (
        await session.execute(select(Device).where(Device.token_hash == access.token_hash(token)))
    ).scalar_one_or_none()
    if device is None or device.revoked_at is not None:
        return None
    user = await session.get(User, device.user_id)
    if user is None:
        return None
    device.last_seen_at = now
    return device, user


async def list_devices(session: AsyncSession, *, user_id: int) -> list[Device]:
    """A user's devices that are still paired, oldest first.

    Their position in this list (1, 2, ...) is what ``/unpair <n>`` takes.
    """
    stmt = (
        select(Device)
        .where(Device.user_id == user_id, Device.revoked_at.is_(None))
        .order_by(Device.created_at, Device.id)
    )
    return list((await session.execute(stmt)).scalars())


def revoke(device: Device, *, now: datetime) -> None:
    device.revoked_at = now
