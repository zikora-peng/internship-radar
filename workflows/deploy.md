# Workflow: Deploy Internship Radar

## Objective
Get the project from a local folder to a live, self-running GitHub Actions
cron job — repo, secrets, and a verified first run.

## Operating principle
**Run the tools yourself. Do not hand me a checklist of console clicks.**
Only three things genuinely require me, listed under "Human inputs" below.
Everything else is automated; if a tool fails, fix it and re-run.

## Human inputs (the only irreducible steps)
| Step | Why it can't be automated |
|---|---|
| Create Telegram bot: @BotFather → `/newbot`, then message the bot once | No API exists to create a bot. The bot also can't see a chat until I message it first. |
| Create Groq API key at console.groq.com | Account creation needs a human. No credit card. |
| Browser logins: `gh auth login`, and the `gcloud auth` prompts | That's the point of a login. |

Ask me for the Groq key and Telegram token together, in one message. Don't
drip-feed requests.

## Tools to use, in order
1. **`bash tools/setup_google.sh`**
   Creates the Cloud project, enables Docs + Drive APIs, creates the service
   account and key (`credentials.json`), creates the Google Doc in *my* Drive,
   shares it with the service account, and writes `GOOGLE_DOC_ID` to `.env`.
   Triggers the `gcloud` browser logins itself.

2. **`cp .env.example .env`**, then have me paste the Groq key and Telegram
   token into it. Leave `TELEGRAM_CHAT_ID` blank.

3. **`bash tools/deploy.sh [repo-name]`**
   Discovers the chat id (calls `tools/get_telegram_chat_id.sh`), creates the
   GitHub repo, pushes, sets all five secrets, triggers the workflow, and tails
   the log until it passes or fails.

## Expected outputs
- A GitHub repo with the workflow on the default branch and 5 secrets set
- A Google Doc with a header row and ~400 scored rows
- One Telegram message
- A bot commit updating `data/seen_ids.json`

## Edge cases
| Situation | Handling |
|---|---|
| `gcloud` not installed | Tell me the install command. It's a prerequisite, not a step I can skip. |
| `gcloud failed to load. You are running gcloud with Python 3.9` | macOS ships Python 3.9; gcloud needs 3.10–3.14. Install a standalone Python and export `CLOUDSDK_PYTHON` to it before every `gcloud` call. gcloud's own `--quiet` installer tries to fix this via `sudo` and fails without a TTY — ignore that, it's non-fatal. |
| `projects.create` fails with `Callers must accept Terms of Service` | The Google account has never opened the Cloud Console. One-time human step: sign in at console.cloud.google.com and accept the ToS. **Decline the $300 trial / billing prompt** — it's not needed and breaks the $0 guarantee. Then re-run. No orphan project is left behind; creation failed atomically. |
| Service account key creation fails | IAM propagation lag. The script already retries 5×. |
| Drive API 403 `SERVICE_DISABLED` at step 5, naming `consumer: projects/764086051850` | That consumer is the generic gcloud OAuth client, not ours. User ADC needs an explicit `x-goog-user-project: $PROJECT_ID` header on every Drive call, or quota is billed to that shared project where Drive is off. Enabling the API on our project does not help. Fixed in `setup_google.sh`; keep the header on any new Drive/Docs call made with ADC. |
| Re-running `setup_google.sh` after a mid-script failure | Pass `PROJECT_ID=<existing-id>` to reuse the project, service account, and key instead of creating a second set. The script now no-ops on each if it already exists, and skips the Drive browser prompt when saved ADC still reaches Drive. Without this every retry burns another project against the account quota. |
| Editing any heredoc inside `$(...)` in a `tools/*.sh` script | Do not use apostrophes (`don't`, `gcloud's`) even in comments. macOS bash 3.2 miscounts quotes there and dies with `unexpected EOF while looking for matching '` pointing at an unrelated line far below. Always `bash -n` the script after editing. |
| `gh workflow run` says the workflow doesn't exist | GitHub hasn't registered it post-push. Script retries 6× with 10s gaps. |
| No Telegram chat id found | I haven't messaged the bot yet. Ask me to, then re-run. |
| Doc write 403 | Doc not shared with the service account, or Docs API not enabled. Re-run `tools/setup_google.sh`. |
| Groq 404 on the model | Groq retired it. Check console.groq.com for current models and update `MODEL` in `tools/scorer.py`. |

## Cost guardrails — do not cross these
- **Never enable billing on the Google Cloud project.** Docs/Drive APIs are
  free without it. Decline the $300 trial if offered.
- **Never add a card to Groq.** That switches me to the metered Developer tier.
  On Free, hitting a limit returns 429, which the code handles.
- Everything here must stay $0/month.

## Re-runnability
Both scripts are safe to re-run. `setup_google.sh` creates a fresh project;
`deploy.sh` detects an existing repo and pushes to it.
