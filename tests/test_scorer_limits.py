import os, sys, time, json, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import scorer

# make sleeps instant but track simulated elapsed time
sim = {"t": 0.0}
scorer.time.monotonic = lambda: sim["t"]
scorer.time.sleep = lambda s: sim.__setitem__("t", sim["t"] + s)

listings = [{"id": f"id-{i}", "company_name": f"Co{i}", "title": "SWE Intern",
             "category": "Software Engineering", "locations": ["NY"],
             "sponsorship": "Other", "degrees": ["Bachelor's"]} for i in range(407)]
PROFILE = "A second-year CS student. " * 20

class FakeUsage:
    def __init__(s, n): s.total_tokens = n
class FakeResp:
    def __init__(s, content, tokens):
        s.choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
        s.usage=FakeUsage(tokens)

calls = {"n": 0, "tokens": [], "times": []}
class FakeCompletions:
    def create(self, model=None, max_tokens=None, temperature=None, messages=None):
        calls["n"] += 1
        calls["times"].append(sim["t"])
        prompt = messages[0]["content"]
        ids = [l.split("id: ")[1].split("\n")[0] for l in prompt.split("---")]
        tokens = len(prompt)//4 + 600
        calls["tokens"].append(tokens)
        body = json.dumps([{"id": i, "score": 80, "pitch": "ok"} for i in ids])
        return FakeResp(body, tokens)
class FakeClient:
    chat = types.SimpleNamespace(completions=FakeCompletions())
scorer._get_client = lambda: FakeClient()

res = scorer.score_listings(listings, PROFILE)
print(f"listings={len(listings)}  batches={calls['n']}  scored={len(res)}")
assert len(res) == len(listings), "every listing must get a result"
assert all(r["score"] == 80 for r in res.values())

total_tokens = sum(calls["tokens"])
print(f"total tokens ~{total_tokens:,} (free tier TPD = 100,000)")
print(f"simulated wall time: {sim['t']/60:.1f} min")

# verify no 60s window ever exceeded the TPM cap
worst = 0
for i, t in enumerate(calls["times"]):
    window = sum(tok for tt, tok in zip(calls["times"], calls["tokens"]) if t-60 < tt <= t)
    worst = max(worst, window)
print(f"peak tokens in any 60s window: {worst:,} (limit {scorer.TPM_LIMIT:,})")
assert worst <= scorer.TPM_LIMIT, "TPM limit exceeded!"

worst_rpm = max(sum(1 for tt in calls["times"] if t-60 < tt <= t) for t in calls["times"])
print(f"peak requests in any 60s window: {worst_rpm} (limit {scorer.RPM_LIMIT})")
assert worst_rpm <= scorer.RPM_LIMIT

# --- 429 handling ---
print("\n--- simulating persistent 429s ---")
sim["t"] = 0.0; scorer._limiter = scorer._RateLimiter()
class Failing(FakeCompletions):
    def create(self, **kw): raise Exception("Error code: 429 - rate_limit_exceeded")
FakeClient.chat = types.SimpleNamespace(completions=Failing())
res2 = scorer.score_listings(listings[:15], PROFILE)
print(f"scored {len(res2)}/15 despite 429s; sample pitch: {list(res2.values())[0]['pitch']!r}")
assert len(res2) == 15, "run must not lose listings on rate-limit failure"

# --- model returns fewer entries than asked ---
print("\n--- simulating model omitting listings ---")
sim["t"] = 0.0; scorer._limiter = scorer._RateLimiter()
class Partial(FakeCompletions):
    def create(self, **kw):
        prompt = kw["messages"][0]["content"]
        ids = [l.split("id: ")[1].split("\n")[0] for l in prompt.split("---")][:5]
        return FakeResp(json.dumps([{"id": i, "score": 60, "pitch": "ok"} for i in ids]), 1500)
FakeClient.chat = types.SimpleNamespace(completions=Partial())
res3 = scorer.score_listings(listings[:15], PROFILE)
print(f"scored {len(res3)}/15 (model returned only 5); "
      f"omitted are flagged failed=True — no row is written and they stay unseen")
assert len(res3) == 15
print("\nAll scorer rate-limit checks passed.")
