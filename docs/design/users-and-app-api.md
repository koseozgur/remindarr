# Users, pairing and the app API

Status: **phase 1 built** (2026-11-17): users, invites, linking, admin commands.
Phase 2 (pairing and the app API) is not built yet. This is the server-side
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
| GET | `/api/v1/reminders` | the user's pending reminders (id, text, due_at, recurrence, where) |
| POST | `/api/v1/reminders` | `{"text": "call mum tomorrow at 9"}`, parsed by the **same** grammar (and the AI plugin later), so the app and the chat behave identically |
| DELETE | `/api/v1/reminders/{id}` | cancel |
| DELETE | `/api/v1/devices/self` | sign out |

It runs on the same aiohttp server as the LINE webhook. Caddy gains one
route, `/api/*`, and everything else stays 404.

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

- **Does the app deliver reminders?** In phase 1 the app *shows and creates*
  reminders, and delivery stays on LINE/Discord. Local notifications on the
  phone would duplicate those, unless "app" becomes a delivery choice
  (`deliver_on: app`). To decide when the app exists.
- **Timezone lives on each identity, kept in step.** Phase 1 left the column
  where it was (the recurring-reminders work reads it). `/tz` and `/link` keep
  all of one user's identities on the same zone. Moving it onto `users` is a
  later tidy-up, not a behaviour change.
