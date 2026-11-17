# iOS app

Native SwiftUI, deliberately small: pair with the server, see your pending
reminders, set new ones, cancel by swiping, and get them as phone
notifications too. "Set a reminder" also works from Siri and Spotlight (see
[`docs/siri-shortcut.md`](../docs/siri-shortcut.md)).

## Phone notifications

Once there's something in the list, the app offers **Notify me on this phone
too**; iOS asks for permission only when that's tapped. With it on, the app
schedules a local notification for each of your pending reminders: the next
10 occurrences of a repeating one, the soonest 60 in all (iOS keeps at most
64). Reminders you set for someone else aren't scheduled; ones others set for
you don't appear in your list, so they aren't either.

There's no remote push (that needs Apple's paid program), so the phone only
knows what it saw at its last refresh: opening the app, pull to refresh,
setting or cancelling a reminder in the app, or a background refresh, which
iOS runs when it chooses. A reminder set or cancelled in chat shows up, or
stops, on the phone after the next one.

**Each reminder currently arrives twice**, as a phone notification and on
your chat app, because the server doesn't know about the phone. Whether to
change that is an open decision; see `docs/design/users-and-app-api.md`.

## Renaming

The product name is a placeholder. The display name and bundle identifier
live **only** in [`Config/App.xcconfig`](Config/App.xcconfig). The Xcode
target and project are called plain `App` on purpose, so nothing else needs
renaming.

## Server address and your own builds

Debug builds default to the local dev server (`http://localhost:8787`),
Release builds (and so any archive, TestFlight included) to
`https://bot.ozkose.dev`. Both are set in `Config/App.xcconfig`.

For builds on your own iPhone that start already signed in, put a device
token in a git-ignored `Config/Local.xcconfig`:

```bash
cp ios/Config/Local.xcconfig.example ios/Config/Local.xcconfig
```

To get a token, send `/pair` to the bot, then within ten minutes:

```bash
curl -s https://bot.ozkose.dev/api/v1/pair -H 'Content-Type: application/json' -d '{"code":"XXXX-XXXX","name":"Xcode build"}'
```

and put the reply's `token` after `LOCAL_DEVICE_TOKEN[sdk=iphoneos*] =`. (The
token the app already holds from pairing stays in the phone's Keychain and
can't be read out, so this is a fresh one; `/devices` lists both, and
`/unpair` removes either.) The example file applies the server and token to
real-iPhone builds only, so the simulator keeps using the dev server.

How the token is kept out of anything shared:

- `Local.xcconfig` is git-ignored. Never commit it or paste the token.
- Only Debug builds carry it. `App.xcconfig` passes it on for Debug alone,
  and the app reads it only `#if DEBUG`. An archive builds Release, so a
  TestFlight or App Store build has neither the token nor the code that
  reads it (checked: a Release build with a token in `Local.xcconfig` has an
  empty value and no trace of the token in the binary).
- Signing out in the app revokes that token on the server; the next launch
  then shows the pair screen until `Local.xcconfig` gets a new one.

If the Keychain already holds a pairing, that wins over the baked token.

## Running it against a local server

Nothing here touches the live bot, its database or LINE's push quota.

```bash
uv run python scripts/dev_api.py serve
```

Then, in another terminal, get a pairing code:

```bash
uv run python scripts/dev_api.py pair
```

Open `ios/App.xcodeproj` in Xcode, run on a simulator, and enter the code. The
server field defaults to `http://localhost:8787`, which the simulator reaches
on the Mac. Due reminders print in the server's terminal instead of being
sent. `dev_api.py chat /devices` (or any other chat text) talks to the bot as
the dev user.

Headless, from the command line:

```bash
xcodebuild -project ios/App.xcodeproj -scheme App -destination 'platform=iOS Simulator,name=iPhone 17' build
```

Debug builds accept `-devPairCode XXXX-XXXX` as a launch argument, which pairs
without typing (for scripted checks).

## On your own iPhone (untested)

Free Apple tier: in Xcode, target **App** → Signing & Capabilities, tick
"Automatically manage signing" and pick your Personal Team. If Xcode says the
bundle id is taken, change it in `App.xcconfig`. Free-tier installs expire
after 7 days; run from Xcode again to re-sign.

Against the Mac's dev server on the same Wi-Fi, start it on the LAN:

```bash
uv run python scripts/dev_api.py --host 0.0.0.0 serve
```

and in the app use `http://<the Mac's LocalHostName>.local:8787`
(`scutil --get LocalHostName` prints the name). Plain HTTP is allowed only to
local addresses; the real server needs HTTPS. iOS asks once for permission
to use the local network; allow it.
