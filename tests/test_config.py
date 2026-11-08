"""Config tests, mostly about which platforms count as enabled."""

from __future__ import annotations

from pathlib import Path

import pytest

from remindarr.config import Settings

TOKEN_VARS = ("REMINDARR_TELEGRAM_TOKEN", "REMINDARR_DISCORD_TOKEN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # A token exported in the developer's shell must not leak into these tests.
    for var in TOKEN_VARS:
        monkeypatch.delenv(var, raising=False)


def test_no_tokens_means_no_platforms() -> None:
    assert Settings(_env_file=None).enabled_platforms == []


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_token_is_treated_as_unset(blank: str) -> None:
    settings = Settings(_env_file=None, telegram_token=blank, discord_token="real")
    assert settings.telegram_token is None
    assert settings.enabled_platforms == ["discord"]


def test_blank_token_in_env_file_is_treated_as_unset(tmp_path: Path) -> None:
    """Regression: ``.env.example`` ships every token as ``KEY=`` with nothing
    after it. Copying it and filling in only Discord used to enable Telegram
    with an empty token, and startup crashed inside the Telegram library."""
    env = tmp_path / ".env"
    env.write_text("REMINDARR_TELEGRAM_TOKEN=\nREMINDARR_DISCORD_TOKEN=abc123\n")
    settings = Settings(_env_file=env)
    assert settings.enabled_platforms == ["discord"]


def test_real_token_enables_platform() -> None:
    settings = Settings(_env_file=None, telegram_token="123:abc")
    assert settings.enabled_platforms == ["telegram"]


def test_unknown_timezone_rejected() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        Settings(_env_file=None, default_timezone="Mars/Olympus_Mons")
