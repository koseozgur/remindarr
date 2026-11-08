"""Check a LINE webhook URL end to end, without LINE.

Sends LINE's Verify payload (``{"destination": ..., "events": []}``) twice:
once signed with the channel secret, expecting 200, and once with a wrong
signature, expecting 401. Neither creates a reminder or calls the LINE API.

    REMINDARR_LINE_CHANNEL_SECRET=... uv run python scripts/line_webhook_check.py \\
        https://<tunnel>.trycloudflare.com/line/webhook

The secret is read from the environment rather than argv so it does not land
in shell history.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request


def post(url: str, body: bytes, signature: str) -> int:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Line-Signature": signature},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    url = sys.argv[1]
    secret = os.environ.get("REMINDARR_LINE_CHANNEL_SECRET", "").strip()
    if not secret:
        print("set REMINDARR_LINE_CHANNEL_SECRET in the environment", file=sys.stderr)
        return 2

    body = json.dumps({"destination": "webhook-check", "events": []}).encode()
    good = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    bad = base64.b64encode(hashlib.sha256(b"not the signature").digest()).decode()

    results = [("signed Verify payload", post(url, body, good), 200)]
    results.append(("wrong signature", post(url, body, bad), 401))

    ok = True
    for name, got, want in results:
        mark = "ok  " if got == want else "FAIL"
        ok &= got == want
        print(f"{mark} {name}: HTTP {got} (expected {want})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
