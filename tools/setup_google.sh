#!/usr/bin/env bash
#
# Automates the entire Google side of setup: project, APIs, service account,
# key file, the Google Doc itself, and sharing the doc with the service
# account. Replaces ~15 manual clicks in the Cloud Console.
#
# The ONLY human step is one browser login, which the script triggers for you.
#
# Usage:  bash tools/setup_google.sh
# Output: ./credentials.json  and GOOGLE_DOC_ID appended to ./.env
#
set -euo pipefail

# Override to reuse an existing project instead of creating a new one. Without
# this, every retry burns another project against the account's quota and
# strands the previous one.
PROJECT_ID="${PROJECT_ID:-internship-radar-$(date +%s | tail -c 7)}"
SA_NAME="internship-radar"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_PATH="$ROOT/credentials.json"
DOC_NAME="Internship Radar — Summer 2027"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

command -v gcloud >/dev/null 2>&1 || die \
  "gcloud is not installed. Install it first: https://cloud.google.com/sdk/docs/install
   (macOS: brew install --cask google-cloud-sdk)"
command -v python3 >/dev/null 2>&1 || die "python3 is required."

# ---------------------------------------------------------------------------
say "Step 1/6 — Google login (browser opens; this is the one manual bit)"
# Two separate credential types are needed:
#   - the gcloud CLI credential, for creating the project/service account
#   - Application Default Credentials WITH Drive scope, so we can create the
#     Doc as YOU. The doc must be owned by you: service accounts have no Drive
#     storage quota and cannot reliably create files.
if ! gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q .; then
  gcloud auth login
else
  echo "Already logged in as: $(gcloud auth list --filter=status:ACTIVE --format='value(account)')"
fi

# Only re-prompt if the saved ADC can't actually reach Drive. Testing the real
# call (not just token presence) is the only reliable check: the ADC file
# records no scope list, so a token can exist yet lack Drive access.
adc_reaches_drive() {
  local tok qp
  tok="$(gcloud auth application-default print-access-token 2>/dev/null)" || return 1
  [ -n "$tok" ] || return 1
  qp="$(python3 -c "import json,os,sys
p=os.path.expanduser('~/.config/gcloud/application_default_credentials.json')
try: print(json.load(open(p)).get('quota_project_id') or '')
except Exception: sys.exit(1)" 2>/dev/null)" || return 1
  [ -n "$qp" ] || return 1
  [ "$(curl -s -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $tok" -H "x-goog-user-project: $qp" \
        'https://www.googleapis.com/drive/v3/files?pageSize=1')" = "200" ]
}

if adc_reaches_drive; then
  echo "Drive access already granted — skipping the second browser prompt."
else
  say "Granting Drive access (a second browser prompt — needed to create the Doc)"
  gcloud auth application-default login \
    --scopes="https://www.googleapis.com/auth/drive.file,https://www.googleapis.com/auth/cloud-platform"
fi

# ---------------------------------------------------------------------------
say "Step 2/6 — Cloud project: $PROJECT_ID"
if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  echo "  project already exists — reusing it"
else
  gcloud projects create "$PROJECT_ID" --name="Internship Radar" --quiet
fi
gcloud config set project "$PROJECT_ID" --quiet
gcloud auth application-default set-quota-project "$PROJECT_ID" --quiet || true

say "Step 3/6 — Enabling Docs + Drive APIs (can take ~30s)"
gcloud services enable docs.googleapis.com drive.googleapis.com --quiet

say "Step 4/6 — Creating service account and key"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
if gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  echo "  service account already exists — reusing it"
else
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="Internship Radar bot" --quiet
fi

# Reuse an existing key rather than minting a spare on every retry; a service
# account is capped at 10 keys and orphaned ones are a standing credential risk.
if [ -s "$KEY_PATH" ]; then
  echo "  reusing existing key at $KEY_PATH"
