"""
Profile + scoring config.

The `PROFILE` text below is sent to the model as context for scoring every new
listing, so specificity drives score quality directly. What ships in this repo
is a **placeholder**: a real profile states where you live, what you're studying
and your work-authorisation status, and none of that belongs in a public repo.

Supply your real one by either route — both are gitignored / secret:

  1. `PROFILE_TEXT` environment variable (use a GitHub Actions secret in CI)
  2. `tools/profile_local.py` defining `PROFILE = \"\"\"...\"\"\"` (for local runs)

The env var wins if both are set. With neither, the placeholder is used and
scoring still works — it just scores against a generic candidate.
"""
import os

PROFILE = """
- Second-year Computer Science student, part-way through a standard core
  sequence (data structures, discrete maths, systems fundamentals).
- Core skills: Java and Python, comfortable with OOP, recursion and basic
  data structures. Building portfolio projects rather than shipping
  production code so far.
- Looking for: Summer 2027 software engineering internships. Open to
  backend, full-stack, or general SWE roles.
- Location: score fully remote roles and roles in the candidate's home metro
  highest; major tech hubs mid-range but only at companies large enough to
  offer intern housing or relocation; everywhere else low, since an unfunded
  cross-country move is not realistic for this candidate.
- Deprioritize (score low): roles requiring a Master's or PhD, roles with no
  software engineering component, and roles clearly aimed at candidates with
  two or more prior internships.
"""

# Only send a Telegram alert for listings scored at or above this threshold.
# Every new listing found is still logged to the Google Doc regardless of score.
ALERT_THRESHOLD = 75

# Which academic terms to pull from the feed. Add "Fall 2026" etc. if you
# also want to see off-cycle/co-op postings.
TERMS = ["Summer 2027"]


# --------------------------------------------------------------------------
# Real-profile override. Keeps personal details out of version control while
# leaving the scoring mechanism itself fully readable in the repo.
# --------------------------------------------------------------------------

_env_profile = os.environ.get("PROFILE_TEXT")
if _env_profile and _env_profile.strip():
    PROFILE = _env_profile
else:
    try:
        from profile_local import PROFILE  # type: ignore  # noqa: F401
    except ImportError:
        pass
