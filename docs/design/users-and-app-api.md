# Users, pairing and the app API

Status: **phase 1 built** (2026-11-17): users, invites, linking, admin commands.
**Phase 2 server side built** (2026-11-17, not deployed): `/pair`, `/devices`,
`/unpair`, device tokens and the v1 API, tested against a local server only.
The SwiftUI app is scaffolded in `ios/`. The Caddy route is not added yet. This is the server-side
foundation for the iPhone app, and it also makes family onboarding possible
without anyone editing the VM.

## Why

Today, who may use the bot and who is who live in two env vars on the VM
(`REMINDARR_ALLOWED_USERS`, `REMINDARR_PEOPLE`). Adding a family member means
SSHing in, editing a root-owned file and restarting. An app needs real
accounts it can sign in to. Both problems have the same fix: **users live in
the database**, and the chat apps become the way people prove who they are.

## Principles

- **The chat apps are the identity providers.** No passwords, no email, no
  third-party login. You are whoever the bot can reach on LINE or Discord.
  (Sign in with Apple and passkeys likely need Apple's paid program; this
  needs nothing.)
- **The owner stays in control** without touching the VM for routine
  changes: invitations happen in chat.
- **Config shrinks to bootstrap.** Env vars only seed the first admin(s) and
  never grant access silently afterwards.
- **Every code is short-lived, single-use and rate-limited.** Every token is
  random, stored only as a hash, and revocable.

## Data model

```
users              one human
  id, name (unique slug), timezone, deliver_on (default app), is_admin,
  created_at

subscribers        one chat identity (exists today)
  + user_id  -> users.id, nullable       linked = allowed

invites / link codes / pairing codes     one table, distinguished by kind
  code_hash, kind (invite|link|pair), user_id (or pending name for an
  invite), created_by, expires_at, used_at

devices            one paired app install
  id, user_id, name, token_hash, created_at, last_seen_at, revoked_at
```

**Allowed** means the identity is linked to a user. `/join` is the only thing
an unlinked identity can do. Everything else gets today's refusal, so the
anti-quota-burn property holds.

## Flows

**Bootstrap (first run).** `REMINDARR_ADMINS=line:U…,discord:…` creates one
admin user ("owner") with those identities linked, once. The existing
`REMINDARR_ALLOWED_USERS` and `REMINDARR_PEOPLE` are imported once into
users, so today's VM keeps working unchanged. After that, the database is the
source of truth.

**Inviting family (owner, in chat).**
```
owner: /invite mum
bot:   Ask mum to send me this on LINE or Discord within 24h:  /join K7QX-3M9D
mum:   /join K7QX-3M9D
bot:   Welcome, mum! …
```

**Linking a second app (any user).** `/link` on one app gives a code; send
`/link CODE` from the other app. This answers the brief's old question of
proving you own both accounts: you just did, from both.

**Pairing the iPhone app.**
```
you (in chat): /pair
bot:           Code for the app: 4F8K-29QW (10 minutes, one use)
app:           enter the server address + the code  ->  POST /api/v1/pair
server:        a long-lived device token; the app stores it in the Keychain
```
`/devices` lists paired devices and `/unpair <n>` revokes one.

**Admin housekeeping.** `/users`, plus `/remove <name>`, which unlinks
everything and revokes devices.

## The app API (v1)

All endpoints are JSON over HTTPS behind Caddy, authenticated with
`Authorization: Bearer <device token>`, and scoped to the token's user.

| Method | Path | Does |
|---|---|---|
| POST | `/api/v1/pair` | code + device name → token (the only unauthenticated call) |
| GET | `/api/v1/me` | user, timezone, linked apps |
| GET | `/api/v1/reminders` | the user's pending reminders (id, text, due_at, upcoming, recurrence, where) |
| POST | `/api/v1/reminders` | `{"text": "call mum tomorrow at 9"}`, parsed by the **same** grammar (and the AI plugin later), so the app and the chat behave identically |
| DELETE | `/api/v1/reminders/{id}` | cancel |
| DELETE | `/api/v1/devices/self` | sign out |
| POST | `/api/v1/devices/self/sync` | the app's report of what it scheduled (see below) |
| GET/PUT | `/api/v1/settings` | where reminders arrive: phone, chat or both, and the chat app |

It runs on **its own listener**, `REMINDARR_API_PORT` (off when unset), like
`/healthz`, rather than on the LINE webhook's server as first planned: that
way it works with LINE disabled, which is also what makes a local dev server
possible. Caddy gains one route, `/api/*` to that port, and everything else
stays 404.

As built:

- A reminder set in the app goes through the same `Router.create` as a chat
  message (grammar, then the AI reader, then `@app`/`@person` routing), as
  if sent in a 1:1 chat on the user's **home app**: their `/deliver` app,
  else the first linked of Discord, Telegram, LINE (LINE last: its pushes
  are metered). Answers carry the same "Got it — … tomorrow at 09:00" line.
