# Workflow: Troubleshoot a Failed Run

## Objective
Diagnose and fix a failed daily scan without losing data or my time.

## First: get the actual error
```bash
gh run list --workflow=daily_scan.yml --limit 5
gh run view <run-id> --log-failed
```
Read the full trace before changing anything. Don't guess from the step name.

## Diagnosis table
| Symptom in the log | Cause | Fix |
|---|---|---|
| `KeyError: 'GROQ_API_KEY'` or similar | Secret missing or misnamed | `gh secret list`, re-set with `tools/deploy.sh` |
| `404` on model name | Groq retired the model | Check console.groq.com, update `MODEL` in `tools/scorer.py` |
| `429` / `rate_limit_exceeded` repeatedly | Pacing insufficient (e.g. `TERMS` widened) | Lower `BATCH_SIZE` or raise pacing headroom in `tools/scorer.py`. Do NOT add a card to Groq. |
| `403 caller does not have permission` | Doc not shared with the service account, or Docs API disabled | Re-run `tools/setup_google.sh` |
| `Invalid requests[N]: insertText` / index errors | Docs index drift | Read the Docs API section in CLAUDE.md. Re-read the doc before computing indices. |
| Rows appear blank in the doc | Fill phase failed after row insert | Rollback should have handled it. Check `_delete_rows` ran. |
| Run passes, no Telegram message | Notify fails soft by design | Check the log for the warning. Verify token and chat id. |
| `batch@N: malformed JSON from the model, retrying once` then `still malformed, splitting` | The model emitted unparseable JSON (usually an unescaped quote inside `pitch`, or a response truncated near `MAX_OUTPUT_TOKENS`). Both scale with response length, so a half-size batch usually parses. | Normal self-healing, not a failure. `scorer.py` tries once, retries once, then splits the batch in half and tries each half — bounded at 6 calls per batch. Only escalates on `JSONDecodeError`; an auth/network error fails every half identically, so it gives up immediately rather than burning quota. |
| `Warning: scoring batch batch@N failed:` after the split | Both halves still unparseable, or a non-JSON error (401/network). | Those listings get `failed=True`. As of 2026-08-16 they are **not written to the doc and not marked seen** — they simply reappear as new on the next run. Nothing to repair; check the Telegram digest for the `pending re-score` count. If it stays non-zero for several days the scorer is genuinely broken — check the Groq key first (a revoked key still returns 200 on `/models` but 401 on `/chat/completions`). |
| `git push` fails in the commit step | `permissions: contents: write` missing | Check `.github/workflows/daily_scan.yml` |

## Recovery invariants — never violate these
- **Never mark a listing seen if it wasn't written.** Failed writes must retry
  tomorrow, not vanish.
- **Never rewrite existing doc rows** to "fix" things. Append-only. My `Status`
  notes must survive.
- If duplicates got written, remove the extra rows from the doc by hand and
  leave `seen_ids.json` alone — it's already correct.

## Testing a fix without burning API credits
Offline tests need no credentials and hit no paid APIs:
```bash
python tests/test_index_logic.py      # Docs index arithmetic
python tests/test_writer_flow.py      # full writer flow vs. simulated Docs API
python tests/test_scorer_limits.py    # rate-limit pacing vs. free-tier caps
```
Run these before any live re-run. Groq's free tier is 1,000 requests/day —
ample, but don't burn it on debugging that a mock could catch.

## After fixing
Update the relevant workflow file with what you learned — a new edge case row,
a corrected constraint, a rate limit you discovered. That's the point of the
loop. Ask before restructuring a workflow; adding a learned constraint is fine.
