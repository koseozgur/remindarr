<h1 align="center">remindarr</h1>

<p align="center">
  <b>Self-hosted, chat-native reminders.</b><br>
  Tell a bot what to remind you about, in the chat app you already have open.<br>
  It tells you back, on time.
</p>

<p align="center">
  <a href="https://github.com/koseozgur/remindarr/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/koseozgur/remindarr/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.13+" src="https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white">
  <img alt="Typed: mypy strict" src="https://img.shields.io/badge/mypy-strict-2A6DB2">
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff" src="https://img.shields.io/badge/lint-ruff-D7FF64"></a>
  <img alt="Tests" src="https://img.shields.io/badge/tests-282-3fb950">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

<p align="center">
  <img src="docs/demo.svg" width="900"
       alt="A remindarr session: reminders captured in plain language, an ambiguous time resolved and disclosed, an unparseable message refused rather than guessed at, and reminders delivered on time — including two that fired after three days of downtime and said they were late.">
</p>

<p align="center"><sub>Generated from the real code — see <a href="#how-it-works">How it works</a>.</sub></p>

**Status: v0, in daily use.** One-shot reminders on LINE, Discord and
Telegram, SQLite, an allow-list, and cross-app delivery (set it on LINE, get it
on Discord). **LINE and Discord were verified live on 2026-11-08**, including
the allow-list's refusal path. **Telegram is implemented and tested but has
never made a real round trip.** See [Roadmap](#roadmap) for what is
deliberately not here yet.

---

## Why this exists

Reminders are a solved problem — unless you want them self-hosted, in your
chat apps, and not tied to a vendor. The existing options each fail one of
those:

- **Home Assistant** works, but means running a whole home-automation platform
  to get reminders, and the usual capture path stores a full Amazon account
  password rather than a scoped token.
- **Todoist et al.** are hosted, and want to be your project manager.
- **The Telegram reminder bots** are mostly one-way, one-platform, and someone
  else's server.

## One thing, done right

Reminders. Not tasks, not notes, not a calendar, not habits. The scope
discipline *is* the product.

---

## The architecture bet

**Capture and delivery are separate problems, and most tools only solve one.**

```mermaid
flowchart LR
    subgraph cap["capture"]
        direction TB
        T1["Telegram"]
        D1["Discord"]
        L1["LINE"]
    end

    core["core<br/>parse · store<br/>schedule · trigger"]

    subgraph del["delivery"]
        direction TB
        T2["Telegram"]
        D2["Discord"]
        L2["LINE"]
        O2["SMS · ntfy · email"]
    end

    T1 --> core
    D1 --> core
    L1 --> core

    core --> T2
    core --> D2
    core --> L2
    core -.-> O2
```

<sub>Solid = shipped. Dashed = planned.</sub>

They are two protocols, not one, because their constraints genuinely differ.
A one-way SMS gateway is delivery-only and that is a first-class case, not a
degraded one. On LINE, *replying* to someone who messaged you is free while
*pushing* to them is quota-metered — so scheduled reminders are the expensive
direction and conversation is the cheap one. An architecture that assumed
symmetry would have to be unpicked to express that.

The core never sees a platform object. Adapters translate to
`IncomingMessage` / `OutgoingMessage` at the boundary.

The bet pays off concretely: because a reminder records where to deliver
separately from where it was set, it can be captured on one app and delivered
on another. Capture on LINE, where replies are free; deliver on Discord, where
pushes are unlimited. See [Cross-app delivery](#cross-app-delivery).

### Inbound ports: none, unless you enable LINE

Telegram long-polls and Discord uses its gateway websocket. Both are
**outbound** connections, so with only those two enabled remindarr runs behind
NAT on a home NAS with no port forwarding, no reverse proxy, and no TLS
certificate.

LINE is different: it *requires* a webhook, so enabling it opens the project's
first inbound endpoint. remindarr listens on plain HTTP at
`REMINDARR_WEBHOOK_HOST:REMINDARR_WEBHOOK_PORT` (default `0.0.0.0:8080`), path
`REMINDARR_LINE_WEBHOOK_PATH`; LINE must reach that over **public HTTPS**. How
— a reverse proxy, a tunnel, a small VM — is a deployment choice remindarr does
not make for you.

LINE does not publish the addresses its webhooks come from, so requests are
authenticated solely by the `x-line-signature` HMAC, verified over the raw
body before anything is parsed. A request that fails it gets a 401 and touches
nothing.

---

## Quick start

Requires Python 3.13+. [`uv`](https://docs.astral.sh/uv/) recommended.

```bash
uv sync
cp .env.example .env    # then put your token(s) in it
uv run remindarr
```

A bot is enabled by having a token. There is no separate on/off flag to fall
out of sync with whether the credential is actually there.

**The first reply you get will be a refusal.** Nobody may use the bot until
they are on the allow-list, and that includes you. Message the bot once; it
answers `Not authorised. Ask the owner to add: telegram:123456789` (or
`discord:…`, `line:U…`). Put that in `.env` as `REMINDARR_ALLOWED_USERS`,
restart, and it works. See [Access control](#access-control).

### Getting a Telegram token

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow prompts.
2. Put the token in `.env` as `REMINDARR_TELEGRAM_TOKEN`.
3. Message your new bot. That is it.

### Getting a Discord token

1. [Developer Portal](https://discord.com/developers/applications) → New
   Application → Bot → Reset Token.
2. **Bot → Privileged Gateway Intents → enable MESSAGE CONTENT INTENT.**
   Without it the bot connects normally and then sees every message as blank,
   which looks exactly like a bug in this code. It is self-serve under 100
   servers. remindarr logs a specific warning if it detects this.
3. OAuth2 → URL Generator → scopes `bot`, permissions *Send Messages* +
   *Read Message History* → open the URL to invite it.
4. Put the token in `.env` as `REMINDARR_DISCORD_TOKEN`.

### Setting up LINE

LINE needs two credentials and a public HTTPS URL (see
[Inbound ports](#inbound-ports-none-unless-you-enable-line)).

1. Create a free **LINE Official Account**:
   [register a Business ID](https://account.line.biz/signup?redirectUri=https://entry.line.biz/form/entry/unverified)
   (your normal LINE account works), then fill in the
   [entry form](https://entry.line.biz/form/entry/unverified). The Developers
   Console can no longer create Messaging API channels itself (since 2024).
2. In the [Official Account Manager](https://manager.line.biz/), **enable the
   Messaging API**. You'll pick a provider, which can't be changed later. This
   creates the Messaging API channel in the
   [Developers Console](https://developers.line.biz/console/).
3. Developers Console → your channel:
   - **Basic settings** → *Channel secret* → `REMINDARR_LINE_CHANNEL_SECRET`.
   - **Messaging API** → *Channel access token (long-lived)* → Issue →
     `REMINDARR_LINE_CHANNEL_ACCESS_TOKEN`.

   Both are required. Setting only one is a startup error, not a silently
   disabled platform.
4. Turn **OFF** *Greeting messages* and *Auto-reply messages*: Developers
   Console → Messaging API tab → *Edit* next to each, which opens the Official
   Account Manager. Otherwise LINE answers alongside remindarr and every
   message gets two replies.
5. Start remindarr and make `…:8080/line/webhook` reachable over HTTPS.
6. Developers Console → Messaging API → *Webhook URL*: set it to your public
   URL, e.g. `https://example.com/line/webhook`, and press **Verify**. LINE
   sends an empty, signed test request; remindarr answers 200. Then turn on
   **Use webhook** on the same tab.
7. **Add the bot as a friend** (scan the QR code on the Messaging API tab),
   then message it. Pushes only reach people who have friended the account.

#### The quota, and what it costs you

On the free plan in Japan, LINE allows **200 counted messages a month**, as a
hard cap with no pay-as-you-go. What counts:

- **Scheduled reminders count** — each is a *push*. A reminder delivered to a
  group counts once per member: one reminder to a group of five is five. Only
  allowed users can set reminders, but an allowed user's group reminders are
  still billed per member — the allow-list does not change that.
- **Replies are free** — confirmations, `/list`, `/help` and error messages go
  back on LINE's reply path, which is not metered. remindarr never retries a
  failed reply as a push.

When the month's quota runs out, LINE refuses pushes, and remindarr marks the
affected reminders failed (with "LINE monthly message limit reached") rather
than retrying into a wall. There is no warning before that point yet.

The way around the cap is not to push on LINE at all: set reminders on LINE
and have them [delivered on Discord](#cross-app-delivery). Every LINE message
is then a free reply.

#### Things LINE does not tell you

- **A user who blocked the account is undetectable.** LINE answers a push to
  them with 200 as though it were delivered. remindarr cannot tell, so neither
  can you.
- **Group messages from some clients have no user id.** LINE omits it for
  users not on LINE for iOS/Android. remindarr cannot tell who owns such a
  reminder, so it replies saying so instead of setting one.
- **Webhook redelivery is off by default.** If you turn it on, remindarr
  ignores duplicate events — but it remembers them in memory, so a redelivery
  arriving after a restart is processed again.

### Docker

```bash
mkdir -p data && chown 1000:1000 data
docker compose up -d
```

The SQLite database lives in `./data`, bind-mounted, so a container rebuild
does not take your reminders with it.

The container runs as uid 1000 rather than root, so the bind-mounted `./data`
has to be writable by that uid — hence the `chown`. On a Synology the share
may be owned by something else; `chown` it to 1000 or add
`user: "<uid>:<gid>"` to the compose service. Skipping this gives an opaque
`unable to open database file` at startup.

There is no published port by default, deliberately — Telegram and Discord
connect outbound. If you enable LINE, the container listens on 8080; uncomment
the `ports:` example in `docker-compose.yml` or put your ingress on the same
Docker network. How LINE reaches it is up to you.

One worked example: [`docs/deploy-gcp.md`](docs/deploy-gcp.md) runs it on a
free-tier GCP `e2-micro` behind Caddy (automatic HTTPS, only the webhook path
forwarded), with the cost model, updates, backups and teardown.

### Access control

remindarr answers only the people you list. **It denies by default:** with
`REMINDARR_ALLOWED_USERS` unset or empty it refuses everyone, and logs
`access.allow_list_empty` at startup so a fresh install does not look broken.

This matters most on LINE, where a bot's Basic ID is searchable: anyone can
friend it, and every reminder a stranger set would spend your monthly push
quota. On Telegram and Discord the cost is a stranger's data in your database.

**Finding your id:** message the bot. A refusal names you in the exact format
to paste:

```
Not authorised. Ask the owner to add: line:U4af4980629...
```

The same id is logged at INFO as `access.denied`. On LINE the refusal is a
reply, so it costs no quota.

**Format:** comma-separated `platform:user_id` entries, spaces allowed:

```bash
REMINDARR_ALLOWED_USERS=line:U4af4980629..., telegram:123456789, discord:987654321
```

- The platform is `telegram`, `discord` or `line`, case-insensitive. The user
  id is case-sensitive and must match exactly. A malformed entry or an unknown
  platform is a startup error, not a silently ignored line.
- It is per platform: `discord:42` does not allow `telegram:42`.
- In a group, the check is on the **person who sent the message**, not the
  group. An allowed user can set reminders in a group with strangers in it.
  The strangers cannot, and get the same refusal. A LINE group message with no
  user id ([see above](#things-line-does-not-tell-you)) is always refused.
- A refused user gets no subscriber row, no reminder, and no command — not
  even `/help`.
- It is configuration: change it and restart. There are no admin commands.
- Linking identities into a person ([below](#cross-app-delivery)) grants no
  access. Every identity in `REMINDARR_PEOPLE` must also be in the allow-list,
  or startup fails and says which one is missing.

### Cross-app delivery

By default a reminder arrives where you set it. To set reminders on one app
and get them on another, tell remindarr which identities are one person and
where that person wants them:

```bash
REMINDARR_ALLOWED_USERS=line:U4af4980629..., discord:987654321
REMINDARR_PEOPLE=ozgur = line:U4af4980629..., discord:987654321 > discord
```

Now a reminder set in your one-to-one LINE chat arrives as a Discord DM, and
the confirmation says so:

```
Got it — “take the bins out” tomorrow at 09:00 (in 14 hours).
id 7
It'll arrive as a direct message on Discord.
```

**Why you would want this:** on LINE's free plan, replies are free and pushes
are capped at 200 a month ([details](#the-quota-and-what-it-costs-you)). If
you capture on LINE and deliver on Discord, every LINE message is a free
reply, and the cap stops mattering.

**Syntax.** People are separated by `;`. Each is
`name = platform:id, platform:id`, optionally followed by `> app`, the app
reminders are delivered on. Names are lowercase letters, digits, `-` and `_`.
Without `> app`, reminders still arrive where they were set. These are
startup errors, not silent misreadings:

- an identity that is in two people, or not in the allow-list;
- two identities on one platform for the same person;
- a default app the person has no identity on, or one that is not enabled.

**The default applies to one-to-one chats only.** A reminder set in a LINE or
Telegram group goes back to that group. A group reminder is for the group.

**Discord is the exception: reminders always arrive by DM.** A reminder set in
a server channel goes to the setter's DMs, and the reply in the channel says
*I'll DM you when it's due.* If the DM is refused (the bot shares no server
with you, or your DMs from server members are off), it is delivered to the
channel it was set in instead, with a mention of you so you are still
notified. If that fails too, the reminder is marked failed and says why.

**Per reminder: `@app`.** Put `@discord`, `@line` or `@telegram` anywhere in
the message to have that one reminder delivered to you directly on that app.
That includes from a group, and it overrides your default:

```
call mum tomorrow at 9 @line
```

The `@` is deliberate. "post the update on discord tomorrow" is a real task,
and remindarr will never read plain words as routing. That is the same rule
the time parser follows: never silently eat part of the task. So:

- only `@line`, `@discord` and `@telegram` are acted on; any other `@word`
  (`email @john about the invoice tomorrow`) stays in the reminder as written;
- for the same reason, a person in `REMINDARR_PEOPLE` cannot be named after
  an app;
- two apps in one message, an app you have no linked identity on, or an app
  that is not enabled are all refused, saying why.

`/list` marks reminders that will arrive somewhere other than the current chat
(`#7 take the bins out — tomorrow at 09:00 (in 14 hours) → Discord DM`).

Delivering on Discord to someone who has not written from Discord opens a DM
with them. That needs the bot to share a server with them and their DMs from
server members to be on. A cross-app reminder has no channel to fall back
to, so if the DM is refused it fails permanently, with a message saying so,
and is not retried.

**Nothing in a reminder can ping anyone.** Reminder text is whatever someone
typed. On Discord every message is sent with mentions disabled, so
`@everyone`, `@here`, a role or a user mention in the text is inert. The one
exception is the channel fallback's mention of the person who set it.

One limit: each app's identity still has its own reminders. `/list` and
`/cancel` on Discord do not see what you set on LINE.

---

## Using it

Just say it:

```
call the dentist tomorrow at 9
remind me to check the burn-in in 45 minutes
water the plants on saturday morning
rent is due on 1 september
deploy freeze 2026-09-03 14:00
```

Commands:

| | |
|---|---|
| `/list` | what is pending, as `#id text — when` |
| `/cancel <id>` | drop one (`/cancel 3` or `/cancel #3`) |
| `/tz <zone>` | set your timezone, IANA name (`/tz Asia/Tokyo`) |
| `/help` | the above |

Add `@line`, `@discord` or `@telegram` to a reminder to have it delivered on
that app; see [Cross-app delivery](#cross-app-delivery).

### What the parser accepts

Deliberately a **narrow, explicit grammar** rather than a general
natural-language date parser. A fuzzy parser's failure mode is silently reading
part of your task as a date — "call Mars about Q3" becoming March — which
produces a confidently wrong reminder. A narrow grammar's failure mode is
saying "I could not find a time in that", which you can immediately fix. For a
tool whose whole job is firing at the right moment, the second failure is much
cheaper.

```
in 20 minutes / in 20m / in 1h30m / in an hour / in 3 days / in 2 weeks
tomorrow / tomorrow at 9 / tomorrow at 09:30 / tomorrow morning
today at 18:00 / tonight / this evening / noon / midnight
at 9am / at 18:00 / at 9
monday / on monday at 9am / next friday
sep 3 / on 3 september at 10:00 / 2026-09-03 / 2026-09-03 14:00
```

Two ambiguities are resolved by *disclosure* rather than by guessing well:

- **`at 9`** with no am/pm resolves to whichever reading comes soonest — at
  15:00 that means 21:00 today; at 22:00 it means 09:00 tomorrow.
- **`next friday`** is treated as the soonest Friday. English does not agree
  with itself here, so remindarr says which date it picked.

Every confirmation echoes the resolved absolute time, so a wrong guess is
visible immediately rather than at the moment the reminder does not fire.

### Late delivery is stated, not hidden

If the process was down when a reminder came due, it fires on the next tick and
*says* it is late. Silently delivering a two-hour-old reminder as though it
were on time leaves you unable to tell whether you misread the clock or the
system failed you.

---

## Configuration

All via environment or `.env`, prefix `REMINDARR_`:

| Variable | Default | Meaning |
|---|---|---|
| `REMINDARR_TELEGRAM_TOKEN` | — | enables Telegram |
| `REMINDARR_DISCORD_TOKEN` | — | enables Discord |
| `REMINDARR_ALLOWED_USERS` | — (nobody) | who may use the bot, `platform:user_id,…` ([details](#access-control)) |
| `REMINDARR_PEOPLE` | — | identities that are one person, and where they get reminders ([details](#cross-app-delivery)) |
| `REMINDARR_LINE_CHANNEL_SECRET` | — | with the token below, enables LINE |
| `REMINDARR_LINE_CHANNEL_ACCESS_TOKEN` | — | with the secret above, enables LINE |
| `REMINDARR_WEBHOOK_HOST` | `0.0.0.0` | webhook listen address (LINE only) |
| `REMINDARR_WEBHOOK_PORT` | `8080` | webhook listen port (LINE only) |
| `REMINDARR_LINE_WEBHOOK_PATH` | `/line/webhook` | webhook path LINE posts to |
| `REMINDARR_DATABASE_URL` | `sqlite+aiosqlite:///data/remindarr.db` | store |
| `REMINDARR_DEFAULT_TIMEZONE` | `Asia/Tokyo` | for subscribers not yet seen |
| `REMINDARR_TICK_SECONDS` | `15` | scheduler poll interval |
| `REMINDARR_MAX_DELIVERY_ATTEMPTS` | `5` | before giving up |
| `REMINDARR_RETRY_BACKOFF_SECONDS` | `30` | base for exponential backoff |
| `REMINDARR_LATE_DELIVERY_GRACE_MINUTES` | `60` | lateness before saying so |
| `REMINDARR_LOG_LEVEL` | `INFO` | |
| `REMINDARR_LOG_JSON` | `false` | JSON logs for shipping |

---

## How it works

The `reminders` table is the single source of truth. A loop polls it every
`tick_seconds` for anything due:

```sql
SELECT * FROM reminders WHERE state='pending' AND next_attempt_at <= now
```

No job-store library, no timer wheel. That buys restart safety (nothing to
resync), downtime catch-up (the query is `<=`, not `==`), and debuggability —
"why did this not fire?" is one query against a table a human can read. The
cost is up to `tick_seconds` of latency, which for reminders is noise.

`due_at` records intent and never changes. `next_attempt_at` moves on retry.
Keeping them separate means a reminder that took four attempts still knows when
it was *supposed* to arrive, which is what the lateness notice is computed from.

### Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run python scripts/gen_demo.py   # regenerate the README's demo image
```

#### LINE webhook on a dev machine

LINE only talks to a public HTTPS URL. For development, `scripts/dev-tunnel.sh`
opens a [Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)
(no Cloudflare account; `brew install cloudflared`) to the local webhook port
and prints the exact URL to paste into the LINE console:

```bash
scripts/dev-tunnel.sh
```

- **The URL is random and changes on every run.** Each time, paste it into
  Developers Console → Messaging API → *Webhook URL* again and press Verify.
- **While it runs, the webhook port is public.** Anyone who finds the URL can
  reach it; the `x-line-signature` check is the only gate. Stop it when you
  are done.
- `scripts/line_webhook_check.py <url>` (secret in
  `REMINDARR_LINE_CHANNEL_SECRET`) sends a signed and a wrongly signed Verify
  payload and checks for 200 / 401. Use it to test the tunnel without LINE.

This is for development only. Production ingress is a separate, undecided
question.

The demo image at the top is generated by driving the real `Router` and
`Scheduler` over a real database, so it cannot drift away from what the code
actually does.

No Alembic yet. The schema is young enough that a bad migration costs more than
a rebuild. When a version adds a column (cross-app delivery added
`reminders.fallback_address`), an older database is refused at startup with a
message naming the missing column. Back it up if it matters, then delete it
and start fresh. When it stabilises and there is data worth preserving, that is the
moment to add migrations — not before.

---

## Roadmap

**Next up**

- **A warning before LINE's monthly push quota runs out**, for anyone who
  still delivers on LINE.
- **Recurring reminders.** The schema has a reserved `recurrence` column; v0
  writes `NULL` and never reads it.

**The one worth protecting**

- **Location triggers.** Fire when you *arrive* somewhere with open items —
  rate-limited, not nagging. This is the thing a calendar structurally cannot
  do, and the real reason to pick this over the reminders app already on your
  phone. It is not a nice-to-have; it is the point.

**Later**

- Native mobile app, deliberately simple, retro-leaning.
- Slack, ntfy, email, SMS delivery adapters.
- One `/list` across a person's identities, so you can see and cancel on
  Discord what you set on LINE.

### WhatsApp: outbound only, and not soon

This is a deliberate scope decision, not an oversight. Meta deprecated the
on-premise/self-hosted API entirely — Cloud API is the only supported path for
new integrations — and it is built and priced for business use. Unofficial
workarounds exist and are exactly the TOS-gray path this project avoids. If
WhatsApp appears at all it will be delivery-only.

## Non-goals

- Not a project manager, note app, calendar, or habit tracker.
- No unofficial or TOS-gray platform integrations.
- Not chasing feature parity with Todoist.

## License

MIT
