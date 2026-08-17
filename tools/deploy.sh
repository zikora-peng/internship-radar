#!/usr/bin/env bash
#
# Creates the GitHub repo, pushes, sets all five secrets, triggers the first
# workflow run, and tails the logs — end to end, no clicking through the
# GitHub UI.
#
# Prereqs: `gh auth login` done once, and ./.env filled in (copy .env.example).
#
# Usage:  bash tools/deploy.sh [repo-name]
#
set -euo pipefail

REPO_NAME="${1:-internship-radar}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

SECRETS_FILE="$ROOT/.env"
KEY_PATH="$ROOT/credentials.json"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

cd "$ROOT"

command -v gh >/dev/null 2>&1 || die "gh (GitHub CLI) is not installed: https://cli.github.com"
gh auth status >/dev/null 2>&1 || die "Not logged in. Run: gh auth login"
[ -f "$SECRETS_FILE" ] || die "Missing $SECRETS_FILE — copy .env.example to .env and fill it in."
[ -f "$KEY_PATH" ] || die "Missing $KEY_PATH — run: bash tools/setup_google.sh"

# shellcheck disable=SC1090
set -a; source "$SECRETS_FILE"; set +a

for var in GROQ_API_KEY TELEGRAM_BOT_TOKEN GOOGLE_DOC_ID; do
  [ -n "${!var:-}" ] || die "$var is empty in $SECRETS_FILE"
done

# ---------------------------------------------------------------------------
say "Resolving Telegram chat id"
if [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "  using the value already in .env: $TELEGRAM_CHAT_ID"
else
  TELEGRAM_CHAT_ID="$(bash "$HERE/get_telegram_chat_id.sh" "$TELEGRAM_BOT_TOKEN")" \
    || die "Could not find a chat id. Message your bot once in Telegram, then re-run."
  echo "  discovered: $TELEGRAM_CHAT_ID"
  printf '\nTELEGRAM_CHAT_ID=%s\n' "$TELEGRAM_CHAT_ID" >> "$SECRETS_FILE"
fi

# ---------------------------------------------------------------------------
say "Preparing git repo"
[ -d .git ] || { git init -q; git branch -M main; }
git add -A
git diff --staged --quiet || git commit -q -m "Internship Radar: Google Docs output"

if gh repo view "$REPO_NAME" >/dev/null 2>&1; then
  echo "  repo already exists — pushing to it"
  git remote get-url origin >/dev/null 2>&1 || \
    git remote add origin "$(gh repo view "$REPO_NAME" --json sshUrl -q .sshUrl)"
  git push -u origin main
else
  say "Creating GitHub repo: $REPO_NAME"
  # Private by default. Flip to --public when you want it on your resume —
  # the repo contains no secrets, only references to them.
  gh repo create "$REPO_NAME" --private --source=. --remote=origin --push
fi

# ---------------------------------------------------------------------------
say "Setting repository secrets"
gh secret set GROQ_API_KEY --body "$GROQ_API_KEY"
gh secret set TELEGRAM_BOT_TOKEN --body "$TELEGRAM_BOT_TOKEN"
gh secret set TELEGRAM_CHAT_ID --body "$TELEGRAM_CHAT_ID"
gh secret set GOOGLE_DOC_ID --body "$GOOGLE_DOC_ID"
gh secret set GOOGLE_SERVICE_ACCOUNT_JSON < "$KEY_PATH"
echo "  five secrets set:"; gh secret list

# ---------------------------------------------------------------------------
say "Triggering the first run"
# The workflow must exist on the default branch before it can be dispatched;
# GitHub can take a moment to register it after the initial push.
for attempt in 1 2 3 4 5 6; do
  if gh workflow run daily_scan.yml 2>/dev/null; then break; fi
  [ "$attempt" = 6 ] && die "Workflow never registered. Check the Actions tab."
  echo "  waiting for GitHub to register the workflow (attempt $attempt)..."
  sleep 10
done

sleep 5
RUN_ID="$(gh run list --workflow=daily_scan.yml --limit 1 --json databaseId -q '.[0].databaseId')"
say "Watching run $RUN_ID (first run scores ~400 listings — allow a few minutes)"
gh run watch "$RUN_ID" --exit-status || {
  say "Run failed. Full logs:"
  gh run view "$RUN_ID" --log-failed
  die "See the log above for the failure."
}

say "Success"
cat <<EOF

  Doc      https://docs.google.com/document/d/$GOOGLE_DOC_ID/edit
  Actions  $(gh repo view --json url -q .url)/actions

Check for: rows in the doc, a Telegram message, and a bot commit updating
data/seen_ids.json. It now runs itself daily at 13:00 UTC.
EOF
