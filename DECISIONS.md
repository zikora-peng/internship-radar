# Design decisions

Why this system is shaped the way it is. The reasoning matters more than the
code here — most of these were forced by a constraint discovered while
building, not chosen up front.

## Architecture

```
tools/fetch_listings.py   -> pulls + filters listings.json
tools/scorer.py           -> batches to Groq, returns {id: {score, pitch}}
tools/docs_writer.py      -> appends rows to the Google Doc table (append-only)
tools/telegram_notify.py  -> daily summary, alerts on score >= ALERT_THRESHOLD
tools/profile.py          -> profile + config (threshold, terms)
tools/main.py             -> orchestrates, tracks data/seen_ids.json for dedup
tests/                    -> offline Docs/Groq API simulation, no credentials
.github/workflows/daily_scan.yml -> cron-scheduled Actions run
```

The split is deliberate: the LLM makes one kind of judgement (does this listing
fit this candidate), and every other step — fetching, filtering, deduping,
index arithmetic, rate limiting, retrying — is deterministic Python. Anything
the model touches is bounded, parsed defensively, and recoverable if it fails.

## Data source: a structured feed, not a scraper

`SimplifyJobs/Summer2027-Internships` publishes `listings.json`, updated
roughly hourly, with `active`, `is_visible`, `terms`, `degrees`, `locations`
and `sponsorship` already as fields.

LinkedIn scraping breaks their ToS. Handshake needs school SSO. Both would mean
maintaining HTML selectors against a hostile, changing target. The feed gives
typed fields for free and has no login wall, so the fetch layer is 43 lines and
has never broken.

The `degrees` field in particular turned out to carry real signal — see the
JP Morgan case below.

## Scoring: Groq free tier, batched, self-paced

`llama-3.3-70b-versatile`, OpenAI-compatible API, no credit card. Listings are
batched 15 per call with strict-JSON output.

**Rate limiting is the interesting part.** The free tier is 30 req/min,
1,000 req/day, 12K tokens/min, 100K tokens/day — and **tokens per minute is the
binding constraint, not requests**. A batch runs ~1.8K tokens, so the real
ceiling is about 6 requests/min, a fifth of what the request limit implies.
Firing the first run's 28 batches back-to-back 429s within seconds.

So `scorer.py` carries a sliding-window rate limiter that paces to 90% of the
token cap and charges itself with the API's *reported* `usage.total_tokens`
rather than an estimate, so pacing self-corrects instead of drifting. 429s
retry with exponential backoff and jitter.

**Malformed JSON escalates rather than failing.** Bad JSON is usually a
truncated or unescaped-quote response, and both scale with response length — so
the batch is retried once, then split in half and each half tried separately. A
smaller batch frequently parses where the full one didn't, which salvages most
of a batch that would otherwise be written off. Only `JSONDecodeError`
escalates; an auth or network error would fail identically on every half, so
retrying those would just burn quota.

## Output: one Google Doc table, append-only

Columns: Date Added, Company, Role, Category, Location, Sponsorship, Date
Posted, Fit Score, Pitch, Link, Status.

A Doc rather than a local file because the deliverable has to be readable on a
phone without a laptop involved. Link is a clickable "Apply" hyperlink — raw
URLs run 100+ chars and wreck the column widths (`SHOW_FULL_URL` in
`docs_writer.py` flips this). Fit Score cells shade green at 70+, amber at
45-69.

**`Status` is user-owned and never written to.** The whole table is append-only
by contract: rows are only ever added, never rewritten or re-sorted, so
anything typed into the doc by hand survives every subsequent run.

### The Docs API is index-based, and that's where the bugs live

- Every `insertText` shifts the index of everything after it, so all text
  inserts are emitted in **descending index order** and can't invalidate each
  other. `_fill_requests()` enforces this.
- Cell styling uses table coordinates, not text offsets, so
  `updateTableCellStyle` / `updateTableColumnProperties` are immune to shifting.
- Adding rows is two-phase: `insertTableRow`, then re-`get` the document to read
  the new cells' real indices. Post-insert indices can't be computed reliably
  client-side.
