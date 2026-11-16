"""Short codes that bring a new chat identity in, and the limits around them.

A code is 8 characters of Crockford base32 -- 40 bits -- shown as
``XXXX-XXXX``. The alphabet has no I, L, O or U, and reading it back forgives
the usual slips (``o`` for ``0``, ``l`` or ``i`` for ``1``, lowercase, the
hyphen or spaces), because people will retype these from one phone to another.

Only a SHA-256 of the code is stored. Codes expire quickly and work once, and
failed attempts are rate-limited, so guessing one inside its window isn't
practical. Every kind of failure gets the same answer, so a wrong guess learns
nothing about which codes exist.
"""

from __future__ import annotations

import hashlib
import secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8

INVITE_TTL = timedelta(hours=24)
LINK_TTL = timedelta(minutes=10)

_FORGIVEN = str.maketrans({"O": "0", "I": "1", "L": "1"})


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def display(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def normalize(raw: str) -> str | None:
    """A typed code in canonical form, or None if it can't be one."""
    code = "".join(raw.split()).replace("-", "").upper().translate(_FORGIVEN)
    if len(code) != CODE_LENGTH or any(c not in ALPHABET for c in code):
        return None
    return code


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


@dataclass(slots=True)
class AttemptLimiter:
    """Caps failed code attempts per identity and across everyone.

    In memory on purpose: a restart resets it, which costs an attacker nothing
    they couldn't get by waiting, and keeps failures out of the database.
    """

    per_identity: int = 5
    overall: int = 50
    window: timedelta = timedelta(hours=1)
    _by_identity: dict[str, deque[datetime]] = field(default_factory=dict)
    _all: deque[datetime] = field(default_factory=deque)

    def blocked(self, identity: str, now: datetime) -> bool:
        self._expire(now)
        mine = self._by_identity.get(identity)
        return len(self._all) >= self.overall or (
            mine is not None and len(mine) >= self.per_identity
        )

    def record_failure(self, identity: str, now: datetime) -> None:
        self._by_identity.setdefault(identity, deque()).append(now)
        self._all.append(now)

    def _expire(self, now: datetime) -> None:
        cutoff = now - self.window
        while self._all and self._all[0] <= cutoff:
            self._all.popleft()
        for key in list(self._by_identity):
            q = self._by_identity[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if not q:
                del self._by_identity[key]
