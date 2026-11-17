"""Japanese replies to messages written in Japanese."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from remindarr.config import Settings
from remindarr.core import i18n
from remindarr.core.router import Router
from remindarr.core.timeparse import parse

from .conftest import NOW, TZ, message


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("call mum tomorrow at 9", "en"),
        ("明日9時に歯医者", "ja"),
        ("カレー", "ja"),
        ("ひらがな", "ja"),
        ("dentist 明日", "ja"),
        ("/list", "en"),
        ("café at 9", "en"),
        ("１２３", "ja"),  # full-width digits only come from a Japanese IME
    ],
)
def test_language_of(text: str, lang: str) -> None:
    assert i18n.language_of(text) == lang


class TestFormatting:
    def test_when(self) -> None:
        def when(delta: timedelta) -> str:
            return i18n.format_when(NOW + delta, TZ, now_utc=NOW)

        # NOW is Tuesday 18 Aug 2026, 10:30 in Tokyo.
        assert when(timedelta(hours=2)) == "今日 12:30（2時間後）"
        assert when(timedelta(days=1)) == "明日 10:30（1日後）"
        assert when(timedelta(days=2)) == "明後日 10:30（2日後）"
        assert when(timedelta(days=3)) == "金曜日 10:30（3日後）"
        assert when(timedelta(days=10)) == "8月28日(金) 10:30（10日後）"
        assert when(timedelta(days=200)).startswith("2027年3月6日(土)")

    def test_delta(self) -> None:
        assert i18n.format_delta(timedelta(seconds=30)) == "30秒後"
        assert i18n.format_delta(timedelta(minutes=-5)) == "5分前"
        assert i18n.format_delta(timedelta(days=21)) == "3週間後"


def series_of(text: str) -> Any:
    from remindarr.core.recurrence import Series

    parsed = parse(text, now_utc=NOW, tz=TZ)
    assert parsed.recurrence is not None
    return Series.parse(parsed.recurrence)


@pytest.mark.parametrize(
    ("english", "japanese"),
    [
        ("pills every day at 8", "毎日 8:00"),
        ("standup every weekday at 9:30", "平日 9:30"),
        ("bins every monday at 7pm", "毎週月曜 19:00"),
        ("gym every 2 weeks on friday at 7am", "2週間ごとの金曜 7:00"),
        ("rent every month on the 31st at 9", "毎月31日（ない月は月末） 9:00"),
        ("water plants every 3 hours", "3時間ごと"),
        ("birthday every year on 1 september at 9", "毎年9月1日 9:00"),
    ],
)
def test_describe_matches_the_english(english: str, japanese: str) -> None:
    assert i18n.describe(series_of(english)) == japanese


class TestRouter:
    @pytest.fixture
    def router(
        self, settings: Settings, session_factory: async_sessionmaker[AsyncSession]
    ) -> Router:
        return Router(settings=settings, session_factory=session_factory, clock=lambda: NOW)

    async def test_japanese_task_gets_a_japanese_confirmation(self, router: Router) -> None:
        # The grammar reads the English time; the task is Japanese.
        reply = await router.handle(message("歯医者に電話 tomorrow at 9"))
        assert reply is not None
        assert reply.text.startswith("了解です。「歯医者に電話」を明日 9:00（")
        assert "id 1" in reply.text

    async def test_japanese_repeat(self, router: Router) -> None:
        reply = await router.handle(message("薬 every day at 8"))
        assert reply is not None
        assert "「薬」を毎日 8:00にお知らせします。次回: 8月19日(水) 8:00" in reply.text

    async def test_not_understood_in_japanese(self, router: Router) -> None:
        # No AI reader configured: the grammar's refusal, in Japanese.
        reply = await router.handle(message("明日の朝に牛乳を買う"))
        assert reply is not None and reply.text == i18n.NOT_UNDERSTOOD

    async def test_english_is_unchanged(self, router: Router) -> None:
        reply = await router.handle(message("call mum tomorrow at 9"))
        assert reply is not None and reply.text.startswith("Got it — “call mum” tomorrow at 09:00")

    async def test_stranger_writing_japanese(self, router: Router) -> None:
        reply = await router.handle(message("こんにちは", user_id="stranger"))
        assert reply is not None
        assert reply.text.startswith("このボットはまだ使えません")
        assert "telegram:stranger" in reply.text