else
# Retry: IAM propagation lags behind creation by a few seconds.
for attempt in 1 2 3 4 5; do
  if gcloud iam service-accounts keys create "$KEY_PATH" \
      --iam-account="$SA_EMAIL" --quiet 2>/dev/null; then
    break
  fi
  [ "$attempt" = 5 ] && die "Could not create service account key after 5 attempts."
  echo "  waiting for service account to propagate (attempt $attempt)..."
  sleep 5
done
fi
chmod 600 "$KEY_PATH"
echo "  key written to: $KEY_PATH"

# ---------------------------------------------------------------------------
say "Step 5/6 — Creating the Google Doc in YOUR Drive and sharing it"
TOKEN="$(gcloud auth application-default print-access-token)"

DOC_ID="$(python3 - "$TOKEN" "$DOC_NAME" "$PROJECT_ID" <<'PY'
import json, sys, urllib.request, urllib.error
token, name, project = sys.argv[1], sys.argv[2], sys.argv[3]
body = json.dumps({"name": name,
                   "mimeType": "application/vnd.google-apps.document"}).encode()
# x-goog-user-project is REQUIRED with user ADC: without it Drive bills quota to
# the generic gcloud OAuth client project (764086051850), where the API is off,
# and the call 403s with SERVICE_DISABLED no matter what we enabled on ours.
# NB: no apostrophes in this heredoc — bash 3.2 (macOS default) miscounts quotes
# inside a heredoc nested in $(...) and fails to parse the whole file.
req = urllib.request.Request(
    "https://www.googleapis.com/drive/v3/files",
    data=body,
    headers={"Authorization": f"Bearer {token}",
             "Content-Type": "application/json",
             "x-goog-user-project": project},
)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(json.loads(r.read())["id"])
except urllib.error.HTTPError as e:
    sys.stderr.write("Drive API error creating doc: " + e.read().decode() + "\n")
    sys.exit(1)
PY
)"
[ -n "$DOC_ID" ] || die "Doc creation failed."
echo "  doc created: $DOC_ID"

python3 - "$TOKEN" "$DOC_ID" "$SA_EMAIL" "$PROJECT_ID" <<'PY'
import json, sys, urllib.request, urllib.error
token, doc_id, sa_email, project = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
body = json.dumps({"role": "writer", "type": "user",
                   "emailAddress": sa_email}).encode()
req = urllib.request.Request(
    f"https://www.googleapis.com/drive/v3/files/{doc_id}/permissions"
    "?sendNotificationEmail=false",
    data=body,
    headers={"Authorization": f"Bearer {token}",
             "Content-Type": "application/json",
             "x-goog-user-project": project},
)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    print("  shared with service account as Editor")
except urllib.error.HTTPError as e:
    sys.stderr.write("Drive API error sharing doc: " + e.read().decode() + "\n")
    sys.exit(1)
PY

# ---------------------------------------------------------------------------
say "Step 6/6 — Writing GOOGLE_DOC_ID to .env"
touch "$ROOT/.env"
# Replace any existing line rather than appending a duplicate.
if grep -q '^GOOGLE_DOC_ID=' "$ROOT/.env"; then
  python3 - "$ROOT/.env" "$DOC_ID" <<'EOF'
import sys, pathlib
path, doc_id = pathlib.Path(sys.argv[1]), sys.argv[2]
lines = [l for l in path.read_text().splitlines()]
out = [(f"GOOGLE_DOC_ID={doc_id}" if l.startswith("GOOGLE_DOC_ID=") else l) for l in lines]
path.write_text("\n".join(out) + "\n")
EOF
else
  printf 'GOOGLE_DOC_ID=%s\n' "$DOC_ID" >> "$ROOT/.env"
fi
cat <<EOF

  GOOGLE_DOC_ID   $DOC_ID
  Service account $SA_EMAIL
  Key file        $KEY_PATH
  Open the doc    https://docs.google.com/document/d/$DOC_ID/edit

GOOGLE_DOC_ID has been appended to .env automatically.
(credentials.json and .env are both gitignored. Never commit them.)
EOF
