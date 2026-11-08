#!/usr/bin/env bash
# Expose this machine's LINE webhook through a Cloudflare quick tunnel, for
# development. No Cloudflare account needed.
#
# While this runs, the webhook port is reachable by anyone on the internet who
# finds the URL. The x-line-signature check is the only gate. Stop it (Ctrl-C)
# when you are done.
#
# The URL is random and changes every run, so after each start you must paste
# it into the LINE Developers Console again and press Verify.
#
# Usage: scripts/dev-tunnel.sh   (with remindarr already running, LINE enabled)

set -euo pipefail

port="${REMINDARR_WEBHOOK_PORT:-8080}"
path="${REMINDARR_LINE_WEBHOOK_PATH:-/line/webhook}"

if ! command -v cloudflared >/dev/null 2>&1; then
    echo "cloudflared not found. Install it: brew install cloudflared" >&2
    exit 1
fi

log="$(mktemp -t remindarr-tunnel.XXXXXX)"
cloudflared tunnel --no-autoupdate --url "http://localhost:${port}" >"$log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; rm -f "$log"' EXIT INT TERM

url=""
for _ in $(seq 1 30); do
    url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$log" | head -n 1 || true)"
    [ -n "$url" ] && break
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "cloudflared exited before printing a tunnel URL:" >&2
        cat "$log" >&2
        exit 1
    fi
    sleep 1
done

if [ -z "$url" ]; then
    echo "No trycloudflare.com URL after 30s. cloudflared output so far:" >&2
    cat "$log" >&2
    exit 1
fi

cat <<EOF

  Tunnel up: ${url} -> http://localhost:${port}

  Paste this into LINE Developers Console -> Messaging API -> Webhook URL,
  then press Verify:

      ${url}${path}

  This URL is new every run. Anyone with it can reach the webhook; only the
  signature check stands in the way. Ctrl-C to stop.

EOF

wait "$pid"
