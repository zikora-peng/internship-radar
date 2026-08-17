# Internship Radar

A daily automation that scans the SimplifyJobs Summer 2027 internship feed for
new postings, scores each one against a candidate profile with an LLM, logs
every posting to a Google Doc table, and sends a Telegram alert only for strong
matches. It runs on a GitHub Actions cron with no human input.

Built with AI assistance (Claude Code). Architecture and design decisions
documented below.

## The problem

Internship postings for a given cycle appear in a trickle over months, and the
useful ones are buried in a few hundred that aren't — wrong degree level, wrong
coast, wrong seniority. Checking by hand means either a daily habit that decays
after a week, or missing the two-day window on the postings that matter.

So: check every day automatically, apply the same filter every time, and only
interrupt me when something clears the bar.

### Why a structured feed, not a scraper

The data source is
[SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships),
which publishes `listings.json` — updated roughly hourly, with `active`,
`is_visible`, `terms`, `degrees`, `locations` and `sponsorship` already as typed
fields.

Scraping LinkedIn breaks their ToS. Handshake sits behind school SSO. Both would
mean maintaining HTML selectors against a target that changes without notice and
actively defends against automation. The feed has no login wall and gives typed
fields for free, so the entire fetch-and-filter layer is **43 lines** and has not
broken once.

That `degrees` field turned out to matter more than expected — see
[DECISIONS.md](DECISIONS.md) for the JP Morgan case where three near-identical
NYC listings correctly scored 90, 0 and 0.

## Architecture

```
                      ┌──────────────────────────────┐
   GitHub Actions ───▶ │ fetch_listings.py            │  public JSON feed
   (cron, 13:00 UTC)   │   filter: active/visible/term│
                      └──────────────┬───────────────┘
                                     ▼
                      ┌──────────────────────────────┐
                      │ diff vs data/seen_ids.json   │  dedup ledger
                      └──────────────┬───────────────┘
                                     ▼
                      ┌──────────────────────────────┐
                      │ scorer.py → Groq             │  batched 15/call,
                      │   {id: {score, pitch}}       │  token-paced, JSON
                      └──────────────┬───────────────┘
                                     ▼
                      ┌──────────────────────────────┐
                      │ docs_writer.py → Google Doc  │  append-only table
                      └──────────────┬───────────────┘
                                     ▼
                      ┌──────────────────────────────┐
                      │ telegram_notify.py           │  only score >= 75
                      └──────────────┬───────────────┘
                                     ▼
                        commit seen_ids.json back
```

The LLM makes exactly one kind of judgement — does this listing fit this
candidate — and everything else is deterministic Python: fetching, filtering,
deduping, Docs index arithmetic, rate limiting, retry, rollback. Anything the
model touches is bounded, parsed defensively, and recoverable when it fails.

**GitHub Actions is also the persistence layer.** There is no database and no
external state store. `data/seen_ids.json` is the dedup ledger, and the workflow
commits it back to the repo at the end of every run using the built-in
`GITHUB_TOKEN`. The repo holds the code *and* the state, and the commit log
doubles as a run history — each `Daily scan: YYYY-MM-DD` commit is one execution
and its diff is exactly what that run discovered. This is what keeps the whole
thing free and dependency-free; it also means the state store inherits git's
concurrency model, which is a real limitation (see Known gaps).

## The Google Doc

| Date Added | Company | Role | Category | Location | Sponsorship | Date Posted | Fit Score | Pitch | Link | Status |
|---|---|---|---|---|---|---|---|---|---|---|

- **Link** is a clickable "Apply" hyperlink. Raw URLs run 100+ characters and
  wreck the column widths; set `SHOW_FULL_URL = True` in `tools/docs_writer.py`
  to show them anyway.
- **Fit Score** cells shade green at 70+ and amber at 45–69. This is cosmetic
  only and is a *different* constant from the alert threshold: `STRONG_CUTOFF`
  in `docs_writer.py` picks the shade, `ALERT_THRESHOLD` in `profile.py` decides
  what reaches Telegram.
- **Status** is never written to by the code — it's for typing Applied /
  Interview / Rejected by hand. The table is append-only by contract: rows are
  only ever added, never rewritten or re-sorted.

