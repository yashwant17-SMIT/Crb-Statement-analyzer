"""
The Apps Script intake route: token, cell parsing, the gate, and the shape the
script writes back. The parsers are replaced; these tests are about the wire.

    pytest -q tests/test_intake.py
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi.testclient", reason="install fastapi and httpx to run the HTTP tests")
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import intake, sheets  # noqa: E402

CLIENT = TestClient(m.app)
PDF = b"%PDF-1.4\n% test\n"
TOKEN = "s3cret-intake-token"
CRB = json.loads((Path(__file__).resolve().parents[2] / "frontend" / "crb.sample.json").read_text())


@pytest.fixture
def engine(monkeypatch):
    calls = {"statements": None}
    monkeypatch.setattr(intake, "TOKEN", TOKEN)
    monkeypatch.setattr(sheets, "SHEET_ID", "")

    def gate(open_):
        crb = json.loads(json.dumps(CRB))
        crb["gate"]["statement_parsing_allowed"] = open_
        monkeypatch.setattr(m, "parse_credit_report", lambda data, passwords=(): type("R", (), {
            "pages": 1, "accounts": [],
            "profile": type("P", (), {"names": crb["applicant"]["names"], "phones": []})})())
        monkeypatch.setattr(m, "assess_credit_report", lambda report, policy: crb)

    def underwrite(files, providers, policy, **kw):
        calls["statements"] = [name for name, _ in files]
        calls["providers"] = providers
        calls["kw"] = kw
        return {"summary": {"loan_eligibility": {"status": "eligible", "sanctioned_amount": 800000}},
                "red_flags": [{"severity": "severe"}], "meta": {"filename": ", ".join(calls["statements"])},
                "account": {"customer": "X"}, "_statements": []}

    monkeypatch.setattr(m, "_underwrite", underwrite)
    monkeypatch.setattr(m, "_identity_for", lambda report, *a: report.update(identity_check={"status": "match"}))
    monkeypatch.setattr(sheets, "build_layout", lambda *a, **k: _layout())
    calls["gate"] = gate
    return calls


def _layout():
    lay = sheets.Layout()
    lay.banner("Underwriting report — X", "meta")
    lay.pairs([("Probability of default", sheets.Pct(0.0114)), ("Assessed", sheets.Stamp(46000.5)),
               ("Nothing", None)])
    return lay


EMPTY_WORKBOOK = json.dumps({"tabs": {"Sheet1": 0}, "register": []})


def post(token=TOKEN, statements=("jan.pdf", "feb.pdf"), **fields):
    data = {"customer_type": "Existing", "requested_amount": "1,000,000",
            "results_state": EMPTY_WORKBOOK, **fields}
    files = [("crb_file", ("crb.pdf", io.BytesIO(PDF), "application/pdf"))]
    # The numbered form Apps Script sends, deliberately out of order.
    for i, name in reversed(list(enumerate(statements, 1))):
        files.append((f"statement_file_{i}", (name, io.BytesIO(PDF), "application/pdf")))
    headers = {"X-Intake-Token": token} if token else {}
    return CLIENT.post("/api/v1/intake/assess", data=data, files=files, headers=headers)


def test_route_is_closed_without_a_configured_token(monkeypatch):
    monkeypatch.setattr(intake, "TOKEN", "")
    assert post().status_code == 503


@pytest.mark.parametrize("token", [None, "", "wrong", TOKEN + "x"])
def test_a_missing_or_wrong_token_is_refused(engine, token):
    assert post(token=token).status_code == 401


def test_the_token_route_skips_the_browser_password_but_others_do_not(engine, monkeypatch):
    monkeypatch.setattr(m, "ACCESS_PASSWORD", "pw")
    engine["gate"](False)
    assert post().status_code == 200
    assert CLIENT.get("/api/v1/policy").status_code == 401


def test_a_cleared_row_is_assessed_in_sheet_order(engine):
    engine["gate"](True)
    r = post(provider="MTN MoMo")
    assert r.status_code == 200, r.text
    body = r.json()
    assert engine["statements"] == ["jan.pdf", "feb.pdf"]
    assert engine["providers"] == ["mtn_momo"]
    assert engine["kw"]["customer_type"] == "existing" and engine["kw"]["requested_amount"] == 1_000_000
    assert body["status"] == "Done"
    assert "Sanctioned 800,000 RWF" in body["result"] and "1 severe finding" in body["result"]


def test_a_blocked_row_never_parses_its_statements(engine):
    engine["gate"](False)
    body = post().json()
    assert engine["statements"] is None
    assert body["status"] == "Blocked at CRB"


@pytest.mark.parametrize("sno,ages,note", [
    (None, (15, 15), False),
    ("229", (15, 15), False),
    (" 230 ", (100_000, 100_000), True),
])
def test_a_listed_row_is_assessed_without_the_freshness_limits(engine, monkeypatch, sno, ages, note):
    engine["gate"](True)
    monkeypatch.setattr(m, "SKIP_FRESHNESS_SNOS", ["230"])
    seen = {}
    monkeypatch.setattr(m, "assess_credit_report", lambda report, policy: (
        seen.update(ages=(policy.crb_max_age_days, policy.statement_max_age_days)) or json.loads(json.dumps(CRB))))
    body = post(**({"sno": sno} if sno else {})).json()
    assert seen["ages"] == ages
    assert body["result"].endswith("(freshness check waived)") is note


def test_the_row_bureau_password_and_national_id_reach_the_parser(engine, monkeypatch):
    engine["gate"](False)
    seen = {}

    def parse(data, passwords=()):
        seen["passwords"] = passwords
        return type("R", (), {"pages": 1, "accounts": []})()

    monkeypatch.setattr(m, "parse_credit_report", parse)
    assert post(crb_password="pw").status_code == 200
    assert seen["passwords"] == ["pw"]
    # The row's ID cell as the sheet shows it; a made-up ID.
    assert post(national_id="NID 1199000012345678").status_code == 200
    assert seen["passwords"] == ["1199000012345"]


@pytest.mark.parametrize("field,value,message", [
    ("customer_type", "", "Customer type is empty"),
    ("requested_amount", "about 1M", "Requested amount"),
    ("provider", "Equity", "Provider 'Equity'"),
])
def test_a_bad_cell_is_named(engine, field, value, message):
    r = post(**{field: value})
    assert r.status_code == 422
    assert message in r.json()["detail"]["message"]


def test_the_response_is_the_results_workbook_write(engine):
    engine["gate"](True)
    body = post().json()
    res = body["results"]
    added = {op["addSheet"]["properties"]["title"] for op in res["ops"] if "addSheet" in op}
    assert {"Register", "Monthly revenue", "Bureau accounts", "Findings", res["tab"]} == added
    assert res["created"] is True and res["superseded"] == 0
    assert any("appendCells" in op and op["appendCells"]["sheetId"] == sheets.TAB_IDS["Register"]
               for op in res["ops"])


def test_a_reassessed_customer_supersedes_their_register_row(engine):
    engine["gate"](False)
    title = sheets.tab_title(CRB, None)
    state = json.dumps({"tabs": {**sheets.TAB_IDS, title: 4242},
                        "register": [[title, "yes"], ["Someone else", "yes"]]})
    res = post(results_state=state).json()["results"]
    assert res["gid"] == 4242 and res["created"] is False and res["superseded"] == 1


@pytest.mark.parametrize("state", [None, "", "not json", "{}", '{"tabs": {"A": "x"}}'])
def test_a_missing_or_broken_workbook_state_is_refused(engine, state):
    engine["gate"](True)
    fields = {} if state is None else {"results_state": state}
    data = {"customer_type": "New", "requested_amount": "500000", **fields}
    r = CLIENT.post("/api/v1/intake/assess", data=data, headers={"X-Intake-Token": TOKEN},
                    files=[("crb_file", ("crb.pdf", io.BytesIO(PDF), "application/pdf"))])
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_results_state"


def _out(*, open_=True, el=None, crb_findings=(), flags=(), identity="match", parse_complete=True, awaiting=False):
    out = {"credit_bureau": {"gate": {"band": "BB", "statement_parsing_allowed": open_,
                                      "reason": "Band FF is below the minimum DD."},
                             "findings": [{"severity": s, "title": t} for s, t in crb_findings]}}
    if awaiting:
        out["decision"] = "awaiting_statement"
    elif open_:
        out["statement"] = {
            "summary": {"loan_eligibility": el or {"status": "eligible", "sanctioned_amount": 800000}},
            "red_flags": [{"severity": s, "title": t} for s, t in flags],
            "identity_check": {"status": identity}, "meta": {"parse_complete": parse_complete}}
    return out


@pytest.mark.parametrize("out,verdict,reason,sanctioned", [
    (_out(), "approved", "", 800000),
    (_out(open_=False), "rejected", "Band FF is below the minimum DD.", None),
    (_out(el={"status": "not_eligible", "reason": "Fee cap 20,000 RWF is below the lowest band."}),
     "rejected", "Fee cap 20,000 RWF is below the lowest band.", None),
    (_out(crb_findings=[("blocking", "Written-off account"), ("info", "Old enquiry")]),
     "on_hold", "CRB: Written-off account.", 800000),
    (_out(flags=[("severe", "Round-tripping"), ("elevated", "Gaps")], identity="mismatch"),
     "on_hold", "Statement: Round-tripping. The statement does not belong to the CRB applicant.", 800000),
    (_out(el={"status": "not_assessed", "reason": "Only 2 of 3 months."}),
     "on_hold", "No loan amount: Only 2 of 3 months.", None),
    (_out(parse_complete=False), "on_hold", "The statement could not be read in full.", 800000),
    (_out(awaiting=True), "on_hold", "No statement to assess.", None),
])
def test_the_row_decision_for_the_ops_sheet(out, verdict, reason, sanctioned):
    assert intake.decision(out) == {"band": "BB", "decision": verdict, "sanctioned_amount": sanctioned,
                                    "reason": reason}


def test_statement_problems_are_said_once_each_with_their_accounts():
    float_ = {"code": "FLOAT_ACCOUNT", "severity": "severe"}
    out = _out(el={"status": "not_assessed", "reason": "No sanction can be derived: ... (long)"},
               crb_findings=[("blocking", "Parsed accounts do not match the bureau summary")])
    out["statement"]["red_flags"] = [
        {**float_, "title": "Not an income document — Bank of Kigali · 100084650247",
         "account": "Bank of Kigali · 100084650247"},
        {"code": "STATEMENT_OUT_OF_SPEC", "severity": "severe", "account": "Bank of Kigali · 100084650597",
         "title": "Statement does not meet document policy — Bank of Kigali · 100084650597",
         "evidence": {"generated_age_days": 16, "generated_max_age_days": 15}},
        {**float_, "title": "Not an income document — Bank of Kigali · 100084650597",
         "account": "Bank of Kigali · 100084650597"},
        {"code": "PASS_THROUGH", "severity": "elevated", "title": "Circular / round-tripping"}]
    assert intake.decision(out)["reason"] == (
        "No loan amount: agent float account, not an income document (100084650247, 100084650597); "
        "statement 16 days old, limit 15 (100084650597). "
        "CRB: Parsed accounts do not match the bureau summary.")


def test_a_deviation_refer_puts_the_points_in_the_reason():
    detail = ("D2 Open performing loans: REFER – 1 of 1 open loan(s) overdue beyond the Allowed limits. "
              "Account #3 Personal loan, listed 1 Mar 2025: 40 days overdue, arrears RWF 60,000; "
              "16-90 days overdue, within the Refer limit of RWF 75,000.")
    out = _out()
    out["credit_bureau"]["findings"] = [
        {"code": "DEVIATION_REFER", "severity": "blocking", "detail": detail,
         "title": "Band BB deviation: refer to the approving authority"},
        {"code": "STALE_REPORT", "severity": "blocking", "title": "Report older than 30 days"}]
    row = intake.decision(out)
    assert row["decision"] == "on_hold" and row["sanctioned_amount"] == 800000
    assert row["reason"] == ("CRB deviation, refer to the approving authority: " + detail
                             + " CRB: Report older than 30 days.")


def test_the_route_returns_the_row_decision(engine):
    engine["gate"](False)
    row = post().json()["row"]
    assert row["decision"] == "rejected" and row["band"] == CRB["gate"]["band"]
