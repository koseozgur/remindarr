"""Where a new reminder should be delivered, and to whom.

The default is where it was set. Four things change it:

* **The capture adapter can ask for private delivery** of reminders set in a
  shared chat (``IncomingMessage.deliver_privately``). Discord does, for
  server channels: the reminder goes to the setter's DM, with the channel kept
  as a fallback in case the DM is refused. Where an adapter does not ask --
  LINE groups, say -- a reminder set in a group is for the group.
* **A person's default app**, from ``REMINDARR_PEOPLE``, applied only to
  reminders set in a one-to-one chat.
* **An explicit ``@app`` override** in the message (``@discord``, ``@line``,
  ``@telegram``), which always means "directly, on that app".
* **A ``@person``** -- a name from ``REMINDARR_PEOPLE``, or on Discord a real
  ``<@id>`` mention of someone in it -- sends the reminder to that person
  instead. The setter still owns it: it is in their ``/list``, and only they
  can cancel it.

Overrides are sigils rather than words on purpose. "post the update on
discord tomorrow" is a real task, and reading "on discord" as an instruction
would silently eat part of it -- exactly the failure the narrow time grammar
exists to avoid. Only tokens that resolve are acted on; any other ``@word``
stays in the task text untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from remindarr.config import KNOWN_PLATFORMS, PLATFORM_NAMES, Person, Settings

from .types import IncomingMessage, Route, Sender

_AT_WORD = re.compile(r"(?<!\S)@([A-Za-z][\w-]*)(?!\S)")
# Discord delivers a real mention as <@id> (or <@!id> from older clients).
_DISCORD_MENTION = re.compile(r"(?<!\S)<@!?(\d+)>(?!\S)")


@dataclass(frozen=True, slots=True)
class Delivery:
    text: str
    """The message text with any resolved ``@`` tokens removed, ready to parse."""

    route: Route
    fallback: Route | None = None
    """Where to deliver if ``route`` (a direct message) is refused."""

    recipient: str | None = None
    """The person it is for, when that is not the setter."""


@dataclass(frozen=True, slots=True)
class Refusal:
    reason: str


@dataclass(frozen=True, slots=True)
class AtTokens:
    """What the ``@`` tokens in a message ask for. The one place they are read."""

    text: str
    """The text with the resolved tokens removed; every other ``@word`` kept."""

    apps: tuple[str, ...]
    """Platform names asked for, lowercase, in order of appearance."""

    people: tuple[Person, ...] = ()
    """People named, deduplicated, in order of appearance."""


def read_at_tokens(text: str, settings: Settings, *, platform: str) -> AtTokens:
    """Resolve ``@app``, ``@name`` and (on Discord) ``<@id>`` tokens.

    Platform words win over names: config refuses a person named ``line``, so
    there is never a clash. A token that resolves to nothing -- ``@john``, or
    a mention of someone not in ``REMINDARR_PEOPLE`` -- stays as text.
    """
    by_name = {p.name: p for p in settings.people}
    spans: list[tuple[int, int]] = []
    apps: list[str] = []
    people: list[Person] = []

    for match in _AT_WORD.finditer(text):
        word = match.group(1).lower()
        if word in KNOWN_PLATFORMS:
            apps.append(word)
        elif word in by_name:
            people.append(by_name[word])
        else:
            continue
        spans.append(match.span())

    if platform == "discord":
        for match in _DISCORD_MENTION.finditer(text):
            person = settings.person_of("discord", match.group(1))
            if person is not None:
                people.append(person)
                spans.append(match.span())

    kept: list[str] = []
    last = 0
    for start, end in sorted(spans):
        kept.append(text[last:start])
        last = end
    kept.append(text[last:])
    unique = tuple(dict.fromkeys(people))  # same person named twice is one person
    return AtTokens(" ".join("".join(kept).split()), tuple(apps), unique)


def resolve(message: IncomingMessage, text: str, settings: Settings) -> Delivery | Refusal:
    sender = message.sender
    tokens = read_at_tokens(text, settings, platform=sender.platform)
    if len(tokens.apps) > 1:
        apps = " and ".join(f"@{a}" for a in tokens.apps)
        return Refusal(f"Pick one app to deliver on — I found {apps}.")

    me = settings.person_of(sender.platform, sender.user_id)
    others = [p for p in tokens.people if p != me]  # "@self" is a normal reminder
    if len(others) > 1:
        names = " and ".join(f"@{p.name}" for p in others)
        return Refusal(f"One person per reminder for now — I found {names}. Set one for each.")
    if others:
        return _for_someone_else(message, tokens, others[0], settings)

    if not tokens.apps:
        return _default(message, tokens.text, settings)

    target = tokens.apps[0]
    refused = _unavailable(target, settings)
    if refused is not None:
        return refused

    if target == sender.platform:
        user_id: str | None = sender.user_id
    else:
        user_id = me.user_id_on(target) if me is not None else None
    name = PLATFORM_NAMES[target]
    if user_id is None:
        return Refusal(
            f"I don't know who you are on {name}. Ask the owner to link your {name} "
            "account to this one in REMINDARR_PEOPLE."
        )

    if target == sender.platform:
        if message.direct:
            return Delivery(tokens.text, message.route)  # already the 1:1 chat
        return Delivery(tokens.text, Route.direct_to(target, user_id), _fallback(message, target))
    return Delivery(tokens.text, Route.direct_to(target, user_id))


def _for_someone_else(
    message: IncomingMessage, tokens: AtTokens, person: Person, settings: Settings
) -> Delivery | Refusal:
    platforms = sorted(i.partition(":")[0] for i in person.identities)
    if tokens.apps:
        target = tokens.apps[0]
    elif person.deliver_on is not None:
        target = person.deliver_on
    elif len(platforms) == 1:
        target = platforms[0]
    else:
        options = " or ".join(f"@{p}" for p in platforms)
        return Refusal(f"{person.name} has several apps; add {options} to pick one.")

    user_id = person.user_id_on(target)
    if user_id is None:
        return Refusal(f"{person.name} isn't on {PLATFORM_NAMES[target]}.")
    refused = _unavailable(target, settings)
    if refused is not None:
        return refused
    return Delivery(
        tokens.text,
        Route.direct_to(target, user_id),
        _fallback(message, target),
        recipient=person.name,
    )


def _fallback(message: IncomingMessage, target: str) -> Route | None:
    """The capture chat, if a refused DM may be redirected there.

    Only a *shared* chat on the same app qualifies: falling back into the
    setter's own DM would deliver someone else's reminder where they cannot
    see it.
    """
    if target == message.sender.platform and message.deliver_privately and not message.direct:
        return message.route
    return None


def _unavailable(target: str, settings: Settings) -> Refusal | None:
    if target in settings.enabled_platforms:
        return None
    return Refusal(f"{PLATFORM_NAMES[target]} isn't set up here, so I can't deliver there.")


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


def setter_name(settings: Settings, platform: str, user_id: str, display_name: str) -> str:
    """How a reminder's recipient is told who set it: "(from oz)"."""
    person = settings.person_of(platform, user_id)
    if person is not None:
        return person.name
    return display_name or f"someone on {PLATFORM_NAMES.get(platform, platform)}"


def disclosure(target: Delivery, here: Route, settings: Settings, sender: Sender) -> str | None:
    """What to tell the setter when the reminder will not arrive where they are."""
    route = target.route
    name = PLATFORM_NAMES.get(route.platform, route.platform)
    if target.recipient is not None:
        by = setter_name(settings, sender.platform, sender.user_id, sender.display_name)
        return f"It'll go to {target.recipient} as a direct message on {name}, marked “from {by}”."
    if route == here:
        return None
    if route.platform == here.platform and route.is_direct_address:
        return "I'll DM you when it's due."
    if route.platform == here.platform:
        return "It'll arrive in another chat."
    return f"It'll arrive as a direct message on {name}."


def label(route: Route, here: Route, recipient: str | None = None) -> str:
    """A suffix for ``/list`` rows delivered somewhere other than this chat."""
    name = PLATFORM_NAMES.get(route.platform, route.platform)
    if recipient is not None:
        return f" → {recipient} on {name}"
    if route == here:
        return ""
    if route.is_direct_address:
        return f" → {name} DM"
    return f" → {name}" if route.platform != here.platform else " → another chat"
