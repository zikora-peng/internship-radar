# Workflow: Daily Internship Scan

## Objective
Find new Summer 2027 internship postings, score each against my profile, log
every one to the Google Doc, and alert me on Telegram only for strong matches.

## When this runs
Automatically at 13:00 UTC daily via `.github/workflows/daily_scan.yml`. No
human trigger needed. Run it manually with `gh workflow run daily_scan.yml`.

## Required inputs
| Input | Source | Notes |
|---|---|---|
| `GROQ_API_KEY` | `.env` locally, repo secret in CI | Free tier |
| `TELEGRAM_BOT_TOKEN` | `.env` / repo secret | From @BotFather |
| `TELEGRAM_CHAT_ID` | `.env` / repo secret | Discovered by `tools/deploy.sh` |
| `GOOGLE_DOC_ID` | `.env` / repo secret | Written by `tools/setup_google.sh` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | `credentials.json` / repo secret | Full JSON body |
| My profile text | `tools/profile_local.py` locally, `PROFILE_TEXT` secret in CI | Drives scoring. `tools/profile.py` holds only a generic placeholder — the real text is never committed |
| Dedup state | `data/seen_ids.json` | Committed back by CI each run |

## Tools to use, in order
Do **not** re-implement any of this inline. `tools/main.py` orchestrates it:

1. `tools/fetch_listings.py` — `fetch_all_listings()` then `filter_relevant()`.
   Pulls the SimplifyJobs `listings.json` feed and keeps entries that are
   active, visible, and match `TERMS`.
2. Diff against `data/seen_ids.json` to get only genuinely new listings.
3. `tools/scorer.py` — `score_listings()`. Batches 15 listings per Groq call,
   returns `{id: {score, pitch}}`. Self-paces against free-tier rate limits.
4. `tools/docs_writer.py` — `append_matches()`. Appends one table row per
   listing to the Google Doc. Returns the set of ids actually written.
5. `tools/telegram_notify.py` — `send_summary()`. Sends the digest, listing
   only matches scoring >= `ALERT_THRESHOLD`.
6. Write back `data/seen_ids.json` with **only the ids that were written**.

## Expected outputs
- **Deliverable:** new rows in the Google Doc, highest score first. This is the
  thing I actually look at. Everything else is plumbing.
- **Notification:** one Telegram message, with a link to the doc.
- **State:** updated `data/seen_ids.json`, committed by the CI bot.

## Edge cases and how to handle them
| Situation | Correct behaviour |
|---|---|
| No new listings | Send the "no new postings" Telegram message. Exit clean. Don't touch the doc. |
| A scoring batch returns malformed JSON | Retry the batch once, then split it in half and try each half. Bad JSON tracks response length, so a smaller batch usually parses. Bounded at 6 calls per batch. |
| A scoring batch fails or 429s after that | Those listings get `failed=True`. They are **not written to the doc and not marked seen**, so they return as new next run. Do NOT write a placeholder row: append-only means it could never be corrected, and un-seeing it later would duplicate the row. Changed 2026-08-16 — see CLAUDE.md. |
| Model returns fewer entries than sent | The missing ones are flagged `failed=True` and held back the same way. Every listing gets a verdict, but "no verdict yet" is not a score of 0. |
| A Doc write chunk fails | Roll back the empty rows, leave those ids **unseen** so tomorrow retries them. Never mark unwritten listings as seen. |
| Telegram send fails | Log a warning and exit 0. The doc is the record; a notify failure must never fail the run or lose data. |
| Feed unreachable | Let the run fail loudly. There's nothing to log and nothing to lose. |

## Verified-correct behaviours — do NOT "fix" these
Things that look like scoring bugs, checked against the feed data and confirmed
right. Re-verify before changing any of them.

- **Near-identical titles at the same company in the same city scoring very
  differently.** Verified 2026-08-16 on JP Morgan, all NYC:

  | Title | `degrees` in the feed | Score |
  |---|---|---|
  | Quantitative Research **Summer Analyst** Intern | `Bachelor's, Master's` | 90 |
  | Quantitative Research **Intern** | `PhD` | 0 |
  | Quantitative Research Intern – Risk & Treasury | `Master's, PhD` | 0 |

  The scorer read the `degrees` field rather than pattern-matching the title,
  which is exactly what the "deprioritize roles requiring a Master's/PhD" rule
  asks for. The titles are nearly the same; the requirements are not. If you
  see this shape again, check `degrees` in `listings.json` **before** concluding
  the model is inconsistent.

## Constraints learned the hard way
- **Groq free tier is 12K tokens/minute** on `llama-3.3-70b-versatile` — the
  binding limit, not requests. A batch is ~1.8K tokens, so ~6 requests/min is
  the real ceiling. The pacing in `scorer.py` exists because of this; removing
  it makes the first run 429 within seconds.
- **The first run processes ~400 listings**, roughly 37K tokens and 4 minutes.
  Every day after is a handful. Daily token cap is 100K.
- **Google Docs writes are index-based and shift on every insert.** See
  CLAUDE.md before editing `docs_writer.py`.
- **The table has a hard width budget of 720pt.** Eleven columns don't fit on
  portrait Letter (468pt usable), so `ensure_table()` forces landscape Letter
  with 0.5in margins: 792 - 36 - 36 = 720pt. `COLUMN_WIDTHS_PT` must sum to at
  most that. Fixed 2026-08-17: the widths totalled 914pt, so Link and Status
  rendered off the right edge and the Apply links were unreachable.
  `tests/test_writer_flow.py` now asserts the sum — re-run it after touching
  any width. Page setup and column widths are document/table properties, not
  row content, so re-applying them to a populated doc is append-only-safe:
  `python tools/docs_writer.py --repair-layout` does exactly that and touches
  no cell text.
- **The `Status` column is mine.** Never write to it. This workflow is
  append-only by contract; anything I type in the doc must survive.

## Verifying a run
1. `gh run watch` — exit status 0
2. New rows in the doc, sorted by score, Fit Score cells shaded
3. Telegram message received
4. A bot commit updating `data/seen_ids.json` in `git log`
