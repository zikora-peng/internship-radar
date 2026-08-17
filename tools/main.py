"""
Daily entry point. Run manually with `python tools/main.py`, or let the
GitHub Actions workflow run it on a schedule.

Flow: fetch listings -> filter to new ones -> score with Groq ->
append rows to the Google Doc table -> notify strong matches via Telegram ->
record what's been seen so it isn't processed again tomorrow.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import profile as profile_module  # noqa: E402
from docs_writer import append_matches, doc_url  # noqa: E402
from fetch_listings import fetch_all_listings, filter_relevant  # noqa: E402
from scorer import score_listings  # noqa: E402
from telegram_notify import send_summary  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
SEEN_PATH = os.path.join(DATA_DIR, "seen_ids.json")


def load_seen():
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH) as f:
            return set(json.load(f))
    return set()


def save_seen(seen_ids):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SEEN_PATH, "w") as f:
        json.dump(sorted(seen_ids), f, indent=2)


def main():
    doc_id = os.environ.get("GOOGLE_DOC_ID")
    if not doc_id:
        raise SystemExit(
            "GOOGLE_DOC_ID is not set. Create the Google Doc, share it with the "
            "service account email as Editor, and set the id (see README.md)."
        )

    seen = load_seen()

    all_listings = fetch_all_listings()
    relevant = filter_relevant(all_listings, terms=profile_module.TERMS)
    new_listings = [item for item in relevant if item["id"] not in seen]

    print(
        f"Fetched {len(all_listings)} total listings, {len(relevant)} relevant, "
        f"{len(new_listings)} new since last run."
    )

    if not new_listings:
        send_summary(0, [], profile_module.ALERT_THRESHOLD, doc_url(doc_id))
        return

    scores = score_listings(new_listings, profile_module.PROFILE)

    scored_listings = []
    pending = []
    for item in new_listings:
        info = scores.get(item["id"], {"score": 0, "pitch": "(not scored)", "failed": True})
        merged = {**item, **info}
        # A listing we could not score gets NO row and is NOT marked seen. Its
        # score is unknown, not zero — writing a placeholder row would freeze it
        # there permanently, since append-only means we can never revise it and
        # a second row tomorrow would be a duplicate. Leaving it unseen is the
        # only recovery path that preserves the append-only contract.
        (pending if merged.get("failed") else scored_listings).append(merged)

    # Written before notifying: if Telegram is down we still keep the record.
    written_ids = append_matches(doc_id, scored_listings)

    strong_matches = sorted(
        (m for m in scored_listings if m["score"] >= profile_module.ALERT_THRESHOLD),
        key=lambda m: m["score"],
        reverse=True,
    )
    send_summary(
        len(new_listings), strong_matches, profile_module.ALERT_THRESHOLD, doc_url(doc_id),
        pending_count=len(pending),
    )

    # Only mark what actually landed in the doc. Anything that failed to score
    # never reached append_matches, and anything that failed to write is absent
    # from written_ids — so both stay unseen and are retried on the next run.
    seen.update(written_ids)
    save_seen(seen)

    write_failures = len(scored_listings) - len(written_ids)
    if write_failures:
        print(f"Note: {write_failures} listing(s) failed to write and will be retried next run.")
    if pending:
        print(f"Note: {len(pending)} listing(s) could not be scored — left unseen "
              f"for re-score on the next run, no rows written.")
    print(f"Done. {len(strong_matches)} strong match(es) out of {len(new_listings)} new.")


if __name__ == "__main__":
    main()
