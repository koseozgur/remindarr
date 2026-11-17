"""Japanese replies, for messages written in Japanese.

Deliberately small. The language is read off the message itself -- any kana
or kanji means Japanese -- so there is nothing to store and nothing to set:
"明日9時に歯医者" gets a Japanese confirmation, "/list" stays English. Only
the replies to setting a reminder (and the refusal to a stranger) are
translated, because the confirmation is the one message that has to be
understood: it states the resolved time, which is how a misread shows.

Everything here mirrors an English function elsewhere (``humanize``,
``Series.describe``, ``delivery.disclosure``), and covers the same cases.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from .recurrence import Series

Lang = Literal["en", "ja"]

# Hiragana, katakana (and the long-vowel mark), CJK ideographs, and the
# full-width forms Japanese IMEs produce for digits and punctuation.
_JAPANESE = re.compile(r"[぀-ヿ㐀-䶿一-鿿！-～]")

_WEEKDAY = "月火水木金土日"
_BYDAY = {"MO": "月", "TU": "火", "WE": "水", "TH": "木", "FR": "金", "SA": "土", "SU": "日"}


def language_of(text: str) -> Lang:
    return "ja" if _JAPANESE.search(text) else "en"


def _date(local: datetime, now_local: datetime) -> str:
    year = "" if local.year == now_local.year else f"{local.year}年"
    return f"{year}{local.month}月{local.day}日({_WEEKDAY[local.weekday()]})"


def format_when(due_utc: datetime, tz: ZoneInfo, *, now_utc: datetime) -> str:
    """ "明日 9:00（14時間後）", like ``humanize.format_when``."""
    local = due_utc.astimezone(tz)
    now_local = now_utc.astimezone(tz)
    days = (local.date() - now_local.date()).days
    if days == 0:
        day = "今日"
    elif days == 1:
        day = "明日"
    elif days == 2:
        day = "明後日"
    elif 3 <= days < 7:
        day = f"{_WEEKDAY[local.weekday()]}曜日"
    else:
        day = _date(local, now_local)
    return f"{day} {local:%-H:%M}（{format_delta(due_utc - now_utc)}）"


def format_next(due_utc: datetime, tz: ZoneInfo, *, now_utc: datetime) -> str:
    """Always the absolute date, like ``humanize.format_next``."""
    local = due_utc.astimezone(tz)
    when = _date(local, now_utc.astimezone(tz))
    return f"{when} {local:%-H:%M}（{format_delta(due_utc - now_utc)}）"


def format_delta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    suffix = "前" if seconds < 0 else "後"
    seconds = abs(seconds)
    if seconds < 60:
        text = f"{seconds}秒"
    elif seconds < 3600:
        text = f"{seconds // 60}分"
    elif seconds < 86400:
        text = f"{seconds // 3600}時間"
    elif seconds < 86400 * 14:
        text = f"{seconds // 86400}日"
    else:
        text = f"{seconds // (86400 * 7)}週間"
    return text + suffix


def describe(series: Series) -> str:
    """ "毎日 8:00", "毎週月・木曜 8:00", ... -- ``Series.describe`` in Japanese."""
    parts = dict(p.split("=", 1) for p in series.rule.split(";") if "=" in p)
    freq = parts.get("FREQ", "")
    n = int(parts.get("INTERVAL", "1"))
    at = ""
    if "BYHOUR" in parts:
        at = f" {int(parts['BYHOUR'])}:{int(parts.get('BYMINUTE', '0')):02d}"
    day = max((int(d) for d in parts.get("BYMONTHDAY", "1").split(",")), default=1)
    clamp = "（ない月は月末）" if "BYSETPOS" in parts else ""

    if freq == "HOURLY":
        return "毎時" if n == 1 else f"{n}時間ごと"
    if freq == "DAILY":
        return ("毎日" if n == 1 else f"{n}日ごと") + at
    if freq == "WEEKLY":
        days = parts.get("BYDAY", "").split(",")
        if n == 1 and days == ["MO", "TU", "WE", "TH", "FR"]:
            return "平日" + at
        if n == 1 and days == ["SA", "SU"]:
            return "週末" + at
        listed = "・".join(_BYDAY.get(d, d) for d in days) + "曜"
        return (f"毎週{listed}" if n == 1 else f"{n}週間ごとの{listed}") + at
    if freq == "MONTHLY":
        return ("毎月" if n == 1 else f"{n}か月ごとの") + f"{day}日{clamp}{at}"
    if freq == "YEARLY":
        month = int(parts.get("BYMONTH", "1"))
        return ("毎年" if n == 1 else f"{n}年ごとの") + f"{month}月{day}日{clamp}{at}"
    return "繰り返し"


AI_NOTE = "AIで読み取りました。上の日時が合っているか確認してください"

NOT_UNDERSTOOD = (
    "すみません、いつお知らせすればいいか分かりませんでした。\n\n"
    "例えば「明日の9時に歯医者に電話」のように、内容と日時を書いてください。"
)

NOT_AUTHORISED = (
    "このボットはまだ使えません。招待コードをお持ちなら、/join に続けてコードを送ってください。\n"
    "(For the owner: {identity})"
)


def confirmation(text: str, when: str, reminder_id: int) -> list[str]:
    return [f"了解です。「{text}」を{when}にお知らせします。", f"id {reminder_id}"]


def confirmation_series(text: str, pattern: str, nxt: str, reminder_id: int) -> list[str]:
    return [f"了解です。「{text}」を{pattern}にお知らせします。次回: {nxt}", f"id {reminder_id}"]


def disclosure(*, recipient: str | None, by: str, app: str, kind: str) -> str:
    """``delivery.disclosure`` in Japanese; ``kind`` says which case it is."""
    if recipient is not None:
        return f"{recipient}さんに{app}のメッセージで届きます（「{by}から」と表示されます）。"
    if kind == "dm_here":
        return "時間になったらDMでお知らせします。"
    if kind == "other_chat":
        return "別のチャットに届きます。"
    return f"{app}のメッセージで届きます。"
