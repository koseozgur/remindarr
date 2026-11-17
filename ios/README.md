# iOS app

Native SwiftUI, deliberately small: pair with the server, see your pending
reminders, set new ones, cancel by swiping. Reminders still arrive on your
chat app; the app doesn't deliver anything (yet).

## Renaming

The product name is a placeholder. The display name and bundle identifier
live **only** in [`Config/App.xcconfig`](Config/App.xcconfig). The Xcode
target and project are called plain `App` on purpose, so nothing else needs
renaming.

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

## On your own iPhone

Free Apple tier: set your team under Signing & Capabilities, make the bundle
id in `App.xcconfig` unique, and run. The app then needs to be re-signed
every 7 days. It needs an HTTPS server address: plain HTTP is allowed only to
local addresses.
