"""
Append newly scored internships as rows in a table inside a Google Doc.

Replaces the old openpyxl/Excel writer. Same contract as before: this only
ever APPENDS — existing rows (including any Status notes you've typed into
the doc yourself) are never touched or overwritten.

Auth model: a Google Cloud **service account**. This runs headless in GitHub
Actions, so an interactive OAuth consent flow is the wrong fit — a service
account key is a single secret with no refresh-token dance.

Important: the doc is created by YOU (in your own Drive) and shared with the
service account's email as Editor. We deliberately do NOT have the service
account create the doc itself — service accounts have no Drive storage quota
of their own, which makes doc creation fail in exactly the annoying,
hard-to-debug way you'd hit on first run.

Env vars:
  GOOGLE_SERVICE_ACCOUNT_JSON  raw JSON of the key file, or a path to it
  GOOGLE_DOC_ID                the doc id from its URL:
                               docs.google.com/document/d/<THIS_PART>/edit
"""
import json
import os
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/documents"]

DOC_HEADING = "Internship Radar — Summer 2027"

COLUMNS = [
    "Date Added", "Company", "Role", "Category", "Location",
    "Sponsorship", "Date Posted", "Fit Score", "Pitch", "Link", "Status",
]

# Page geometry. Eleven columns do not fit on a portrait Letter page — that's
# only 468pt of usable width at 1in margins — so the doc is forced to LANDSCAPE
# Letter with 0.5in margins, giving 792 - 36 - 36 = 720pt to work with.
PAGE_WIDTH_PT = 792
PAGE_HEIGHT_PT = 612
MARGIN_PT = 36
USABLE_WIDTH_PT = PAGE_WIDTH_PT - 2 * MARGIN_PT     # 720

# Google Docs column widths are in points (not Excel character units).
# These total exactly USABLE_WIDTH_PT. Keep the sum at or under it: overflow
# pushes the rightmost columns (Link, Status) off the right edge of the page,
# where they are invisible — the widths before this totalled 914pt and did
# exactly that. tests/test_writer_flow.py asserts the sum, so re-run it after
# changing any of these.
COLUMN_WIDTHS_PT = {
    "Date Added": 50, "Company": 75, "Role": 105, "Category": 50,
    "Location": 80, "Sponsorship": 55, "Date Posted": 50,
    "Fit Score": 38, "Pitch": 122, "Link": 45, "Status": 50,
}

# The Link cell shows a short clickable label instead of the raw URL —
# application URLs are routinely 100+ chars and would blow out the column.
# Flip SHOW_FULL_URL to True if you'd rather see the whole thing.
LINK_LABEL = "Apply"
SHOW_FULL_URL = False

# Rows written per batchUpdate call. The first run appends ~400 listings at
# once; at 11 cells/row that's ~4,400 requests, which is far past what a single
# batchUpdate will accept. Chunking also means a failure late in a big run
# doesn't discard the rows already committed.
ROWS_PER_BATCH = 20

HEADER_BG = {"red": 0.12, "green": 0.16, "blue": 0.22}      # slate, matches old Excel header
SCORE_BG_STRONG = {"red": 0.83, "green": 0.93, "blue": 0.84}  # green,  score >= 70
SCORE_BG_MEDIUM = {"red": 1.0, "green": 0.95, "blue": 0.80}   # amber,  score >= 45
STRONG_CUTOFF = 70
MEDIUM_CUTOFF = 45


# --------------------------------------------------------------------------
# auth / plumbing
# --------------------------------------------------------------------------

def _credentials():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not set. Put the service account "
            "key JSON in that env var (or point it at the key file path)."
        )
    if os.path.exists(raw):
        return service_account.Credentials.from_service_account_file(raw, scopes=SCOPES)
    return service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )


def _service():
    return build("docs", "v1", credentials=_credentials(), cache_discovery=False)


