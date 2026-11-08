"""Where a new reminder should be delivered.

The default is where it was set. Three things change it:

* **The capture adapter can ask for private delivery** of reminders set in a
  shared chat (``IncomingMessage.deliver_privately``). Discord does, for
  server channels: the reminder goes to the setter's DM, with the channel kept
  as a fallback in case the DM is refused. Where an adapter does not ask --
  LINE groups, say -- a reminder set in a group is for the group.
* **A person's default app**, from ``REMINDARR_PEOPLE``, applied only to
  reminders set in a one-to-one chat.
* **An explicit ``@app`` override** in the message (``@discord``, ``@line``,
  ``@telegram``), which always means "to me, directly, on that app".

The override is a sigil rather than words on purpose. "post the update on
discord tomorrow" is a real task, and reading "on discord" as an instruction
would silently eat part of it -- exactly the failure the narrow time grammar
exists to avoid. Only the platform words are acted on; any other ``@word``
stays in the task text untouched, which keeps ``@name`` free to mean a person
later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from remindarr.config import KNOWN_PLATFORMS, PLATFORM_NAMES, Settings

from .types import IncomingMessage, Route

_AT_WORD = re.compile(r"(?<!\S)@([A-Za-z][\w-]*)(?!\S)")


@dataclass(frozen=True, slots=True)
class Delivery:
    text: str
    """The message text with any ``@app`` override removed, ready to parse."""

    route: Route
    fallback: Route | None = None
    """Where to deliver if ``route`` (a direct message) is refused."""


@dataclass(frozen=True, slots=True)
class Refusal:
    reason: str


@dataclass(frozen=True, slots=True)
class AtTokens:
    """What the ``@`` tokens in a message ask for. The one place they are read."""

    text: str
    """The text with the acted-on tokens removed; every other ``@word`` kept."""

    apps: tuple[str, ...]
    """Platform names asked for, lowercase, in order of appearance."""


def read_at_tokens(text: str) -> AtTokens:
    """Find ``@line`` / ``@discord`` / ``@telegram``, case-insensitively.

    Nothing else is touched: ``email @john about it`` keeps ``@john``. A
    future ``@person`` resolver belongs here, next to the app words.
    """
    apps: list[str] = []
    kept: list[str] = []
    last = 0
    for match in _AT_WORD.finditer(text):
        word = match.group(1).lower()
        if word in KNOWN_PLATFORMS:
            apps.append(word)
            kept.append(text[last : match.start()])
            last = match.end()
    kept.append(text[last:])
    return AtTokens(" ".join("".join(kept).split()), tuple(apps))


def resolve(message: IncomingMessage, text: str, settings: Settings) -> Delivery | Refusal:
    tokens = read_at_tokens(text)
    if len(tokens.apps) > 1:
        apps = " and ".join(f"@{a}" for a in tokens.apps)
        return Refusal(f"Pick one app to deliver on — I found {apps}.")
    if not tokens.apps:
        return _default(message, text, settings)

    target = tokens.apps[0]
    name = PLATFORM_NAMES[target]
    if target not in settings.enabled_platforms:
        return Refusal(f"{name} isn't set up here, so I can't deliver there.")

    sender = message.sender
    if target == sender.platform:
        user_id: str | None = sender.user_id
    else:
        person = settings.person_of(sender.platform, sender.user_id)
        user_id = person.user_id_on(target) if person is not None else None
    if user_id is None:
        return Refusal(
            f"I don't know who you are on {name}. Ask the owner to link your {name} "
            "account to this one in REMINDARR_PEOPLE."
        )

    if target == sender.platform:
        if message.direct:
            return Delivery(tokens.text, message.route)  # already the 1:1 chat
        fallback = message.route if message.deliver_privately else None
        return Delivery(tokens.text, Route.direct_to(target, user_id), fallback)
    return Delivery(tokens.text, Route.direct_to(target, user_id))


def _default(message: IncomingMessage, text: str, settings: Settings) -> Delivery:
    sender = message.sender
    if not message.direct:
        if message.deliver_privately:
            return Delivery(text, Route.direct_to(sender.platform, sender.user_id), message.route)
        return Delivery(text, message.route)

    person = settings.person_of(sender.platform, sender.user_id)
    if person is None or person.deliver_on in (None, sender.platform):
        return Delivery(text, message.route)
    assert person.deliver_on is not None
    user_id = person.user_id_on(person.deliver_on)
    # Settings guarantees a person has an identity on their default app.
    assert user_id is not None
    return Delivery(text, Route.direct_to(person.deliver_on, user_id))


def disclosure(route: Route, here: Route) -> str | None:
    """What to tell the user when the reminder will not arrive where they are."""
    if route == here:
        return None
    name = PLATFORM_NAMES.get(route.platform, route.platform)
    if route.platform == here.platform and route.is_direct_address:
        return "I'll DM you when it's due."
    if route.platform == here.platform:
        return "It'll arrive in another chat."
    return f"It'll arrive as a direct message on {name}."


def label(route: Route, here: Route) -> str:
    """A suffix for ``/list`` rows delivered somewhere other than this chat."""
    if route == here:
        return ""
    name = PLATFORM_NAMES.get(route.platform, route.platform)
    if route.is_direct_address:
        return f" → {name} DM"
    return f" → {name}" if route.platform != here.platform else " → another chat"
