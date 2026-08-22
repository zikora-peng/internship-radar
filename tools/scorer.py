"""
Score newly-found internship listings against the user's profile using
Groq's OpenAI-compatible chat completions API. Listings are sent in
batches to keep prompt size and request count down, and the model is
asked to return strict JSON so results can be parsed safely.

Groq's free tier requires no credit card and comfortably covers this
workload (a handful of batched requests/day, well under its per-model
RPM/TPD caps) — see README.md for current limits.
"""
import json
import os
import random
import time
from collections import deque

from groq import Groq

MODEL = "llama-3.1-8b-instant"
BATCH_SIZE = 15
MAX_OUTPUT_TOKENS = 2000

# Groq free-tier caps for this model (verified Aug 2026; check console.groq.com
# if these change): 30 req/min, 1,000 req/day, 12K tokens/min, 100K tokens/day.
#
# Tokens-per-minute is the binding constraint here, not requests. A batch runs
# roughly 1.2K input + 0.6K output tokens, so ~6 requests/min is the real
# ceiling — firing 28 batches back-to-back on the first run would 429 almost
# immediately. We stay under 90% of the limit and self-pace.
TPM_LIMIT = 12000
RPM_LIMIT = 30
SAFETY = 0.9
MAX_RETRIES = 4

# Placeholder pitches for listings we could not score. Both carry failed=True so
# callers can tell "we have no judgement on this" apart from "the model scored
# it 0". main.py relies on that flag to keep these OUT of the doc and OUT of
# seen_ids, so they retry on the next run instead of freezing at 0 forever.
FAILED_PITCH = "(scoring failed — review manually)"
OMITTED_PITCH = "(not returned by scorer — review manually)"


def _failed(pitch):
    return {"score": 0, "pitch": pitch, "failed": True}


class _RateLimiter:
    """Sliding 60-second window over both token and request usage."""

    def __init__(self):
        self.events = deque()  # (timestamp, tokens)

    def _prune(self, now):
        while self.events and now - self.events[0][0] > 60:
            self.events.popleft()

    def wait_for(self, tokens):
        while True:
            now = time.monotonic()
            self._prune(now)
            used_tokens = sum(t for _, t in self.events)
            if (
                used_tokens + tokens <= TPM_LIMIT * SAFETY
                and len(self.events) + 1 <= RPM_LIMIT * SAFETY
            ):
                return
            # Sleep until the oldest event ages out of the window.
            sleep_for = max(0.5, 60 - (now - self.events[0][0]) + 0.25)
            print(f"  pacing for Groq rate limit: sleeping {sleep_for:.0f}s")
            time.sleep(sleep_for)

    def record(self, tokens):
        self.events.append((time.monotonic(), tokens))


_limiter = _RateLimiter()
_client = None


def _estimate_tokens(prompt):
    """~4 characters per token, plus the output we've reserved."""
    return len(prompt) // 4 + MAX_OUTPUT_TOKENS


def _is_rate_limit(exc):
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


def _get_client():
    global _client
    if _client is None:
        _client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _client


def _build_prompt(batch, profile_text):
    listing_blocks = []
    for item in batch:
        listing_blocks.append(
            f"id: {item['id']}\n"
            f"company: {item.get('company_name', '')}\n"
            f"title: {item.get('title', '')}\n"
            f"category: {item.get('category', '')}\n"
            f"locations: {', '.join(item.get('locations', []))}\n"
            f"sponsorship: {item.get('sponsorship', 'Unknown')}\n"
            f"degrees: {', '.join(item.get('degrees', []) or ['Not specified'])}"
        )
    listings_block = "\n---\n".join(listing_blocks)

    return f"""You are screening internship listings for this candidate:
{profile_text}

For each listing below, return:
- "score": a fit score from 0-100 (how well it matches the candidate's
  skills, interests, and constraints)
- "pitch": one sentence on why they should apply, or why it's a stretch/
  weak fit if the score is low

Listings:
{listings_block}

Respond with ONLY a JSON array, no markdown code fences, no commentary.
Format: [{{"id": "...", "score": 0, "pitch": "..."}}, ...]
Return exactly one object per listing above."""


