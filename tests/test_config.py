"""Config tests, mostly about which platforms count as enabled."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from remindarr.config import Settings

TOKEN_VARS = (
    "REMINDARR_TELEGRAM_TOKEN",
    "REMINDARR_DISCORD_TOKEN",
    "REMINDARR_LINE_CHANNEL_SECRET",
    "REMINDARR_LINE_CHANNEL_ACCESS_TOKEN",
)


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


class TestLine:
    def test_both_credentials_enable_line(self) -> None:
        settings = Settings(_env_file=None, line_channel_secret="s", line_channel_access_token="t")
        assert settings.enabled_platforms == ["line"]

    def test_neither_credential_leaves_line_disabled(self) -> None:
        assert not Settings(_env_file=None).line_enabled

    @pytest.mark.parametrize(
        ("given", "missing"),
        [
            ({"line_channel_secret": "s"}, "REMINDARR_LINE_CHANNEL_ACCESS_TOKEN"),
            ({"line_channel_access_token": "t"}, "REMINDARR_LINE_CHANNEL_SECRET"),
        ],
    )
    def test_exactly_one_credential_is_a_config_error(
        self, given: dict[str, str], missing: str
    ) -> None:
        """Half-configured is almost certainly a typo, not a wish to disable
        LINE -- so it fails at startup rather than silently doing nothing."""
        with pytest.raises(ValidationError, match=f"{missing} is not"):
            Settings(_env_file=None, **given)  # type: ignore[arg-type]

    def test_blank_credential_counts_as_unset(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text(
            "REMINDARR_LINE_CHANNEL_SECRET=\nREMINDARR_LINE_CHANNEL_ACCESS_TOKEN=\n"
            "REMINDARR_DISCORD_TOKEN=abc\n"
        )
        assert Settings(_env_file=env).enabled_platforms == ["discord"]

    def test_one_real_one_blank_is_still_an_error(self) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, line_channel_secret="s", line_channel_access_token="  ")

    def test_webhook_defaults(self) -> None:
        settings = Settings(_env_file=None)
        assert (settings.webhook_host, settings.webhook_port, settings.line_webhook_path) == (
            "0.0.0.0",
            8080,
            "/line/webhook",
        )

    def test_webhook_path_must_be_absolute(self) -> None:
        with pytest.raises(ValidationError, match="must start with"):
            Settings(_env_file=None, line_webhook_path="line/webhook")

    def test_build_adapters_includes_line(self) -> None:
        from remindarr.adapters.line import LineAdapter
        from remindarr.app import build_adapters

        settings = Settings(_env_file=None, line_channel_secret="s", line_channel_access_token="t")
        [adapter] = build_adapters(settings)
        assert isinstance(adapter, LineAdapter)
