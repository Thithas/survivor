#!/bin/bash
# Runs automatically when the Codespace starts: downloads cloudflared, starts the relay, publishes its URL to RELAY.
cd "$(dirname "$0")/.."
pip install -q requests
TG() { [ -n "$TELEGRAM_BOT_TOKEN" ] && curl -s -o /dev/null "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" -d chat_id="$TELEGRAM_CHAT_ID" -d text="relay: $1"; }
TG "codespace booting, fetching cloudflared"
if ! command -v cloudflared >/dev/null 2>&1; then
  curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared \
  && sudo install /tmp/cloudflared /usr/local/bin/cloudflared || TG "cloudflared download failed"
fi
export GH_TOKEN="${GH_TOKEN:-$GITHUB_TOKEN}" REPO="${GITHUB_REPOSITORY:-Thithas/survivor}" RELAY_SECRET="${RELAY_SECRET:-s7v-relay-2026}"
while true; do python phone/relay.py; echo "relay exited, restarting"; sleep 5; done