def _clean_json_text(raw_text):
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    return cleaned.strip()


def _call_model(client, prompt, estimated):
    """One paced API call. Retries 429s internally; raises anything else."""
    last_error = None
    for attempt in range(MAX_RETRIES):
        _limiter.wait_for(estimated)
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            )
            # Charge the limiter with real usage when the API reports it,
            # so the pacing self-corrects instead of drifting on estimates.
            usage = getattr(response, "usage", None)
            _limiter.record(getattr(usage, "total_tokens", None) or estimated)
            return response.choices[0].message.content
        except Exception as e:  # noqa: BLE001
            last_error = e
            if _is_rate_limit(e) and attempt < MAX_RETRIES - 1:
                # Exponential backoff with jitter; a 429 means our estimate
                # was low, so record it and let the window drain.
                _limiter.record(estimated)
                backoff = (2 ** attempt) * 15 + random.uniform(0, 5)
                print(f"  rate limited, backing off {backoff:.0f}s "
                      f"(attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(backoff)
                continue
            raise
    raise last_error


def _attempt(client, batch, profile_text):
    """Score `batch` once. Returns the parsed JSON list, or raises."""
    prompt = _build_prompt(batch, profile_text)
    raw_text = _call_model(client, prompt, _estimate_tokens(prompt))
    return json.loads(_clean_json_text(raw_text))


def _score_batch(client, batch, profile_text, results, label, allow_split=True):
    """
    Score one batch into `results`, escalating on malformed JSON:
    try once, retry once, then split in half and try each half.

    Bad JSON is usually a truncated or unescaped-quote response, and both scale
    with response length — so a smaller batch frequently parses cleanly where
    the full one did not. Splitting salvages most of a batch that would
    otherwise have been written off entirely.

    Only JSONDecodeError escalates. An auth or network error would fail
    identically on every half, so retrying those would just burn quota.
    """
    parsed = None
    last_error = None
    for attempt in (1, 2):
        try:
            parsed = _attempt(client, batch, profile_text)
            break
        except json.JSONDecodeError as e:
            last_error = e
            if attempt == 1:
                print(f"  {label}: malformed JSON from the model, retrying once")
        except Exception as e:  # noqa: BLE001
            last_error = e
            break

    if parsed is None:
        if (allow_split and len(batch) > 1
                and isinstance(last_error, json.JSONDecodeError)):
            mid = len(batch) // 2
            print(f"  {label}: still malformed, splitting {len(batch)} into "
                  f"{mid}+{len(batch) - mid}")
            _score_batch(client, batch[:mid], profile_text, results,
                         label + "a", allow_split=False)
            _score_batch(client, batch[mid:], profile_text, results,
                         label + "b", allow_split=False)
            return
        print(f"Warning: scoring batch {label} failed: {last_error}")
        for item in batch:
            results.setdefault(item["id"], _failed(FAILED_PITCH))
        return

    try:
        for entry in parsed:
            results[entry["id"]] = {
                "score": int(entry.get("score", 0)),
                "pitch": entry.get("pitch", ""),
            }
    except (TypeError, KeyError, ValueError) as e:
        print(f"Warning: malformed scoring response in {label}: {e}")

    # Any listing the model silently omitted still needs a verdict.
    for item in batch:
        results.setdefault(item["id"], _failed(OMITTED_PITCH))


def score_listings(new_listings, profile_text):
    """Returns {listing_id: {"score": int, "pitch": str, "failed"?: True}}."""
    client = _get_client()
    results = {}
    for i in range(0, len(new_listings), BATCH_SIZE):
        batch = new_listings[i : i + BATCH_SIZE]
        _score_batch(client, batch, profile_text, results, f"batch@{i}")
    return results
