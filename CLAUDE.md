# Reminder app — project context

Started 2026-08-18. **v0 is built and working end to end** (Telegram +
Discord, one-shot reminders). This file is the brief, not a record — update it
as real decisions get made and reality replaces intent.

**Working name is `remindarr` (the folder name), deliberately a placeholder.
Pick a real name before anything is published.**

---

## What this is

A self-hosted reminder tool that connects to the messaging surfaces people
already use, instead of asking them to live in another app.

**One thing, done right.** Reminders. Not a project manager, not a note
app, not a calendar. Scope discipline is the product — resist feature
creep even when it's easy.

## Why it exists

This came out of a real, failed search for such a tool (2026-08, during
the owner's homelab work — see `~/dev/homelab/CLAUDE.md`'s Home Assistant
section for the full history). What was actually tried:

- **Home Assistant** — works, and currently runs the owner's to-do lists
  via `alexa_devices` + automations. But it means running a whole
  home-automation platform to get reminders, and the to-do capture path
  depends on an Amazon integration that stores the **full Amazon account
  password** (not a scoped token) and forced a 2FA downgrade
  (passkey → password+TOTP) on a shared family account.
- **Alexa** — the capture path above. Unofficial emulated-login, can break
  without notice.
- **Siri Shortcuts → HA** — the Amazon-free alternative, never fully
  evaluated.

None of these were satisfying. The gap is a **self-hosted, chat-native,
narrow-scope reminder tool**. That's what this is.

**The space is crowded** (Todoist, ntfy, countless Telegram reminder bots,
HA itself) — this is not a novel category. What makes it worth building is
the specific combination: self-hosted + genuinely bidirectional chat +
LINE support that Western tools skip + deliberately narrow scope + the
location-trigger idea below. If those stop being true, the reason to
build it stops being true too.

---

## Core architecture bet

**Capture and delivery are separate problems, and most tools only solve
one.** Design them as independent, pluggable adapter sets:

```
capture adapters → core (store, schedule, trigger) → delivery adapters
```

Some platforms do both, some only one, and their constraints differ enough
(quota models, reply windows, auth) that this isn't over-engineering — it's
the actual shape of the problem. Verified per-platform, 2026-08-18:

| Platform | Bidirectional? | Notes |
|---|---|---|
| **Telegram** | Yes | Free, unlimited, webhook-based, no business account. **Easiest — start here.** |
| **Discord** | Yes | Free, well-documented. **Start here too.** |
| Slack | Yes | Free tier fine for personal use. |
| **LINE** | Yes | Real Messaging API + webhooks. Needs a (free) LINE Official Account. **Quota model matters: replies to a user who messaged first are NOT capped; push/broadcast messages count against the free tier.** So reactive use is cheap, scheduled push reminders are the part that eats quota. **Differentiator — dominant in Japan, skipped by most Western self-hosted tools.** |
| WhatsApp | **Outbound only, backlog** | Meta deprecated on-premise/self-hosted entirely (Oct 2025 — Cloud API is the only supported path for new integrations), and it's built/priced for business use. Unofficial workarounds (WAHA etc.) exist but are exactly the TOS-gray path this project avoids. **Deliberate scope decision — say so in the README rather than leaving it looking like a gap.** |

One-way delivery-only platforms are fine and explicitly supported ("set a
reminder, get a text"). Don't let the hardest platform dictate the
architecture.

## Mobile

**Native apps, deliberately very simple.** Retro-leaning visual style —
tentative, can change later, not load-bearing.

Not yet decided: whether "native" means truly native per-platform or a
cross-platform toolkit. A PWA was considered and rejected for the main
app (iOS notification limitations), but revisit if the native path proves
disproportionately expensive.

## The differentiator worth protecting

**Location-based triggers.** The owner built one in Home Assistant that
fires on entering `zone.home` when the to-do list has open items,
rate-limited to once per 4h. This is the thing a calendar structurally
cannot do, and it's a real reason to pick this over "just use reminders."
Don't let it fall off the roadmap as a nice-to-have.

---

## Explicit non-goals

- Not a project manager, note app, calendar, or habit tracker.
- No unofficial/TOS-gray platform integrations (this is why WhatsApp is
  outbound-only and why WAHA-style workarounds are out).
- Not chasing feature parity with Todoist et al.

---

## Owner context worth knowing

- Self-hosting is the norm here, not a novelty — there's an established
  homelab (Synology NAS, Docker stack, ~20 services). Assume Docker
  deployment is a first-class target, not an afterthought.
- Strong stated preference: **clean rebuilds over clever
  state-preserving workarounds.** When there's a choice between an
  intricate migration and delete-and-recreate, propose the latter.
- **Don't propose solutions framed as "might work."** Verify before
  suggesting, or say explicitly that something is untested. This was
  direct feedback, not an inference.
- Japan-based — hence LINE mattering, and hence timezone/locale handling
  being real requirements rather than v2 concerns.

---

## Stack (decided 2026-08-18)

**Python 3.13 + uv.** Go was considered and declined: its only real edge
here was image/RSS size, which Docker largely flattens, and the owner's
domain language is Python+bash. LINE ships official, actively maintained
SDKs for both languages, so that was not a tiebreaker. If Go comes up
again, the honest framing is to learn it on a project where the language
is the *only* new thing.

- **Store:** SQLAlchemy 2 async + SQLite (WAL). No Alembic yet — the schema
  is young enough that a bad migration costs more than a rebuild.
- **Scheduling:** no APScheduler. The `reminders` table is the single source
  of truth and a tick loop polls it. Buys restart safety, free downtime
  catch-up, and a "why didn't this fire?" answerable in one SQL query.
- **Parsing:** a narrow hand-written grammar, not `dateparser`. A fuzzy
  parser's failure mode is silently reading task text as a date, which
  yields a confidently wrong reminder; a narrow one says "I didn't
  understand", which is fixable.
- **No inbound ports.** Telegram long-polls, Discord uses its gateway —
  both outbound. This changes when LINE lands, since LINE requires webhooks.

## Status

**v0 done:** capture → parse → store → schedule → deliver, on Telegram and
Discord. 128 tests, mypy strict, ruff clean. CI (GitHub Actions) runs all
of that plus a Docker image build and startup smoke test on every push —
the image builds, but has **not yet been run with real tokens**, and no
message has made a real round trip through Telegram or Discord.

**Not yet decided:** the mobile approach (native per-platform vs
cross-platform toolkit) is still open, and unblocked — nothing in v0
depends on it.

**Next:** LINE (the differentiator, and the first webhook requirement),
then recurring reminders (the `recurrence` column is reserved and unused),
then location triggers — which must not be allowed to slide off the
roadmap.
