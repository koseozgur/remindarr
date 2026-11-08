"""Configuration, from environment or a ``.env`` file.

An adapter is enabled by having a token. There is no separate "enable" flag to
get out of sync with whether the credential is actually present. LINE needs two
credentials, so it is enabled by having both -- and having only one is a
startup error rather than a silently disabled platform.
"""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

KNOWN_PLATFORMS = frozenset({"telegram", "discord", "line"})
"""Platform names as adapters report them in ``Sender.platform``."""

PLATFORM_NAMES = {"telegram": "Telegram", "discord": "Discord", "line": "LINE"}
"""How a platform is spelled to humans, in replies."""

_PERSON_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def parse_identity(entry: str, *, field: str) -> str:
    """``" LINE : U1234 "`` -> ``"line:U1234"``, or a ValueError naming ``field``.

    The platform is case-insensitive (normalised to lowercase, as adapters
    report it); the user id is kept exactly, because LINE ids are
    case-sensitive. Only the first colon separates: ids are opaque.
    """
    platform, sep, user_id = entry.partition(":")
    platform, user_id = platform.strip().lower(), user_id.strip()
    if not sep or not platform or not user_id or any(c.isspace() for c in user_id):
        raise ValueError(
            f"{field} entry {entry.strip()!r} is not platform:user_id "
            "(e.g. line:U1234abcd or discord:123456789)"
        )
    if platform not in KNOWN_PLATFORMS:
        known = ", ".join(sorted(KNOWN_PLATFORMS))
        raise ValueError(
            f"{field} entry {entry.strip()!r} has unknown platform {platform!r} (known: {known})"
        )
    return f"{platform}:{user_id}"


