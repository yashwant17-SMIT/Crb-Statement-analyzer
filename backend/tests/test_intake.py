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
        monkeypatch.setattr(m, "parse_credit_report", lambda data: type("R", (), {
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
