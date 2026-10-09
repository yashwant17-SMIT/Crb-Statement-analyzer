"""
Google Sheets export: layout from real payloads, the data tabs, and the write
sequence against a fake Google session (no network, no credentials).

    pytest -q tests/test_sheets.py
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app import sheets

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
CRB = json.loads((FRONTEND / "crb.sample.json").read_text())
# A captured report from a real statement: kept out of the repository, so
# these tests run only where a local copy exists.
_REPORT_FILE = FRONTEND / "report.sample.json"
if not _REPORT_FILE.exists():
    pytest.skip("frontend/report.sample.json is not present", allow_module_level=True)
REPORT = json.loads(_REPORT_FILE.read_text())
AT = datetime(2026, 10, 7, 14, 30)


def _titles(lay):
    return [r[0] for r, k in zip(lay.rows, lay.kind) if k == "title"]


# ---------------------------------------------------------- customer report ---
def test_report_carries_every_console_section():
    titles = _titles(sheets.build_layout(CRB, REPORT, {}, "A1", AT))
    for expected in ("Summary", "Credit bureau decision", "What the applicant already owes",
                     "Bureau findings", "Applicant profile", "Statement verdict",
                     "Net revenue summary (per month, RWF)", "Revenue calculation (RWF)",
                     "Loan eligibility: how much can be disbursed?", "Mandatory disclosures",
                     "Statement health", "Risk findings"):
        assert expected in titles, expected


def test_report_values_are_typed_not_text():
    lay = sheets.build_layout(CRB, REPORT, {}, "A1", AT)
    total = next(r for r, k in zip(lay.rows, lay.kind) if k == "total" and r[0] == "Total")
    assert total[1] == REPORT["summary"]["totals"]["gross_inflows"]
    pd = next(r for r in lay.rows if r and r[0] == "Probability of default")
    assert isinstance(pd[1], sheets.Pct) and pd[1] == pytest.approx(0.0114)
    for row in lay.rows:
        for v in row:
            assert isinstance(v, (str, int, float)), (row, v)


def test_enum_values_read_as_words():
    lay = sheets.build_layout(None, REPORT, {}, "A1", AT)
    by = {r[0]: r[1] for r, k in zip(lay.rows, lay.kind) if k == "pair"}
    assert by["Eligibility"] == "Not eligible"
    assert by["Sanction basis"] == "Not eligible"


def test_bureau_only_and_statement_only_both_lay_out():
    assert "Statement verdict" not in _titles(sheets.build_layout(CRB, None, {}, "A1", AT))
    assert "Credit bureau decision" not in _titles(sheets.build_layout(None, REPORT, {}, "A1", AT))


def test_nothing_to_save_is_refused():
    with pytest.raises(sheets.SheetsError):
        sheets.build_layout(None, None)


def test_report_requests_merge_titles_and_size_columns():
    lay = sheets.build_layout(CRB, REPORT, {}, "A1", AT)
    ops = sheets._report_requests(5, lay)
    assert any("mergeCells" in o for o in ops)
    widths = [o["updateDimensionProperties"]["properties"]["pixelSize"] for o in ops
              if "updateDimensionProperties" in o
              and o["updateDimensionProperties"]["range"]["dimension"] == "COLUMNS"]
    assert all(90 <= w <= 360 for w in widths)


# --------------------------------------------------------------- data tabs ---
def test_register_row_matches_its_columns():
    row = sheets.register_row("A1", AT, "T", 9, CRB, REPORT)
    assert len(row) == len(sheets.REGISTER_COLS)
    by = dict(zip((c.header for c in sheets.REGISTER_COLS), row))
    assert by["National ID"] == "0000000000000000"
    assert by["Net revenue (RWF)"] == REPORT["summary"]["totals"]["net_revenue"]
    assert by["Eligibility"] == "Not eligible"
    assert by["Current"] == "yes" and by["Underwriter decision"] == "Pending"
    assert by["Comments"] is None and by["Approved amount (RWF)"] is None
    assert by["Customer"] == '=HYPERLINK("#gid=9","T")'


def test_detail_rows_are_long_format_and_keyed():
    out = sheets.detail_rows("A1", "T", CRB, REPORT)
    assert len(out["Monthly revenue"]) == len(REPORT["summary"]["rows"])
    assert len(out["Bureau accounts"]) == len(CRB["exposure"]["accounts"])
    assert len(out["Findings"]) == (len(CRB["findings"]) + len(REPORT["red_flags"])
                                    + len(REPORT["edge_cases"]))
    for name, rows in out.items():
        width = len(sheets.DETAIL_TABS[name])
        assert all(len(r) == width and r[0] == "A1" and r[1] is None for r in rows), name


def test_current_formula_points_at_the_register_current_column():
    ops = sheets._data_tab_setup("Findings", sheets.DETAIL_TABS["Findings"])
    header = next(o["updateCells"] for o in ops if "updateCells" in o)
    formula = header["rows"][0]["values"][1]["userEnteredValue"]["formulaValue"]
    assert f"'Register'!A:{sheets._col_letter(sheets.CURRENT_COL)}" in formula
    assert f",{sheets.CURRENT_COL + 1},FALSE" in formula and "FILTER(A2:A" in formula


def test_appended_rows_carry_formats_and_the_decision_dropdown():
    op = sheets._append(1, sheets.REGISTER_COLS, [sheets.register_row("A1", AT, "T", 9, CRB, REPORT)])
    cells = op["appendCells"]["rows"][0]["values"]
    idx = {c.header: i for i, c in enumerate(sheets.REGISTER_COLS)}
    assert cells[idx["Assessed at"]]["userEnteredFormat"]["numberFormat"]["type"] == "DATE_TIME"
    assert "dataValidation" in cells[idx["Underwriter decision"]]
    assert "userEnteredValue" not in cells[idx["Comments"]]


# -------------------------------------------------------------- tab naming ---
def test_tab_keys_on_national_id_when_there_is_a_bureau_report():
    assert sheets.tab_title(CRB, REPORT) == "MS SAMPLE APPLICANT · 0000000000000000"


def test_statement_only_tab_keys_on_msisdn():
    assert sheets.tab_title(None, REPORT) == "Benie NIYOMUFASHA · 250788437794"


def test_tab_title_is_legal_and_bounded():
    crb = {"applicant": {"names": 'A/B:C*D?[E]\'F"G ' * 20, "national_id": "1199"}}
    title = sheets.tab_title(crb, None)
    assert len(title) <= 100 and title.endswith("· 1199")
    assert not any(ch in title for ch in "[]:*?/\\'\"")


# ------------------------------------------------------------------ writing ---
class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeSession:
    """Just enough of the Sheets API to follow what save() asks for."""

    def __init__(self, tabs, register=()):
        self.tabs = dict(tabs)
        self.register = list(register)   # (customer, current) pairs
        self.batches = []

    def request(self, method, url, **kw):
        if url.endswith("values:batchGet"):
            return FakeResponse(200, {"valueRanges": [
                {"values": [[c] for c, _ in self.register]},
                {"values": [[x] for _, x in self.register]}]})
        if method == "GET":
            return FakeResponse(200, {"sheets": [{"properties": {"title": t, "sheetId": g}}
                                                 for t, g in self.tabs.items()]})
        ops = kw["json"]["requests"]
        self.batches.append(ops)
        replies = []
        for op in ops:
            if "addSheet" in op:
                props = op["addSheet"]["properties"]
                gid = props.get("sheetId", 500 + len(self.tabs))
                self.tabs[props["title"]] = gid
                replies.append({"addSheet": {"properties": {"sheetId": gid}}})
            else:
                replies.append({})
        return FakeResponse(200, {"replies": replies})


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(sheets, "SHEET_ID", "sheet123")
    monkeypatch.setattr(sheets, "KEY_FILE", "/k.json")

    def install(tabs, register=()):
        fake = FakeSession(tabs, register)
        monkeypatch.setattr(sheets, "_get_session", lambda: fake)
        return fake
    return install


def test_first_save_builds_the_workbook(configured):
    fake = configured({"Sheet1": 0})
    out = sheets.save(CRB, REPORT)
    assert {"Register", "Monthly revenue", "Bureau accounts", "Findings"} <= set(fake.tabs)
    assert out["created"] is True and out["superseded"] == 0
    assert out["register_url"].endswith(f"#gid={sheets.TAB_IDS['Register']}")

    final = fake.batches[-1]
    appended = {op["appendCells"]["sheetId"] for op in final if "appendCells" in op}
    assert appended == set(sheets.TAB_IDS.values())


def test_reassessment_appends_and_supersedes_the_old_row(configured):
    title = "MS SAMPLE APPLICANT · 0000000000000000"
    tabs = {"Sheet1": 0, **{n: g for n, g in sheets.TAB_IDS.items()}, title.lower(): 77}
    fake = configured(tabs, register=[("Someone else · 1", "yes"), (title, "yes"), (title, "no")])
    out = sheets.save(CRB, None)

    assert out["created"] is False and out["superseded"] == 1 and out["url"].endswith("#gid=77")
    assert len(fake.batches) == 1, "existing workbook: one atomic write"
    final = fake.batches[0]
    marks = [op["updateCells"]["range"]["startRowIndex"] for op in final
             if "updateCells" in op and op["updateCells"]["range"]["sheetId"] == sheets.TAB_IDS["Register"]]
    assert marks == [2]   # row 3 of the sheet: header + "Someone else" + this customer
    # Bureau-only: no monthly revenue rows to append.
    appended = {op["appendCells"]["sheetId"] for op in final if "appendCells" in op}
    assert sheets.TAB_IDS["Monthly revenue"] not in appended


def test_google_refusal_is_reported_with_a_fix(configured, monkeypatch):
    class Denied(FakeSession):
        def request(self, method, url, **kw):
            return FakeResponse(403, {"error": {"message": "The caller does not have permission"}})
    monkeypatch.setattr(sheets, "_get_session", lambda: Denied({}))
    with pytest.raises(sheets.SheetsError, match="shared with the service account"):
        sheets.save(CRB, REPORT)


def test_unconfigured_server_refuses(monkeypatch):
    monkeypatch.setattr(sheets, "SHEET_ID", "")
    with pytest.raises(sheets.SheetsError, match="not configured"):
        sheets.save(CRB, REPORT)