def doc_url(doc_id):
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def _unix_to_date(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def _find_table(doc):
    """Return the first table element in the document body, or None."""
    for element in doc.get("body", {}).get("content", []):
        if "table" in element:
            return element
    return None


# --------------------------------------------------------------------------
# page + table layout
#
# Both of these are *document/table properties*, not row content: they describe
# the page and the columns, never the text inside a cell. That's what makes it
# safe to re-apply them to a doc that already has rows in it — the append-only
# contract is about row text, and none of this touches any.
# --------------------------------------------------------------------------

def _pt(magnitude):
    return {"magnitude": magnitude, "unit": "PT"}


def _page_setup_request():
    """Force landscape Letter with 0.5in margins, so the table has 720pt."""
    return {
        "updateDocumentStyle": {
            "documentStyle": {
                "pageSize": {"width": _pt(PAGE_WIDTH_PT), "height": _pt(PAGE_HEIGHT_PT)},
                "marginTop": _pt(MARGIN_PT),
                "marginBottom": _pt(MARGIN_PT),
                "marginLeft": _pt(MARGIN_PT),
                "marginRight": _pt(MARGIN_PT),
            },
            "fields": "pageSize,marginTop,marginBottom,marginLeft,marginRight",
        }
    }


def _column_width_requests(table_start):
    """One FIXED_WIDTH request per column. Keyed by table coordinates, so these
    are immune to text-index shifting and can be batched in any order."""
    return [
        {
            "updateTableColumnProperties": {
                "tableStartLocation": {"index": table_start},
                "columnIndices": [i],
                "tableColumnProperties": {
                    "widthType": "FIXED_WIDTH",
                    "width": _pt(COLUMN_WIDTHS_PT[name]),
                },
                "fields": "widthType,width",
            }
        }
        for i, name in enumerate(COLUMNS)
    ]


# --------------------------------------------------------------------------
# index-safe cell filling
# --------------------------------------------------------------------------

def _cell_start_index(cell):
    """Index of the first insertion point inside a table cell."""
    return cell["content"][0]["startIndex"]


def _fill_requests(cells_and_values):
    """
    Build insertText requests for a set of (cell_start_index, text, link_url)
    tuples.

    Every insertion shifts the index of everything after it, so we apply them
    in DESCENDING index order. A request that inserts at a low index can't
    invalidate one that already ran at a higher index, so each request's
    coordinates stay correct at the moment it executes.

    Any link styling for a cell is emitted immediately after that cell's
    insertText, while its range is still known-good.
    """
    requests = []
    for start_index, text, link_url in sorted(
        cells_and_values, key=lambda t: t[0], reverse=True
    ):
        if not text:
            continue  # the API rejects empty insertText; blank cells stay blank
        requests.append(
            {"insertText": {"location": {"index": start_index}, "text": text}}
        )
        if link_url:
            requests.append({
                "updateTextStyle": {
                    "range": {
                        "startIndex": start_index,
                        "endIndex": start_index + len(text),
                    },
                    "textStyle": {
                        "link": {"url": link_url},
                        "foregroundColor": {
                            "color": {"rgbColor": {"red": 0.05, "green": 0.33, "blue": 0.75}}
                        },
                        "underline": True,
                    },
                    "fields": "link,foregroundColor,underline",
                }
            })
    return requests


def _cell_style_request(table_start, row, col, bg=None, bold=None, color=None):
    """Cell styling keys off table coordinates, not text offsets, so these are
    immune to the index-shifting above and can be batched in any order."""
    reqs = []
    if bg is not None:
        reqs.append({
            "updateTableCellStyle": {
                "tableRange": {
                    "tableCellLocation": {
                        "tableStartLocation": {"index": table_start},
                        "rowIndex": row,
                        "columnIndex": col,
                    },
                    "rowSpan": 1,
                    "columnSpan": 1,
                },
                "tableCellStyle": {"backgroundColor": {"color": {"rgbColor": bg}}},
                "fields": "backgroundColor",
            }
        })
    return reqs


# --------------------------------------------------------------------------
# first-run setup
# --------------------------------------------------------------------------

def ensure_table(service, doc_id):
    """
    Create the heading + header row on first run. No-op afterwards.
    Returns the document dict as it stands after any setup.
    """
    doc = service.documents().get(documentId=doc_id).execute()
    if _find_table(doc) is not None:
        return doc

    print("No table found in the doc — creating heading and header row.")

    # Page setup first — landscape, narrow margins — then the heading, then an
    # empty 1-row table at the end of the body.
    service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [
            _page_setup_request(),
            {"insertText": {"location": {"index": 1}, "text": DOC_HEADING + "\n"}},
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": 1, "endIndex": 1 + len(DOC_HEADING)},
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "fields": "namedStyleType",
                }
            },
            {
                "insertTable": {
                    "endOfSegmentLocation": {"segmentId": ""},
                    "rows": 1,
                    "columns": len(COLUMNS),
                }
            },
        ]},
    ).execute()

    doc = service.documents().get(documentId=doc_id).execute()
    table_element = _find_table(doc)
    table_start = table_element["startIndex"]
    header_cells = table_element["table"]["tableRows"][0]["tableCells"]

    fills = [
        (_cell_start_index(header_cells[i]), COLUMNS[i], None)
        for i in range(len(COLUMNS))
    ]
    requests = _fill_requests(fills)

    # Column widths + header shading + white bold header text.
    requests.extend(_column_width_requests(table_start))
    for i in range(len(COLUMNS)):
        requests.extend(_cell_style_request(table_start, 0, i, bg=HEADER_BG))

    service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute()

    # Header text style is a separate pass: re-fetch so the ranges below
    # reflect the text we just inserted.
    doc = service.documents().get(documentId=doc_id).execute()
    table_element = _find_table(doc)
    style_requests = []
    for cell in table_element["table"]["tableRows"][0]["tableCells"]:
        para = cell["content"][0]
        style_requests.append({
            "updateTextStyle": {
                "range": {
                    "startIndex": para["startIndex"],
                    "endIndex": para["endIndex"] - 1,
                },
                "textStyle": {
                    "bold": True,
                    "foregroundColor": {
                        "color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}
                    },
                    "fontSize": {"magnitude": 9, "unit": "PT"},
                },
                "fields": "bold,foregroundColor,fontSize",
            }
        })
    if style_requests:
        service.documents().batchUpdate(
            documentId=doc_id, body={"requests": style_requests}
        ).execute()

    return service.documents().get(documentId=doc_id).execute()


