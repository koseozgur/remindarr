"""Configuration, from environment or a ``.env`` file.

An adapter is enabled by having a token. There is no separate "enable" flag to
get out of sync with whether the credential is actually present. LINE needs two
credentials, so it is enabled by having both -- and having only one is a
startup error rather than a silently disabled platform.
"""

from __future__ import annotations

import zoneinfo
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

KNOWN_PLATFORMS = frozenset({"telegram", "discord", "line"})
"""Platform names as adapters report them in ``Sender.platform``."""


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

        The platform is case-insensitive (normalised to lowercase, as adapters
        report it); the user id is kept exactly, because LINE ids are
        case-sensitive. Empty entries, e.g. from a trailing comma, are skipped.
        """
        if v is None:
            return frozenset()
        if isinstance(v, str):
            raw = v.split(",")
        elif isinstance(v, (list, tuple, set, frozenset)):
            raw = list(v)
        else:
            return v  # let pydantic reject it with its own message

        entries: set[str] = set()
        for item in raw:
            entry = str(item).strip()
            if not entry:
                continue
            platform, sep, user_id = entry.partition(":")
            platform, user_id = platform.strip().lower(), user_id.strip()
            if not sep or not platform or not user_id or any(c.isspace() for c in user_id):
                raise ValueError(
                    f"allowed_users entry {entry!r} is not platform:user_id "
                    "(e.g. line:U1234abcd or discord:123456789)"
                )
            if platform not in KNOWN_PLATFORMS:
                known = ", ".join(sorted(KNOWN_PLATFORMS))
                raise ValueError(
                    f"allowed_users entry {entry!r} has unknown platform {platform!r} "
                    f"(known: {known})"
                )
            entries.add(f"{platform}:{user_id}")
        return frozenset(entries)

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

    @property
    def line_enabled(self) -> bool:
        return self.line_channel_secret is not None and self.line_channel_access_token is not None