## Numbers

Measured, not projected. Where a figure comes from a simulation rather than a
live run, it says so.

**Code** — 2,745 lines committed, of which:

| | Lines |
|---|---|
| Daily-loop Python (`tools/*.py`, 6 files) | 1,046 |
| One-time setup shell (`tools/*.sh`, 3 files) | 324 |
| Tests (`tests/`, 4 files) | 515 |
| Docs + workflow SOPs (7 files) | 860 |

`docs_writer.py` is 538 lines of that — over half the daily-loop code — because
the Google Docs API is index-based and nearly all of the difficulty lives there.

**Tests** — 3 suites, **36 assertions**, all passing, no credentials or network
needed. They run against a hand-written fake of the Docs API
(`tests/fake_docs_service.py`, 177 lines) that reproduces its index model, plus
a simulated Groq free tier with a synthetic clock.

**Throughput** — the honest shape of this is *one big day, then a trickle*:

- **First run only:** every currently-active Summer 2027 posting at once. That
  was ~400 when first run; the feed today carries 14,216 total entries of which
  **415** are active, visible Summer 2027 postings.
- **Every day after: typically 0–50 new postings.** The last scheduled run
  processed a handful in 49 seconds. `data/seen_ids.json` currently holds 403
  ids. It does **not** process 400 listings a day — that number is the one-time
  backfill.

**Runtime** — measured wall-clock from the three real runs: 10m9s and 6m36s for
the backfill runs, **49s** for a steady-state scheduled run.

**Groq usage on the heaviest day** — simulated in `tests/test_scorer_limits.py`
against the real free-tier caps, not measured live: 28 requests, ~37K tokens,
~4 minutes wall-clock, peak **9,370 tokens/min against a 12,000 limit**.

**Cost: $0/month.** Nothing here takes a payment method. Groq's free tier is
rate-limited, not metered — hitting a cap returns 429, it never bills. The
Google Cloud project has no billing account attached, which Docs/Drive don't
require. GitHub Actions is unmetered on public repos; on a private repo this
would draw roughly 30 min/month of the 2,000-minute allowance, based on the
49-second steady-state run.

## The two things that were hard

**Groq's real rate limit isn't the one in the headline.** The free tier
advertises 30 requests/min, but it's also 12K *tokens*/min — and at ~1.8K tokens
per batch, that caps you at about 6 requests/min. The first run's 28 back-to-back
batches 429 within seconds. `scorer.py` carries a sliding-window limiter that
paces to 90% of the token cap and charges itself with the API's *reported*
`usage.total_tokens` rather than an estimate, so pacing self-corrects instead of
drifting.

**Partial failure, under an append-only contract.** A row written today can never
be corrected tomorrow, so a listing is marked seen only if it was **both** scored
**and** written. Anything the scorer couldn't score is flagged `failed=True` and
gets no row at all, so it returns as new next run. An earlier version wrote a
placeholder row at score 0 and marked it seen — which froze 45 listings at 0
permanently, since the only fix would have been rewriting existing rows. An
unknown score is not a zero.

## Known gaps

Stated plainly, because they're real.

- **No live end-to-end test.** The 36 assertions all run against fakes. The Docs
  API fake reproduces the index model as I understand it, and if that
  understanding is wrong the tests pass and production breaks. Real runs have
  worked, but that's evidence, not a test.
- **The state store has git's concurrency model.** Two overlapping runs would
  race on `data/seen_ids.json` and one would fail to push. A single daily cron
  makes this near-impossible rather than impossible, and nothing detects or
  retries it.
- **Scoring is not evaluated.** There's no held-out set, no inter-run
  consistency check, no measurement of whether a score of 78 means anything
  stable. Scores have been spot-checked by hand and looked reasonable. That's
  all that can honestly be claimed.
- **Rerunning against the same listings can produce different scores.**
  Temperature is 0.3, not 0, and there's no caching of prior verdicts.
- **`seen_ids.json` grows without bound** and is never pruned of listings that
  have gone inactive.
- **Single-user by construction.** One profile, one doc, one Telegram chat.
  Nothing is multi-tenant.
- **The feed is a single point of failure.** If SimplifyJobs stops publishing or
  changes the schema, this stops working, and the failure mode for a schema
  change is silent — filters would just match nothing.