def repair_layout(doc_id, service=None):
    """
    Re-apply page setup and column widths to a doc that already has rows.

    One-off repair for docs created before the widths were fixed: the old set
    totalled 914pt on a 468pt portrait page, so Link and Status rendered off the
    right edge and the Apply links were unreachable. Safe to run repeatedly.

    This does not violate the append-only contract. It emits exactly two kinds
    of request — updateDocumentStyle and updateTableColumnProperties — neither
    of which can read or write cell text. No row is added, removed, reordered or
    edited, and the Status column's contents are untouched.
    """
    service = service or _service()
    doc = service.documents().get(documentId=doc_id).execute()
    table_element = _find_table(doc)
    if table_element is None:
        raise RuntimeError(
            "No table in the doc — nothing to repair. Run the daily scan first; "
            "ensure_table() applies this layout at creation time."
        )

    requests = [_page_setup_request()]
    requests.extend(_column_width_requests(table_element["startIndex"]))
    service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute()

    rows = len(table_element["table"]["tableRows"])
    print(
        f"Repaired layout: landscape {PAGE_WIDTH_PT}x{PAGE_HEIGHT_PT}pt, "
        f"{MARGIN_PT}pt margins, {len(COLUMNS)} columns totalling "
        f"{sum(COLUMN_WIDTHS_PT.values())}pt of {USABLE_WIDTH_PT}pt usable. "
        f"{rows - 1} data row(s) left untouched."
    )


# --------------------------------------------------------------------------
# the append path (runs every day)
# --------------------------------------------------------------------------

def _row_values(item, today):
    """One listing -> the 11 cell strings, in COLUMNS order."""
    return [
        today,
        item.get("company_name", ""),
        item.get("title", ""),
        item.get("category", ""),
        ", ".join(item.get("locations", []) or []),
        item.get("sponsorship", ""),
        _unix_to_date(item.get("date_posted")),
        str(item.get("score", "")),
        item.get("pitch", ""),
        (item.get("url", "") if SHOW_FULL_URL else (LINK_LABEL if item.get("url") else "")),
        "",  # Status — yours to fill in (Applied / Interview / Rejected...)
    ]


def append_matches(doc_id, scored_listings):
    """
    scored_listings: list of dicts merging the original listing fields with
    "score" and "pitch". Appends one table row per listing, highest score
    first, and never modifies rows already in the doc.

    Returns the set of listing ids actually written. Callers should only mark
    those as "seen" — anything that failed will simply be retried tomorrow
    rather than silently dropped.
    """
    if not scored_listings:
        return set()

    service = _service()
    ensure_table(service, doc_id)

    ordered = sorted(scored_listings, key=lambda x: x.get("score", 0), reverse=True)
    written = set()

    for start in range(0, len(ordered), ROWS_PER_BATCH):
        chunk = ordered[start : start + ROWS_PER_BATCH]
        try:
            _append_chunk(service, doc_id, chunk)
            written.update(item["id"] for item in chunk)
            print(f"  wrote rows {start + 1}-{start + len(chunk)} of {len(ordered)}")
        except Exception as e:  # noqa: BLE001 - keep earlier chunks, retry the rest tomorrow
            print(f"Warning: failed writing rows {start + 1}-{start + len(chunk)}: {e}")

    print(f"Appended {len(written)}/{len(ordered)} row(s) to the Google Doc.")
    return written