- `GET /reminders` lists the user's pending reminders from **all** their
  linked apps (chat `/list` still shows only the current app's).
- Errors are `{"error": "..."}`: 400 bad body, 401 any auth failure (one
  body for all), 404 not found or not yours, 409 already done, 413 body over
  4 KB, 422 unreadable reminder text, 429 too many wrong pairing codes.
- Pairing failures are limited per remote address and overall. Behind Caddy
  the address is Caddy's, so the overall cap (50 an hour) is what bites;
  someone could use it to block pairing for an hour, not to guess a code.
- `/remove` deletes the user's devices; `/unpair` and sign-out set
  `revoked_at`.
- Each reminder carries `upcoming`: its next occurrences in UTC, starting
  with `due_at` (just that for a one-off, up to 10 for a series). The app
  schedules phone notifications from it.

### Where reminders arrive: phone, chat, or both

Decided by the owner 2026-11-17. Each user picks one, in the app's settings
or with `/deliver` in chat (one setting, stored on the server):

| Mode | Phone | Chat |
|---|---|---|
| `chat` (default) | nothing scheduled | always |
| `both` | always | always |
| `phone` | always | **only when the phone isn't provably ready** |

The chat app is the existing `/deliver <app>` default, unchanged.

**The guard for `phone`.** Without remote push, a phone only knows what it
saw at its last refresh, and a phone can stop working silently (app deleted,
free-tier signing lapsed, notifications switched off). So the server skips
chat for one occurrence only if some paired, not-signed-out device has
reported, within the last 24 hours, all of:

- notifications allowed, and nothing dropped by iOS's 64-pending cap;
- a sync from a reminder list read *after* the reminder was created (so the
  phone had it), and *before* the occurrence was due;
- for a series, the occurrence is within the first 9 after that list (the
  app schedules 10), so an hourly series past what the phone scheduled goes
  to chat.

Otherwise chat delivers as usual. So "phone only" can produce a chat message
when the phone isn't provably ready, but never silence. Reminders someone
else set for you aren't in your app's list, so they always come by chat.

The app reports with `POST /api/v1/devices/self/sync` after every refresh:
`{"as_of": <from GET /reminders>, "notifications": bool, "complete": bool}`.
`GET/PUT /api/v1/settings` reads and changes the mode and chat app. Both live
in new tables (`phone_delivery`, `device_syncs`), so an existing database
gains them on start without a rebuild.

## Security notes

- **Codes:** 8 characters from a 32-symbol alphabet with no look-alikes
  (40 bits), shown as `XXXX-XXXX`. They expire (invite 24h, link and pair
  10min), are single-use, and are compared by hash. Failed attempts are
  rate-limited per identity and per IP, so brute force inside the expiry
  window isn't practical.
- **Tokens:** `secrets.token_urlsafe(32)` (256 bits), stored as SHA-256,
  shown once. `last_seen_at` makes stale devices visible.
- **No user enumeration:** a wrong, expired or used code gets the same reply.
- **Request limits:** bodies capped at a few KB; reminder text keeps today's
  length limits.
- **The API is a second internet-facing endpoint.** Signature checks protect
  the LINE webhook; bearer tokens protect the API. Both sit behind Caddy and
  Let's Encrypt.

## Phasing

1. **Users in the database** (built): the tables, bootstrap and import,
   `/invite` `/join` `/link` `/users` `/remove` `/deliver`, and the allow
   check moving to "linked to a user". `@person` resolves against users.
   Behaviour for today's VM is unchanged after the one-time import, except
   that the schema change needs a fresh database (see the PR).
2. **Pairing and the API:** devices, `/pair` `/devices` `/unpair`, the v1
   endpoints, and the Caddy route.

## Open questions

- **Invites by email** (owner request, 2026-11-17, for once the app is
  running): send the invite to an email address instead of pasting a code
  into chat. Open questions: the outbound mail path (provider, SPF/DKIM),
  whether the email carries a code or a link that opens the app (Associated
  Domains, likely the paid Apple program), and whether email also becomes a
  sign-in route for the app.
- **Does the app deliver reminders?** In phase 1 the app *shows and creates*
  reminders, and delivery stays on LINE/Discord. Local notifications on the
  phone would duplicate those, unless "app" becomes a delivery choice
  (`deliver_on: app`). To decide when the app exists.
- **Timezone lives on each identity, kept in step.** Phase 1 left the column
  where it was (the recurring-reminders work reads it). `/tz` and `/link` keep
  all of one user's identities on the same zone. Moving it onto `users` is a
  later tidy-up, not a behaviour change.