- `insertText` with `""` is rejected, so empty cells must be skipped.
- Writes are chunked at 20 rows. The first run appends ~400 listings; unchunked
  that's ~4,400 requests in one `batchUpdate`, which fails.
- If the fill phase fails after rows were inserted, they're rolled back with
  `deleteTableRow` — otherwise the doc accumulates blank rows *and* re-adds
  those listings tomorrow.

### Auth: service account, and the user creates the doc

This runs headless in CI, so an interactive OAuth refresh-token flow is the
wrong tool. But service accounts have **no Drive storage quota**, so letting one
create the file fails in a confusing way. The user creates a blank doc and
shares it with the service account email — which also means the doc lives in
their own Drive, under their ownership, which is where you'd want it anyway.

## Partial failure: two gates before anything is marked seen

This is the invariant the whole system rests on, and it was got wrong first.

A listing is recorded in `data/seen_ids.json` only if it was **both** scored
**and** written:

1. `scorer.py` flags anything it couldn't score with `failed=True`. `main.py`
   keeps those out of the doc entirely — no placeholder row — so they come back
   as new next run.
2. `append_matches()` returns the set of ids it actually wrote, and `main.py`
   records only those, so a failed write retries instead of vanishing.

**Why it matters:** the earlier behaviour wrote a placeholder row at score 0 and
marked the listing seen. Because the table is append-only, that froze 45
listings at 0 permanently — the only fix would have been rewriting existing
rows, which the contract forbids. An unknown score is not a zero. The Telegram
digest reports the `pending re-score` count so a scorer failing every day is
visible rather than silent.

## Everything fails soft except the feed

A bad scoring batch, a failed Doc chunk or a Telegram error must never kill the
run or lose written data. Only an unreachable feed fails loudly — there's
nothing to lose in that case.

## Log everything, alert selectively

Every new listing goes in the doc regardless of score; only `ALERT_THRESHOLD`
(75) triggers Telegram. The doc stays comprehensive, Telegram stays quiet. The
threshold is 75 rather than 70 so an alert has to clear the location bar too.

## Persistence is the git repo itself

There is no database. `data/seen_ids.json` is the dedup ledger, and the Actions
workflow commits it back to the repo at the end of every run — so the repo is
both the code and the state store, and the commit log doubles as a run history.

This is the one file that is *not* disposable. Delete it and the next run
re-processes and re-logs every active listing as if new.

## Verified-correct behaviour that looks like a bug

Near-identical titles at the same company in the same city can score very
differently. Verified 2026-08-16 on JP Morgan, all NYC:

| Title | `degrees` in the feed | Score |
|---|---|---|
| Quantitative Research **Summer Analyst** Intern | `Bachelor's, Master's` | 90 |
| Quantitative Research **Intern** | `PhD` | 0 |
| Quantitative Research Intern – Risk & Treasury | `Master's, PhD` | 0 |

The scorer read the `degrees` field rather than pattern-matching the title,
which is exactly what the "deprioritize roles requiring a Master's/PhD" rule
asks for. Check `degrees` in `listings.json` before concluding the model is
being inconsistent.

## Page geometry

Eleven columns don't fit on portrait Letter — that's only 468pt of usable width
at 1in margins. `ensure_table()` forces landscape Letter with 0.5in margins,
giving 792 - 36 - 36 = 720pt, and `COLUMN_WIDTHS_PT` must sum to at most that.
The widths originally totalled 914pt, so Link and Status rendered off the right
edge and the Apply links were unreachable.

`tests/test_writer_flow.py` now asserts the sum. Page setup and column widths
are document/table properties, not row content, so re-applying them to a
populated doc is append-only-safe: `python tools/docs_writer.py
--repair-layout` does exactly that and touches no cell text.

## Cost: $0/month, by construction

No billing account on the Google Cloud project (Docs/Drive APIs are free
without one; the $300 trial is declined deliberately, since it attaches a card).
No card on Groq — adding one switches to the metered Developer tier. The free
tier is rate-limited, not metered, so the worst case is a 429, which the code
handles.