def _append_chunk(service, doc_id, ordered):
    """Append one chunk of rows. Re-reads the doc each time so indices are fresh."""
    doc = service.documents().get(documentId=doc_id).execute()
    table_element = _find_table(doc)
    table_start = table_element["startIndex"]
    existing_rows = len(table_element["table"]["tableRows"])

    # Step 1 — structural: add the empty rows.
    row_requests = [
        {
            "insertTableRow": {
                "tableCellLocation": {
                    "tableStartLocation": {"index": table_start},
                    "rowIndex": existing_rows - 1 + i,
                    "columnIndex": 0,
                },
                "insertBelow": True,
            }
        }
        for i in range(len(ordered))
    ]
    service.documents().batchUpdate(
        documentId=doc_id, body={"requests": row_requests}
    ).execute()

    try:
        _fill_new_rows(service, doc_id, ordered, existing_rows)
    except Exception:
        # The rows exist but are empty. Left alone they'd sit in the doc as blank
        # clutter forever AND get re-added tomorrow when these ids retry, so roll
        # them back before letting the caller record this chunk as failed.
        _delete_rows(service, doc_id, existing_rows, len(ordered))
        raise


def _delete_rows(service, doc_id, first_row, count):
    """Remove `count` rows starting at `first_row`. Best-effort cleanup."""
    try:
        doc = service.documents().get(documentId=doc_id).execute()
        table_start = _find_table(doc)["startIndex"]
        requests = [
            {
                "deleteTableRow": {
                    "tableCellLocation": {
                        "tableStartLocation": {"index": table_start},
                        "rowIndex": r,
                        "columnIndex": 0,
                    }
                }
            }
            # highest row first, so each delete can't shift the next one's index
            for r in range(first_row + count - 1, first_row - 1, -1)
        ]
        service.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute()
        print(f"  rolled back {count} empty row(s) after a failed write")
    except Exception as e:  # noqa: BLE001 - cleanup must never mask the original error
        print(f"  Warning: could not roll back empty rows: {e}")


def _fill_new_rows(service, doc_id, ordered, existing_rows):
    """Re-fetch to get the new cells' real indices, then fill them."""
    doc = service.documents().get(documentId=doc_id).execute()
    table_element = _find_table(doc)
    table_start = table_element["startIndex"]
    rows = table_element["table"]["tableRows"]
    new_rows = rows[existing_rows:]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fills = []
    for row, item in zip(new_rows, ordered):
        values = _row_values(item, today)
        for col_idx, cell in enumerate(row["tableCells"]):
            link = item.get("url") if (COLUMNS[col_idx] == "Link" and item.get("url")) else None
            fills.append((_cell_start_index(cell), values[col_idx], link))

    requests = _fill_requests(fills)

    # Step 3 — score-band shading on the Fit Score cell. Coordinate-based, so
    # it's unaffected by the text inserts above and rides along in the same batch.
    score_col = COLUMNS.index("Fit Score")
    for offset, item in enumerate(ordered):
        score = item.get("score", 0) or 0
        bg = None
        if score >= STRONG_CUTOFF:
            bg = SCORE_BG_STRONG
        elif score >= MEDIUM_CUTOFF:
            bg = SCORE_BG_MEDIUM
        if bg:
            requests.extend(
                _cell_style_request(table_start, existing_rows + offset, score_col, bg=bg)
            )

    service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute()


# --------------------------------------------------------------------------
# one-off maintenance
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if sys.argv[1:2] != ["--repair-layout"]:
        print("usage: python tools/docs_writer.py --repair-layout")
        print("       re-applies page setup + column widths to GOOGLE_DOC_ID")
        raise SystemExit(2)

    target = os.environ.get("GOOGLE_DOC_ID")
    if not target:
        raise SystemExit("GOOGLE_DOC_ID is not set.")
    repair_layout(target)
    print(doc_url(target))
