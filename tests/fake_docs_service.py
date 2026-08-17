"""
A stand-in for the Google Docs API that models the document's index structure
closely enough to catch index-drift bugs, wrong request shapes, and ordering
mistakes without needing network access or real credentials.

Index model (mirrors the real one):
  body text lives at [1, 1+len)
  table starts after it; each row costs 1 index unit
  each cell costs 1, its paragraph starts at cell_start+1 and ends after
  the text plus a newline
"""


class FakeDocsService:
    def __init__(self):
        self.prefix_text = ""
        self.table = None          # list of rows; each row = list of cell texts
        self.cell_bg = {}          # (row, col) -> rgb dict
        self.text_styles = []      # recorded updateTextStyle payloads
        self.col_widths = {}
        self.document_style = None
        self.batch_sizes = []
        self.get_calls = 0

    # ---- index computation -------------------------------------------------
    def _table_start(self):
        return 1 + len(self.prefix_text)

    def _layout(self):
        """Return {(row, col): (cell_start, para_start, para_end)}."""
        pos = {}
        idx = self._table_start()
        for r, row in enumerate(self.table):
            idx += 1  # row marker
            for c, text in enumerate(row):
                cell_start = idx
                para_start = cell_start + 1
                para_end = para_start + len(text) + 1
                pos[(r, c)] = (cell_start, para_start, para_end)
                idx = para_end
        return pos

    # ---- API surface -------------------------------------------------------
    def documents(self):
        return self

    def get(self, documentId=None):
        self.get_calls += 1
        return _Exec(self._build_doc())

    def batchUpdate(self, documentId=None, body=None):
        reqs = body["requests"]
        self.batch_sizes.append(len(reqs))
        for req in reqs:
            self._apply(req)
        return _Exec({})

    def _build_doc(self):
        content = []
        if self.prefix_text:
            content.append({
                "startIndex": 1,
                "endIndex": 1 + len(self.prefix_text),
                "paragraph": {},
            })
        if self.table is not None:
            pos = self._layout()
            rows = []
            for r, row in enumerate(self.table):
                cells = []
                for c, text in enumerate(row):
                    cell_start, para_start, para_end = pos[(r, c)]
                    cells.append({
                        "startIndex": cell_start,
                        "content": [{
                            "startIndex": para_start,
                            "endIndex": para_end,
                            "paragraph": {"elements": [{"textRun": {"content": text}}]},
                        }],
                    })
                rows.append({"tableCells": cells})
            content.append({
                "startIndex": self._table_start(),
                "table": {"tableRows": rows},
            })
        return {"body": {"content": content}}

    # ---- request handlers --------------------------------------------------
    def _apply(self, req):
        if "insertText" in req:
            at = req["insertText"]["location"]["index"]
            text = req["insertText"]["text"]
            if self.table is None:
                self.prefix_text = text
                return
            pos = self._layout()
            for (r, c), (_, para_start, _) in pos.items():
                if para_start == at:
                    assert self.table[r][c] == "", (
                        f"cell ({r},{c}) written twice: had {self.table[r][c]!r}"
                    )
                    self.table[r][c] = text
                    return
            if at <= self._table_start():
                self.prefix_text = text + self.prefix_text
                return
            raise AssertionError(
                f"insertText at index {at} matched no cell paragraph — index drift. "
                f"valid starts: {sorted(p[1] for p in pos.values())[:12]}..."
            )

        elif "insertTable" in req:
            cols = req["insertTable"]["columns"]
            rows = req["insertTable"]["rows"]
            assert self.table is None, "table created twice"
            self.table = [["" for _ in range(cols)] for _ in range(rows)]

        elif "insertTableRow" in req:
            loc = req["insertTableRow"]["tableCellLocation"]
            assert loc["tableStartLocation"]["index"] == self._table_start(), (
                f"stale tableStartLocation {loc['tableStartLocation']['index']} "
                f"(actual {self._table_start()})"
            )
            r = loc["rowIndex"]
            assert 0 <= r < len(self.table), f"insertTableRow rowIndex {r} out of range"
            below = req["insertTableRow"].get("insertBelow", False)
            self.table.insert(r + (1 if below else 0), ["" for _ in self.table[0]])

        elif "deleteTableRow" in req:
            loc = req["deleteTableRow"]["tableCellLocation"]
            assert loc["tableStartLocation"]["index"] == self._table_start()
            r = loc["rowIndex"]
            assert 0 < r < len(self.table), f"deleteTableRow rowIndex {r} invalid (never delete header)"
            self.table.pop(r)

        elif "updateTableCellStyle" in req:
            tr = req["updateTableCellStyle"]["tableRange"]["tableCellLocation"]
            assert tr["tableStartLocation"]["index"] == self._table_start(), (
                "stale tableStartLocation in updateTableCellStyle"
            )
            r, c = tr["rowIndex"], tr["columnIndex"]
            assert 0 <= r < len(self.table) and 0 <= c < len(self.table[0]), (
                f"cell style targets out-of-range cell ({r},{c})"
            )
            self.cell_bg[(r, c)] = (
                req["updateTableCellStyle"]["tableCellStyle"]["backgroundColor"]["color"]["rgbColor"]
            )

        elif "updateTextStyle" in req:
            rng = req["updateTextStyle"]["range"]
            span = rng["endIndex"] - rng["startIndex"]
            assert span > 0, f"empty text style range {rng}"
            self.text_styles.append(req["updateTextStyle"])

        elif "updateTableColumnProperties" in req:
            p = req["updateTableColumnProperties"]
            assert p["tableStartLocation"]["index"] == self._table_start()
            for i in p["columnIndices"]:
                self.col_widths[i] = p["tableColumnProperties"]["width"]["magnitude"]

        elif "updateDocumentStyle" in req:
            # Page geometry. Deliberately has no interaction with the index
            # model — it can't move text — so recording it is enough.
            self.document_style = req["updateDocumentStyle"]["documentStyle"]

        elif "updateParagraphStyle" in req:
            pass
        else:
            raise AssertionError(f"unhandled request type: {list(req)[0]}")


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload
