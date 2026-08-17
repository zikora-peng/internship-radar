#!/usr/bin/env bash
#
# Discovers your Telegram chat id by polling getUpdates. You must have sent
# your bot at least one message first — a bot cannot see a chat until the
# human speaks first. Prints the id to stdout, nothing else, so it can be
# captured with $(...).
#
# Usage: bash tools/get_telegram_chat_id.sh <bot-token>
set -euo pipefail

TOKEN="${1:?usage: get_telegram_chat_id.sh <bot-token>}"

for attempt in 1 2 3 4 5 6; do
  CHAT_ID="$(curl -sS --max-time 20 "https://api.telegram.org/bot${TOKEN}/getUpdates" \
    | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not data.get("ok"):
    sys.stderr.write("Telegram API rejected the token: %s\n" % data.get("description", "?"))
    sys.exit(2)
ids = [u["message"]["chat"]["id"] for u in data.get("result", []) if "message" in u]
if ids:
    print(ids[-1])
')" || true
  if [ -n "${CHAT_ID:-}" ]; then echo "$CHAT_ID"; exit 0; fi
  printf 'No messages yet. Send your bot any message in Telegram now... (attempt %s/6)\n' "$attempt" >&2
  sleep 10
done
echo "Never saw a message. Open Telegram, message your bot, then re-run." >&2
exit 1
