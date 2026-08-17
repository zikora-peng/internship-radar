"""
Fetch and filter internship listings from the SimplifyJobs/Summer2027-Internships
GitHub repo. This repo is community + Simplify maintained and updated roughly
hourly — no scraping of LinkedIn/Handshake required, and no ToS risk.

Source: https://github.com/SimplifyJobs/Summer2027-Internships
"""
import json
import urllib.request

LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/"
    "Summer2027-Internships/dev/.github/scripts/listings.json"
)


def fetch_all_listings():
    """Download the full listings feed as a list of dicts."""
    req = urllib.request.Request(
        LISTINGS_URL, headers={"User-Agent": "internship-radar/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def filter_relevant(listings, terms=("Summer 2027",), categories=None):
    """
    Keep only postings that are:
      - active (not closed)
      - visible (not hidden/removed)
      - matching one of the given academic terms
      - matching one of the given categories, if provided
    """
    out = []
    for item in listings:
        if not item.get("active", False) or not item.get("is_visible", False):
            continue
        if terms and not any(t in item.get("terms", []) for t in terms):
            continue
        if categories and item.get("category") not in categories:
            continue
        out.append(item)
    return out
