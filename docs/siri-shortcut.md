# Setting reminders by voice

Two ways, both speaking the server's confirmation back, which states the
resolved time, so a misheard "nine" shows up at once.

## 1. With the app: "Hey Siri, remind me in Remind"

The app ships an App Shortcut (`ios/App/RemindIntent.swift`). Once the app is
installed and signed in, Siri, Spotlight and the Shortcuts app offer **Set a
reminder**, with the phrases "Remind me in *app name*" and "Set a reminder in
*app name*". Siri then asks "What should I remind you about, and when?", and
the answer goes through the same reader as a chat message.

App Intents need no entitlement (unlike the older SiriKit), so this works
with free signing. Verified in the simulator from Spotlight, including the
confirmation coming back. **Not yet tried by voice or on a real iPhone.**

## 2. Without the app: a Shortcut

Works with nothing installed but the Shortcuts app, and doesn't expire with
the free tier's 7-day signing. **Untested**: the API calls below were
checked with curl against the local dev server, but the Shortcut itself has
not been built and run.

### Get a token for it

Give the Shortcut its own device token, so it shows up separately in
`/devices` and can be revoked on its own with `/unpair`.

1. Send `/pair` to the bot on LINE or Discord. It replies with a code.
2. On the Mac, within ten minutes:

   ```bash
   curl -s https://bot.ozkose.dev/api/v1/pair -H 'Content-Type: application/json' -d '{"code":"XXXX-XXXX","name":"Siri Shortcut"}'
   ```

   The reply's `token` is the credential. Treat it like a password: it can
   read and set your reminders. Don't paste it into chat, an issue or a PR.

### Build the Shortcut

In the Shortcuts app, a new shortcut named, say, "Remind me" (the name is
what you say to Siri):

1. **Dictate Text.** Language: whichever you'll speak. Stop listening: after
   pause.
2. **Get Contents of URL**
   - URL: `https://bot.ozkose.dev/api/v1/reminders`
   - Method: POST
   - Headers: `Authorization` = `Bearer <token>`
   - Request Body: JSON, one field: `text` (Text) = *Dictated Text*
3. **Get Dictionary Value** `confirmation` from *Contents of URL*.
4. **If** *Dictionary Value* **has any value**
   - **Speak Text** *Dictionary Value*
5. **Otherwise**
   - **Get Dictionary Value** `error` from *Contents of URL*
   - **Speak Text** *Dictionary Value*
6. **End If**

What comes back (verified against the dev server):

- Set: HTTP 201 with `{"confirmation": "Got it — “call mum” tomorrow at
  09:00 (in 21 hours).", "reminder": {...}, "notes": [...]}`. The
  confirmation is in Japanese when the text was.
- Not understood: HTTP 422 with `{"error": "I could not find a time in
  that.\n\nTry something like “call mom tomorrow at 9”."}`.
- Bad or revoked token: HTTP 401 with `{"error": "unauthorized"}`.

The reminder arrives wherever your setting says (`/deliver` in chat, or the
app's settings). Under "phone only", one set by this Shortcut comes by chat
until the app next refreshes, because the phone hasn't seen it yet.
