"""Regenerate ``docs/demo.svg``, the terminal transcript in the README.

Run from the repo root:

    uv run python scripts/gen_demo.py

The transcript is produced by driving the **real** Router and Scheduler over a
real SQLite database -- only the network is stubbed. That means the image in
the README cannot drift away from what the code actually says, which is the
whole point of generating it rather than pasting a screenshot.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from remindarr.adapters.base import DeliveryRegistry
from remindarr.config import Settings
from remindarr.core.models import Base
from remindarr.core.router import Router
from remindarr.core.scheduler import Scheduler
from remindarr.core.types import IncomingMessage, OutgoingMessage, Route, Sender

OUT = Path("docs/demo.svg")
TZ = ZoneInfo("Asia/Tokyo")
START = datetime(2026, 8, 18, 10, 30, tzinfo=TZ)

# GitHub's dark palette, so the image sits comfortably on the repo page.
BG = "#0d1117"
CHROME = "#161b22"
BORDER = "#30363d"
FG = "#c9d1d9"
DIM = "#6e7681"
USER = "#79c0ff"
USER_TXT = "#e6edf3"
BOT = "#3fb950"
ALARM = "#d29922"
NOTE = "#8b949e"

FONT = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"
FS = 13.5
LH = 21.0
PAD_X = 22.0
CHROME_H = 38.0
PAD_TOP = 14.0
PAD_BOT = 18.0
WIDTH = 900.0
GUTTER = 52.0


class Clock:
    """A clock the script advances by hand, so "three days later" is instant."""

    def __init__(self, start: datetime) -> None:
        self.now = start.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class Recorder:
    """A delivery adapter that records instead of sending."""

    platform = "telegram"

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)


def incoming(text: str) -> IncomingMessage:
    return IncomingMessage(
        sender=Sender("telegram", "demo", "demo"),
        route=Route("telegram", "c1"),
        text=text,
        received_at=datetime.now(UTC),
    )


async def transcript() -> list[tuple[str, str]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    clock = Clock(START)
    settings = Settings(
        database_url="sqlite+aiosqlite://",
        default_timezone="Asia/Tokyo",
        allowed_users=frozenset({"telegram:demo"}),
    )
    router = Router(settings=settings, session_factory=factory, clock=clock)
    recorder = Recorder()
    registry = DeliveryRegistry()
    registry.register(recorder)
    scheduler = Scheduler(
        settings=settings, session_factory=factory, registry=registry, clock=clock
    )

    lines: list[tuple[str, str]] = []

    async def say(text: str) -> None:
        lines.append(("user", text))
        reply = await router.handle(incoming(text))
        if reply is not None:
            for i, line in enumerate(reply.text.rstrip().split("\n")):
                if not line.strip():
                    continue
                if i == 0:
                    kind = "bot"
                elif line.startswith("note:"):
                    kind = "note"
                else:
                    kind = "cont"
                lines.append((kind, line))
        lines.append(("gap", ""))

    async def tick() -> None:
        before = len(recorder.sent)
        await scheduler.tick()
        for message in recorder.sent[before:]:
            stamp = f"{clock.now.astimezone(TZ):%a %H:%M}"
            lines.append(("push", f"{stamp}   → {message.route.platform}"))
            lines.extend(("pushb", line) for line in message.text.rstrip().split("\n"))
            lines.append(("gap", ""))

    lines.append(("dim", "$ remindarr"))
    lines.append(
        ("dim", "  remindarr.ready  capture=[telegram, discord]  delivery=[telegram, discord]")
    )
    lines.append(("gap", ""))

    await say("call the dentist tomorrow at 9")
    await say("remind me to check the burn-in in 45 minutes")
    await say("stand-up call at 9")
    await say("call mom about flight 320")

    lines.append(("sep", "time passes"))
    lines.append(("gap", ""))
    clock.advance(minutes=46)
    await tick()

    lines.append(("sep", "process down for 3 days"))
    lines.append(("gap", ""))
    clock.advance(days=3)
    await tick()

    await engine.dispose()
    return lines


def text_el(x: float, y: float, fill: str, body: str, extra: str = "") -> str:
    return f'<text x="{x:.0f}" y="{y:.0f}" fill="{fill}"{extra}>{escape(body)}</text>'


def render(lines: list[tuple[str, str]]) -> str:
    height = CHROME_H + PAD_TOP + LH * len(lines) + PAD_BOT
    mid = WIDTH / 2
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {WIDTH:.0f} {height:.0f}" font-family="{FONT}" font-size="{FS}">',
        f'<rect x="0" y="0" width="{WIDTH:.0f}" height="{height:.0f}" rx="10" '
        f'fill="{BG}" stroke="{BORDER}"/>',
        f'<path d="M0 10 a10 10 0 0 1 10 -10 h{WIDTH - 20:.0f} a10 10 0 0 1 10 10 '
        f'v{CHROME_H - 10:.0f} h-{WIDTH:.0f} z" fill="{CHROME}"/>',
        f'<line x1="0" y1="{CHROME_H:.0f}" x2="{WIDTH:.0f}" y2="{CHROME_H:.0f}" '
        f'stroke="{BORDER}"/>',
    ]
    for i, colour in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        out.append(f'<circle cx="{22 + i * 18}" cy="{CHROME_H / 2:.0f}" r="5.5" fill="{colour}"/>')
    out.append(
        text_el(
            mid,
            CHROME_H / 2 + 4.5,
            DIM,
            "remindarr — telegram",
            ' text-anchor="middle" font-size="12"',
        )
    )

    y = CHROME_H + PAD_TOP + FS
    for kind, text in lines:
        if kind == "gap":
            y += LH
            continue

        if kind == "sep":
            cy = y - FS / 2 - 1
            half = (len(text) * 7.2 + 20) / 2
            out.append(
                f'<line x1="{PAD_X:.0f}" y1="{cy:.0f}" x2="{mid - half:.0f}" y2="{cy:.0f}" '
                f'stroke="{BORDER}"/>'
            )
            out.append(
                f'<line x1="{mid + half:.0f}" y1="{cy:.0f}" x2="{WIDTH - PAD_X:.0f}" '
                f'y2="{cy:.0f}" stroke="{BORDER}"/>'
            )
            out.append(
                text_el(
                    mid,
                    y - 3,
                    DIM,
                    text,
                    ' text-anchor="middle" font-size="11.5" font-style="italic"',
                )
            )
        elif kind in ("user", "bot", "push"):
            label, colour = {
                "user": ("you", USER),
                "bot": ("bot", BOT),
                "push": ("PUSH", ALARM),
            }[kind]
            out.append(text_el(PAD_X, y, colour, label, ' font-weight="600"'))
            if kind != "push":
                out.append(f'<text x="{PAD_X + 34:.0f}" y="{y:.0f}" fill="{DIM}">&#9656;</text>')
            body_colour = {"user": USER_TXT, "bot": FG, "push": DIM}[kind]
            out.append(text_el(PAD_X + GUTTER, y, body_colour, text))
        elif kind == "pushb":
            out.append(text_el(PAD_X + GUTTER, y, ALARM, text))
        elif kind == "note":
            out.append(text_el(PAD_X + GUTTER, y, NOTE, text))
        elif kind == "cont":
            out.append(text_el(PAD_X + GUTTER, y, DIM, text))
        elif kind == "dim":
            out.append(text_el(PAD_X, y, DIM, text))
        y += LH

    out.append("</svg>")
    return "\n".join(out)


def main() -> None:
    lines = asyncio.run(transcript())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(lines), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
