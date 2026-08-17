"""
Offline check of docs_writer's index-shifting logic.

The Google Docs API applies batchUpdate requests sequentially, and every
insertText shifts the index of all content after it. This simulates that
document model and asserts every value lands in the cell it was aimed at.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from docs_writer import _fill_requests, _row_values, COLUMNS  # noqa: E402


class FakeDoc:
    """Cells laid out in document order, each occupying [start, start+len+1)."""

    def __init__(self, n_cells, cell_overhead=2):
        idx = 10  # arbitrary table start offset
        self.cells = []
        for _ in range(n_cells):
            self.cells.append({"start": idx, "text": ""})
            idx += cell_overhead

    def starts(self):
        return [c["start"] for c in self.cells]

    def apply(self, requests):
        for req in requests:
            if "insertText" in req:
                at = req["insertText"]["location"]["index"]
                text = req["insertText"]["text"]
                target = next((c for c in self.cells if c["start"] == at), None)
                assert target is not None, (
                    f"insertText at index {at} hit no cell boundary — "
                    f"index drift. Cell starts: {self.starts()}"
                )
                assert target["text"] == "", (
                    f"cell at {at} written twice: {target['text']!r} then {text!r}"
                )
                target["text"] = text
                for c in self.cells:
                    if c["start"] > at:
                        c["start"] += len(text)
            elif "updateTextStyle" in req:
                rng = req["updateTextStyle"]["range"]
                span = rng["endIndex"] - rng["startIndex"]
                target = next((c for c in self.cells if c["start"] == rng["startIndex"]), None)
                assert target is not None, f"link style range {rng} does not align to a cell"
                assert span == len(target["text"]), (
                    f"link range covers {span} chars but cell text is {len(target['text'])}"
                )


def test_single_row():
    listing = {
        "company_name": "Stripe", "title": "Software Engineer Intern, Backend",
        "category": "Software Engineering", "locations": ["New York, NY", "Seattle, WA"],
        "sponsorship": "Does Not Offer Sponsorship", "date_posted": 1755300000,
        "score": 88, "pitch": "Strong backend fit and fintech-adjacent, exactly your stated interest.",
        "url": "https://stripe.com/jobs/listing/swe-intern/123456",
    }
    values = _row_values(listing, "2026-08-16")
    assert len(values) == len(COLUMNS)

    doc = FakeDoc(len(COLUMNS))
    fills = [
        (doc.cells[i]["start"], values[i], listing["url"] if COLUMNS[i] == "Link" else None)
        for i in range(len(COLUMNS))
    ]
    doc.apply(_fill_requests(fills))

    got = [c["text"] for c in doc.cells]
    for col, want, actual in zip(COLUMNS, values, got):
        assert want == actual, f"{col}: expected {want!r}, landed {actual!r}"
    print("single row OK ->", dict(zip(COLUMNS, got)))


def test_many_rows_with_blanks_and_unicode():
    """15 rows at once (a full scoring batch), including empty cells and
    non-ASCII pitches, which is where naive index math tends to break."""
    n_rows = 15
    doc = FakeDoc(n_rows * len(COLUMNS))
    all_values, fills = [], []
    cell_i = 0
    for r in range(n_rows):
        listing = {
            "company_name": f"Company {r}",
            "title": "SWE Intern — Platform" if r % 2 else "",
            "category": "Software Engineering",
            "locations": [] if r % 3 == 0 else ["Remote"],
            "sponsorship": "" if r % 4 == 0 else "Offers Sponsorship",
            "date_posted": 1755300000 if r % 2 else None,
            "score": 95 - r * 6,
            "pitch": f"Row {r}: café/naïve unicode ✓ and a fairly long sentence to push indices.",
            "url": f"https://example.com/apply/{r}" if r % 5 else "",
        }
        values = _row_values(listing, "2026-08-16")
        all_values.append(values)
        for c in range(len(COLUMNS)):
            link = listing["url"] if (COLUMNS[c] == "Link" and listing["url"]) else None
            fills.append((doc.cells[cell_i]["start"], values[c], link))
            cell_i += 1

    doc.apply(_fill_requests(fills))

    cell_i = 0
    blanks = 0
    for r, values in enumerate(all_values):
        for c in range(len(COLUMNS)):
            want, actual = values[c], doc.cells[cell_i]["text"]
            assert want == actual, f"row {r} / {COLUMNS[c]}: expected {want!r}, got {actual!r}"
            if want == "":
                blanks += 1
            cell_i += 1
    print(f"{n_rows} rows x {len(COLUMNS)} cols OK ({blanks} intentionally blank cells skipped)")


def test_descending_order_enforced():
    """The whole scheme depends on requests running highest-index-first."""
    doc = FakeDoc(5)
    fills = [(c["start"], f"value-{i}", None) for i, c in enumerate(doc.cells)]
    reqs = [r for r in _fill_requests(fills) if "insertText" in r]
    indices = [r["insertText"]["location"]["index"] for r in reqs]
    assert indices == sorted(indices, reverse=True), f"not descending: {indices}"
    print("request ordering OK ->", indices)


if __name__ == "__main__":
    test_descending_order_enforced()
    test_single_row()
    test_many_rows_with_blanks_and_unicode()
    print("\nAll index-safety checks passed.")
