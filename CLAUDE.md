# Agent Instructions — Internship Radar

You're working inside the **WAT framework** (Workflows, Agents, Tools). This
architecture separates concerns so that probabilistic AI handles reasoning while
deterministic code handles execution. That separation is what makes this system
reliable.

## What this project is
A daily automation that scans the SimplifyJobs Summer 2027 internships feed for
new postings, scores each against my profile with an LLM, logs every one to a
Google Doc table, and pings me on Telegram for strong matches. It runs itself
on a GitHub Actions cron — no daily human input. Portfolio project for a
second-year CS student targeting Summer 2027 internships.

## The WAT Architecture, as it exists here

**Layer 1: Workflows (The Instructions)** — `workflows/`
- `daily_scan.md` — the core loop: fetch → score → log → notify
- `deploy.md` — first-time deployment, and what only I can do
- `troubleshoot_failed_run.md` — diagnosis table and recovery invariants

**Layer 2: Agents (The Decision-Maker)** — you
- Read the relevant workflow first, then run tools in the order it specifies
- Handle failures per the workflow's edge-case table rather than improvising
- Ask me only for the three human inputs listed in `workflows/deploy.md`

**Layer 3: Tools (The Execution)** — `tools/`

| Tool | Responsibility |
|---|---|
| `fetch_listings.py` | Pull + filter `listings.json` (active, visible, matching term) |
| `scorer.py` | Batch listings to Groq, return `{id: {score, pitch}}` |
| `docs_writer.py` | Append scored rows to the Google Doc table |
| `telegram_notify.py` | Daily digest, alerts only at `ALERT_THRESHOLD`+ |
| `profile.py` | Placeholder profile + config (threshold, terms). My real profile is **not** committed — it lives in gitignored `tools/profile_local.py`, or the `PROFILE_TEXT` secret in CI, either of which overrides the placeholder. Edit `profile_local.py`, not `profile.py`. |
| `main.py` | Orchestrates the above; tracks `data/seen_ids.json` for dedup |
| `setup_google.sh` | One-time: Cloud project, APIs, service account, Doc, sharing |
| `deploy.sh` | One-time: repo, push, secrets, first run, log tailing |
| `get_telegram_chat_id.sh` | Polls `getUpdates` to discover my chat id |

**Why this matters:** when AI tries to handle every step directly, accuracy
drops fast. Offloading execution to deterministic scripts keeps you on
orchestration, where you're strongest.

## How to Operate

**1. Look for existing tools first**
Everything the daily loop needs already exists in `tools/`. Before writing new
code, check whether a tool covers it. In particular: don't hand-roll Google
Docs API calls, don't write a second scorer, and don't scrape job boards —
`fetch_listings.py` uses a structured public feed for a reason.

**2. Learn and adapt when things fail**
Read the full trace, fix the tool, retest, then record what you learned in the
relevant workflow. Groq's free tier is 1,000 requests/day — ample, but use the
offline tests in `tests/` for debugging rather than burning live calls. **Ask
me before any re-run that hits paid or rate-limited APIs beyond the ordinary
daily loop.**

**3. Keep workflows current**
Add constraints and edge cases to `workflows/` as you discover them. Don't
create or overwrite a workflow file wholesale without asking — adding a learned
row to an existing table is fine and encouraged.

## The Self-Improvement Loop
Identify what broke → fix the tool → verify with `tests/` → update the workflow
→ move on with a more robust system.

## File Structure

```
workflows/       # Markdown SOPs — read these first
tools/           # Deterministic execution: the daily loop + one-time setup
tests/           # Offline simulations. No credentials, no network, no API spend.
data/            # seen_ids.json — durable dedup state (see deviation below)
.tmp/            # Disposable intermediates. Regenerated freely.
.env             # All secrets. NEVER store secrets anywhere else.
credentials.json # Google service account key (gitignored)
.github/workflows/daily_scan.yml   # GitHub Actions cron — NOT a WAT workflow
```

**Naming collision, read carefully:** `.github/workflows/` is GitHub's required
CI directory. `workflows/` is the WAT SOP layer. They are unrelated. Don't put
SOPs in `.github/workflows/` or YAML in `workflows/`.

**Deliverable vs intermediate:** the Google Doc is the deliverable — it lives in
the cloud where I can read it on my phone. Local files are plumbing.

**One deliberate deviation:** `data/seen_ids.json` is *not* disposable and does
not belong in `.tmp/`. It's the dedup ledger, committed back to the repo by CI
every run. Delete it and the next run re-processes and re-logs all ~400 active
listings as if new. Treat it as durable state.

## Domain constraints you must not break