@dataclass(frozen=True, slots=True)
class Person:
    """One human's identities across platforms, as the owner declared them.

    Declared in config rather than claimed in chat: the owner is the only one
    who edits config, so there is no "prove you own both accounts" problem.
    """

    name: str
    identities: frozenset[str]
    """``platform:user_id`` entries, at most one per platform."""

    deliver_on: str | None = None
    """Platform that reminders set in a one-to-one chat are delivered on.
    ``None`` means "wherever it was set"."""

    def user_id_on(self, platform: str) -> str | None:
        for identity in self.identities:
            p, _, user_id = identity.partition(":")
            if p == platform:
                return user_id
        return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REMINDARR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///data/remindarr.db"

    default_timezone: str = "Asia/Tokyo"
    """Timezone assumed for a subscriber we have not seen before."""

    telegram_token: SecretStr | None = None
    discord_token: SecretStr | None = None

    line_channel_secret: SecretStr | None = None
    """Verifies that webhook requests really come from LINE."""
    line_channel_access_token: SecretStr | None = None
    """Authorises replies and pushes. The long-lived token from the console."""

    webhook_host: str = "0.0.0.0"
    webhook_port: Annotated[int, Field(ge=1, le=65535)] = 8080
    line_webhook_path: str = "/line/webhook"

    tick_seconds: Annotated[float, Field(gt=0)] = 15.0
    """How often the scheduler looks for due reminders."""

    max_delivery_attempts: Annotated[int, Field(ge=1)] = 5

    retry_backoff_seconds: Annotated[float, Field(gt=0)] = 30.0
    """Base for exponential retry backoff: base * 2**(attempts-1)."""

    late_delivery_grace_minutes: Annotated[int, Field(ge=0)] = 60
    """A reminder that came due while the process was down is still delivered,
    but past this much lateness the message says so rather than pretending it
    is on time."""

    allowed_users: Annotated[frozenset[str], NoDecode] = frozenset()
    """Who may use the bot, as ``platform:user_id`` entries. Empty means nobody:
    deny by default, because on LINE every stranger's reminder spends the
    owner's push quota. ``NoDecode`` because pydantic-settings would otherwise
    parse the env value as JSON rather than a comma-separated list."""

    people: Annotated[tuple[Person, ...], NoDecode] = ()
    """Which identities are one person, and where they want reminders. See
    :meth:`_parse_people` for the syntax."""

    log_level: str = "INFO"
    log_json: bool = False

    @field_validator("default_timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            zoneinfo.ZoneInfo(v)
        except zoneinfo.ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @field_validator("allowed_users", mode="before")
    @classmethod
    def _parse_allowed_users(cls, v: object) -> object:
        """``line:U1234, discord:5678`` -> ``{"line:U1234", "discord:5678"}``.

        See :func:`parse_identity` for case handling. Empty entries, e.g. from
        a trailing comma, are skipped.
        """
        if v is None:
            return frozenset()
        if isinstance(v, str):
            raw = v.split(",")
        elif isinstance(v, (list, tuple, set, frozenset)):
            raw = list(v)
        else:
            return v  # let pydantic reject it with its own message

        return frozenset(
            parse_identity(str(item), field="allowed_users") for item in raw if str(item).strip()
        )

    @field_validator("people", mode="before")
    @classmethod
    def _parse_people(cls, v: object) -> object:
        """``ozgur = line:U1, discord:2 > discord; mum = line:U3``.

        People are separated by ``;``. Each is ``name = identity, identity``,
        optionally followed by ``> platform``: the app that person's reminders
        are delivered on when set in a one-to-one chat. The platform must be
        one the person has an identity on. Names are case-insensitive.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return ()
        if not isinstance(v, str):
            return v  # already-built Persons (tests), or let pydantic reject it

        people: list[Person] = []
        owner: dict[str, str] = {}  # identity -> person name
        for chunk in v.split(";"):
            if not chunk.strip():
                continue
            name, sep, rest = chunk.partition("=")
            name = name.strip().lower()
            if not sep or not _PERSON_NAME.fullmatch(name):
                raise ValueError(
                    f"people entry {chunk.strip()!r} is not 'name = platform:id, ...' "
                    "(names: lowercase letters, digits, - and _)"
                )
            if name in KNOWN_PLATFORMS:
                # Reserved: "@line" routes to an app, so "@name" can only ever
                # mean a person if no person shares an app's name.
                raise ValueError(f"people: {name!r} is a platform name and cannot be a person")
            if any(p.name == name for p in people):
                raise ValueError(f"people: {name!r} is defined twice")

            idents_part, arrow, deliver = rest.partition(">")
            identities: set[str] = set()
            for raw in idents_part.split(","):
                if not raw.strip():
                    continue
                identity = parse_identity(raw, field=f"people ({name})")
                platform = identity.partition(":")[0]
                if any(i.startswith(f"{platform}:") for i in identities):
                    raise ValueError(f"people: {name!r} has two {platform} identities")
                if identity in owner:
                    raise ValueError(
                        f"people: {identity} is in both {owner[identity]!r} and {name!r}; "
                        "an identity can belong to only one person"
                    )
                owner[identity] = name
                identities.add(identity)
            if not identities:
                raise ValueError(f"people: {name!r} has no identities")

            deliver_on: str | None = None
            if arrow:
                deliver_on = deliver.strip().lower()
                if not any(i.startswith(f"{deliver_on}:") for i in identities):
                    raise ValueError(
                        f"people: {name!r} delivers on {deliver_on!r} but has no "
                        f"{deliver_on} identity"
                        if deliver_on in KNOWN_PLATFORMS
                        else f"people: {name!r} delivers on unknown platform {deliver_on!r}"
                    )
            people.append(Person(name, frozenset(identities), deliver_on))
        return tuple(people)

    @field_validator("line_webhook_path")
    @classmethod
    def _validate_webhook_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"line_webhook_path must start with '/', got {v!r}")
        return v

    @field_validator(
        "telegram_token",
        "discord_token",
        "line_channel_secret",
        "line_channel_access_token",
        mode="before",
    )
    @classmethod
    def _blank_token_means_unset(cls, v: object) -> object:
        """``REMINDARR_TELEGRAM_TOKEN=`` with nothing after it is "not configured".

        pydantic would otherwise read it as the empty string, enabling the
        platform with an empty token -- and ``.env.example`` ships every token
        blank, so anyone who fills in only one platform would hit this.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _line_needs_both_credentials(self) -> Settings:
        has_secret = self.line_channel_secret is not None
        has_token = self.line_channel_access_token is not None
        if has_secret != has_token:
            present, missing = (
                ("LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN")
                if has_secret
                else ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET")
            )
            raise ValueError(
                f"REMINDARR_{present} is set but REMINDARR_{missing} is not. "
                "LINE needs both (or neither, to leave it disabled)."
            )
        return self

    @model_validator(mode="after")
    def _people_are_allowed_and_deliverable(self) -> Settings:
        """Being in a person does not grant access: the allow-list stays the one
        place that decides who may use the bot. And a default delivery app
        that is not running would silently strand every reminder."""
        enabled = self.enabled_platforms
        for person in self.people:
            for identity in sorted(person.identities):
                if identity not in self.allowed_users:
                    raise ValueError(
                        f"people: {person.name!r} includes {identity}, which is not in "
                        "REMINDARR_ALLOWED_USERS (being in a person does not grant access)"
                    )
            if person.deliver_on is not None and person.deliver_on not in enabled:
                raise ValueError(
                    f"people: {person.name!r} delivers on {person.deliver_on}, "
                    "which is not enabled (no token configured)"
                )
        return self

    @property
    def enabled_platforms(self) -> list[str]:
        enabled = []
        if self.telegram_token is not None:
            enabled.append("telegram")
        if self.discord_token is not None:
            enabled.append("discord")
        if self.line_enabled:
            enabled.append("line")
        return enabled

    def is_allowed(self, platform: str, user_id: str) -> bool:
        return f"{platform.lower()}:{user_id}" in self.allowed_users

    def person_of(self, platform: str, user_id: str) -> Person | None:
        identity = f"{platform.lower()}:{user_id}"
        return next((p for p in self.people if identity in p.identities), None)

    @property
    def line_enabled(self) -> bool:
        return self.line_channel_secret is not None and self.line_channel_access_token is not None
