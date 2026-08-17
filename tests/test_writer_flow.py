import os, sys, random
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import docs_writer as dw
from fake_docs_service import FakeDocsService

fake = FakeDocsService()
dw._service = lambda: fake          # bypass real auth/network

def listing(i, score, url=True):
    return {
        "id": f"id-{i}", "company_name": f"Company {i}",
        "title": "Software Engineer Intern, Distributed Systems",
        "category": "Software Engineering",
        "locations": ["New York, NY", "Remote"] if i % 2 else [],
        "sponsorship": "Offers Sponsorship" if i % 3 else "",
        "date_posted": 1755300000 if i % 2 else None,
        "score": score, "pitch": f"Pitch {i}: café ✓ solid backend fit for a second-year student.",
        "url": f"https://boards.greenhouse.io/co{i}/jobs/{100000+i}" if url else "",
    }

# ---------- run 1: fresh doc, 47 listings (forces 3 chunks) ----------
random.seed(3)
batch1 = [listing(i, random.randint(10, 99)) for i in range(47)]
written = dw.append_matches("DOC", batch1)
assert written == {l["id"] for l in batch1}, "not all ids reported written"
assert len(fake.table) == 1 + 47, f"expected 48 rows, got {len(fake.table)}"
assert fake.table[0] == dw.COLUMNS, f"header row wrong: {fake.table[0]}"
print(f"run 1: {len(fake.table)-1} data rows, header intact, batch sizes={fake.batch_sizes[:6]}...")

# ---------- page + column geometry (the table must fit on the page) ----------
total_w = sum(dw.COLUMN_WIDTHS_PT.values())
assert set(dw.COLUMN_WIDTHS_PT) == set(dw.COLUMNS), "a column has no width, or vice versa"
assert total_w <= dw.USABLE_WIDTH_PT, (
    f"columns total {total_w}pt but only {dw.USABLE_WIDTH_PT}pt is usable — "
    f"the rightmost columns would render off the page"
)
assert fake.document_style is not None, "page setup was never applied"
ps = fake.document_style["pageSize"]
assert ps["width"]["magnitude"] > ps["height"]["magnitude"], "page is not landscape"
assert dw.USABLE_WIDTH_PT == (
    ps["width"]["magnitude"]
    - fake.document_style["marginLeft"]["magnitude"]
    - fake.document_style["marginRight"]["magnitude"]
), "USABLE_WIDTH_PT disagrees with the page setup actually sent"
assert fake.col_widths == {i: dw.COLUMN_WIDTHS_PT[n] for i, n in enumerate(dw.COLUMNS)}, (
    f"column widths not applied as configured: {fake.col_widths}"
)
print(f"layout: landscape {ps['width']['magnitude']:.0f}x{ps['height']['magnitude']:.0f}pt, "
      f"columns {total_w}pt of {dw.USABLE_WIDTH_PT}pt usable ({dw.USABLE_WIDTH_PT - total_w}pt spare)")

# rows must be sorted by score descending
scores = [int(r[dw.COLUMNS.index("Fit Score")]) for r in fake.table[1:]]
assert scores == sorted(scores, reverse=True), "rows not sorted by score desc"
print(f"run 1: scores sorted desc, top={scores[:5]}")

# score shading applied to the right band
score_col = dw.COLUMNS.index("Fit Score")
shaded = {r: bg for (r, c), bg in fake.cell_bg.items() if c == score_col and r > 0}
for r, sc in enumerate(scores, start=1):
    if sc >= 70:   assert shaded.get(r) == dw.SCORE_BG_STRONG, f"row {r} score {sc} not green"
    elif sc >= 45: assert shaded.get(r) == dw.SCORE_BG_MEDIUM, f"row {r} score {sc} not amber"
    else:          assert r not in shaded, f"row {r} score {sc} should be unshaded"
print(f"run 1: score shading correct ({len(shaded)} cells shaded)")

# ---------- run 2: append to existing doc, must not disturb run 1 ----------
snapshot = [row[:] for row in fake.table]
batch2 = [listing(100+i, random.randint(10, 99)) for i in range(6)]
dw.append_matches("DOC", batch2)
assert len(fake.table) == 48 + 6, f"expected 54 rows, got {len(fake.table)}"
assert [r[:] for r in fake.table[:48]] == snapshot, "existing rows were modified!"
print(f"run 2: appended 6 rows, previous {len(snapshot)} rows byte-identical (append-only holds)")

# ---------- content spot check ----------
row = fake.table[49]
print("\nsample appended row:")
for col, val in zip(dw.COLUMNS, row):
    print(f"   {col:<12} | {val[:60]}")

# link styling: one link per row that has a url
links = [ts for ts in fake.text_styles if "link" in ts.get("textStyle", {})]
urls = {ts["textStyle"]["link"]["url"] for ts in links}
assert len(urls) == 53, f"expected 53 distinct links, got {len(urls)}"
assert all(u.startswith("https://") for u in urls)
print(f"\nlinks: {len(links)} hyperlinked cells, {len(urls)} distinct URLs, all https")

# ---------- run 3: simulate a mid-run API failure ----------
calls = {"n": 0}
real_batch = fake.batchUpdate
def flaky(documentId=None, body=None):
    calls["n"] += 1
    if calls["n"] == 2:
        raise RuntimeError("simulated 503 from Docs API")
    return real_batch(documentId=documentId, body=body)
fake.batchUpdate = flaky
before = len(fake.table)
batch3 = [listing(200+i, 80) for i in range(30)]   # 2 chunks
written3 = dw.append_matches("DOC", batch3)
print(f"\nrun 3 (injected failure): {len(written3)}/30 ids reported written, "
      f"doc grew by {len(fake.table)-before} rows")
assert len(written3) < 30, "failure should have been reported"
assert len(written3) == len(fake.table) - before, "reported ids must match rows actually added"
print("run 3: reported ids exactly match rows written — failed listings stay unseen and retry")

# ---------- run 4: repair_layout on a populated doc must not touch content ----------
fake.batchUpdate = real_batch
fake.col_widths = {}
fake.document_style = None
before_rows = [row[:] for row in fake.table]
before_bg = dict(fake.cell_bg)
before_styles = len(fake.text_styles)

dw.repair_layout("DOC", service=fake)

assert [r[:] for r in fake.table] == before_rows, "repair_layout modified row content!"
assert fake.cell_bg == before_bg, "repair_layout changed cell shading"
assert len(fake.text_styles) == before_styles, "repair_layout restyled text"
assert fake.document_style is not None, "repair_layout did not set page geometry"
assert fake.col_widths == {i: dw.COLUMN_WIDTHS_PT[n] for i, n in enumerate(dw.COLUMNS)}, (
    "repair_layout did not re-apply every column width"
)
status_col = dw.COLUMNS.index("Status")
assert all(r[status_col] == "" for r in fake.table[1:]), "Status column was written to"
print(f"run 4: repair_layout re-applied page + widths, all {len(fake.table)-1} rows byte-identical")

print("\nAll writer-flow checks passed.")