**Append-only contract.** The `Status` column in the Doc is mine — I type
Applied / Interview / Rejected there. Never write to it, never rewrite an
existing row, never re-sort the table. Only append.

**Never mark a listing seen unless it was actually scored *and* written.**
Two gates, both required:
- `scorer.py` flags anything it could not score with `failed=True`. `main.py`
  keeps those out of the doc entirely — no placeholder row — so they come back
  as new next run. An unknown score is not a zero, and append-only means a row
  written today can never be corrected tomorrow.
- `append_matches()` returns the set of ids it actually wrote; `main.py` records
  only those, so a failed write retries instead of vanishing.

Together these are why partial failures are safe. Writing a placeholder row and
marking it seen — the behaviour before 2026-08-16 — froze 45 listings at score
0 permanently, because the only fix would have been rewriting existing rows.
The Telegram digest reports the `pending re-score` count so a scorer that fails
every day is visible rather than silent.

**Everything fails soft except the feed.** A bad scoring batch, a failed Doc
chunk, or a Telegram error must never kill the run or lose written data. Only an
unreachable feed should fail loudly — there's nothing to lose in that case.

**Log everything, alert selectively.** Every new listing goes in the Doc
regardless of score; only `ALERT_THRESHOLD` (75) triggers Telegram. The Doc
stays comprehensive, Telegram stays quiet. Note these are two different
constants: `ALERT_THRESHOLD = 75` in `profile.py` gates Telegram, while
`STRONG_CUTOFF = 70` in `docs_writer.py` only picks the green cell shade.

## Google Docs API — read before touching `docs_writer.py`
The API is index-based, and this is where bugs hide:
- **Every `insertText` shifts the index of everything after it.** All text
  inserts are emitted in **descending index order** so a request can't
  invalidate one that already ran. `_fill_requests()` enforces this.
- **Cell styling uses table coordinates, not text offsets**, so
  `updateTableCellStyle` / `updateTableColumnProperties` are immune to shifting.
- **Adding rows is two-phase**: `insertTableRow`, then re-`get` the document to
  read the new cells' real indices, then fill. Post-insert indices can't be
  computed client-side reliably.
- **Empty strings must be skipped** — `insertText` with `""` is rejected.
- **Writes are chunked** at `ROWS_PER_BATCH = 20`. The first run appends ~400
  listings; unchunked that's ~4,400 requests in one `batchUpdate`, which fails.
- **If the fill phase fails after rows were inserted, roll them back** with
  `deleteTableRow`. Otherwise the Doc accumulates blank rows *and* re-adds those
  listings tomorrow.
- **Auth is a service account**, and *I* create the Doc and share it with the
  service account email. Service accounts have no Drive storage quota, so they
  can't reliably create the file themselves.

## Groq free-tier limits — don't remove the pacing
`llama-3.3-70b-versatile` free tier: 30 RPM, 1,000 RPD, **12K TPM**, 100K TPD.
TPM is the binding constraint — a batch is ~1.8K tokens, so ~6 requests/min is
the real ceiling. `scorer.py` has a sliding-window `_RateLimiter` that paces to
90% of the cap and charges itself with the API's reported `usage.total_tokens`,
so pacing self-corrects. 429s retry with exponential backoff and jitter.

Without this, the first run (28 back-to-back batches) 429s within seconds and
most listings land unscored. Verified by `tests/test_scorer_limits.py`.

## Cost guardrails
This must stay **$0/month**. Never enable billing on the Google Cloud project
(Docs/Drive APIs are free without it; decline the $300 trial). Never add a card
to Groq (that switches to the metered Developer tier). GitHub Actions usage is
~150 min/month against a 2,000-min free allowance.

## Conventions
- Stdlib-first; a new dependency has to earn its place
- Secrets live in `.env` and GitHub Actions secrets only — never hardcoded,
  never committed. `.env`, `credentials.json`, and `token.json` are gitignored.
- Data source is the SimplifyJobs public feed. Don't add LinkedIn or Handshake
  scraping — ToS risk and login walls are why this design exists.

## Testing loop (do this before trusting a change)
```bash
python tests/test_index_logic.py      # Docs index arithmetic
python tests/test_writer_flow.py      # writer flow vs. simulated Docs API
python tests/test_scorer_limits.py    # pacing vs. simulated free-tier caps
```
Then live: `pip install -r requirements.txt`, fill `.env`,
`export $(cat .env | xargs)`, `python tools/main.py`. Then deployed:
`gh workflow run daily_scan.yml && gh run watch`.

Confirm all four: rows in the Doc, a Telegram message, updated
`data/seen_ids.json`, and the bot's commit in `git log`.