- **No alerting on the automation itself.** If the Actions run fails outright,
  no Telegram message is sent, so a silent failure looks exactly like a quiet
  day. The pending-re-score count in the digest covers scorer failures only.
- **Setup is not one-click.** Three scripts plus a Telegram bot, a Groq key, and
  a Google Cloud project.

## Setup

Two accounts only you can create — a Groq API key
([console.groq.com](https://console.groq.com), no card) and a Telegram bot
(@BotFather → `/newbot`, then message it once). The scripts do the rest.

```bash
bash tools/setup_google.sh    # Cloud project, APIs, service account, key, Doc, sharing
cp .env.example .env          # paste the Groq key + Telegram token
bash tools/deploy.sh          # repo, secrets, first run
```

### Your profile

`tools/profile.py` ships a **generic placeholder** profile. A real one names
where you live, what you're studying and your work-authorisation status, none of
which belongs in a public repo — so supply yours by either route, both kept out
of git:

- `tools/profile_local.py` with `PROFILE = """..."""` — gitignored, for local runs
- the `PROFILE_TEXT` repo secret — for CI

The env var wins if both are set. With neither, the run still works; it just
scores against the placeholder.

### Repo secrets

| Secret | Source |
|---|---|
| `GROQ_API_KEY` | console.groq.com |
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_CHAT_ID` | `tools/get_telegram_chat_id.sh` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | entire contents of the service account key file |
| `GOOGLE_DOC_ID` | the doc URL's `/document/d/THIS_PART/edit` |
| `PROFILE_TEXT` | your real profile text (optional) |

The Google Doc is deliberately created by **you**, not the script: service
accounts have no Drive storage quota of their own, so letting one create the
file fails confusingly — and this way the doc lives in your Drive, under your
ownership. Share it with the service account email as Editor.

### Manual Google setup, if the script fails

1. [console.cloud.google.com](https://console.cloud.google.com) → create a
   project. **Decline the $300 trial** — it attaches a card, and Docs/Drive are
   free without billing.
2. APIs & Services → Library → enable **Google Docs API** and **Google Drive API**.
3. Credentials → Create Credentials → **Service account**; skip the optional
   role steps.
4. Open it → Keys → Add Key → Create new key → **JSON**. That file is
   `GOOGLE_SERVICE_ACCOUNT_JSON`.
5. Create a blank Google Doc, share it with the service account email
   (`...iam.gserviceaccount.com`) as **Editor**, and copy the doc id from the URL.

## Running it

```bash
python tests/test_index_logic.py      # Docs index arithmetic
python tests/test_writer_flow.py      # writer flow vs. a simulated Docs API
python tests/test_scorer_limits.py    # pacing vs. simulated free-tier caps

pip install -r requirements.txt       # then live:
export $(cat .env | xargs)
python tools/main.py

gh workflow run daily_scan.yml && gh run watch   # then deployed
```

A successful run leaves four traces: new rows in the Doc, a Telegram message, an
updated `data/seen_ids.json`, and the bot's commit in `git log`.

`python tools/docs_writer.py --repair-layout` re-applies page geometry and column
widths to an existing doc without touching any cell text.

## Troubleshooting

- **`403 caller does not have permission`** — the doc isn't shared with the
  service account email, or the Docs API isn't enabled.
- **`404` on the model name** — Groq retired it. Check console.groq.com and
  update `MODEL` in `tools/scorer.py`.
- **Rows written but no Telegram message** — check the Actions log. Notify
  failures are logged as warnings and never abort a run, by design.
- **Listings reappearing as new** — they failed to score or failed to write, so
  they were deliberately left unseen. The Telegram digest reports the count.

## Repository layout

```
workflows/       Markdown SOPs for operating the system
tools/           The daily loop, plus one-time setup scripts
tests/           Offline simulations — no credentials, no network, no API spend
data/            seen_ids.json — durable dedup state, committed by CI
.github/workflows/daily_scan.yml   GitHub Actions cron (unrelated to workflows/)
```

`DECISIONS.md` covers why the system is shaped this way. `CLAUDE.md` is the
working brief for AI-assisted changes to it.
