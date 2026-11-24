"""
HTTP-level tests.

The engine tests exercise pure functions; these exercise the wire. They are the
tests that would have caught a bad default in config.py, an unenforced bureau
gate, and an upload guard that only fires after the file is already in memory.

    pip install pytest httpx
    pytest -q
"""
from __future__ import annotations

import io

import pytest

fastapi_testclient = pytest.importorskip(
    "fastapi.testclient", reason="install fastapi and httpx to run the HTTP tests"
)
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

PDF = b"%PDF-1.4\n% not a real statement\n"
NOT_PDF = b"PK\x03\x04 this is a zip"


def _upload(path: str, data: bytes, filename="s.pdf", content_type="application/pdf", **form):
    return client.post(path, files={"file": (filename, io.BytesIO(data), content_type)}, data=form)


# ------------------------------------------------------------------ basics ---
def test_health_is_open():
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_providers_lists_something_the_console_can_render():
    r = client.get("/api/v1/providers")
    assert r.status_code == 200
    providers = r.json()["providers"]
    assert providers
    for p in providers:
        assert p["value"] and p["label"] and p["formats"]


def test_policy_endpoint_exposes_the_thresholds_it_claims_to():
    body = client.get("/api/v1/policy").json()
    # The console labels this block "thresholds actually applied", so anything
    # the engine reads must appear here.
    for key in ("min_band", "large_txn_threshold", "credit_cap", "loan_cycle_days",
                "profile_sigma", "round_step", "full_month_min_days",
                "concentration_single_txn_share"):
        assert key in body, f"{key} missing from the policy provenance block"


# ------------------------------------------------------------ upload guards --
def test_wrong_content_type_is_415():
    assert _upload("/api/v1/analyze", PDF, content_type="text/csv").status_code == 415


def test_non_pdf_bytes_are_415_whatever_the_browser_claimed():
    r = _upload("/api/v1/analyze", NOT_PDF, content_type="application/octet-stream")
    assert r.status_code == 415
    assert r.json()["detail"]["error"] == "not_a_pdf"


def test_a_byte_order_mark_before_the_pdf_header_is_tolerated():
    """A PDF that passed through a text tool arrives as EF BB BF %PDF-. It is
    otherwise intact and must reach the parser, not bounce as 'not a PDF'."""
    r = _upload("/api/v1/analyze", b"\xef\xbb\xbf" + PDF, provider="mtn_momo")
    assert r.status_code == 422
    assert r.json()["error"] == "statement_unreadable"


def test_empty_file_is_400():
    assert _upload("/api/v1/analyze", b"").status_code == 400


def test_missing_file_is_422():
    assert client.post("/api/v1/analyze").status_code == 422


def test_unknown_provider_is_rejected():
    """StatementParseError is handled as 422 with a top-level error payload."""
    r = _upload("/api/v1/analyze", PDF, provider="barclays")
    assert r.status_code == 422
    assert "barclays" in r.json()["message"]


def test_unreadable_pdf_is_reported_not_500():
    r = _upload("/api/v1/analyze", PDF, provider="mtn_momo")
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "statement_unreadable"
    # The message must name what IS supported, not just say no.
    assert "MTN MoMo" in body["message"] or "Bank of Kigali" in body["message"]


def _locked_open(*a, password="", **k):
    """pdfplumber.open on a password-protected PDF: pdfminer's error, wrapped."""
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    from pdfplumber.utils.exceptions import PdfminerException
    try:
        raise PDFPasswordIncorrect()
    except PDFPasswordIncorrect as e:
        raise PdfminerException(e)


@pytest.mark.parametrize("form,code", [
    ({}, "credit_report_password_required"),
    ({"crb_password": "wrong"}, "credit_report_password_incorrect"),
])
def test_a_locked_bureau_report_asks_for_its_password(monkeypatch, form, code):
    import app.crb_parser as p
    monkeypatch.setattr(p.pdfplumber, "open", _locked_open)
    r = _upload("/api/v1/crb/check", PDF, **form)
    assert r.status_code == 422
    assert r.json()["error"] == code


# A made-up 16-digit national ID and its first 13 digits.
NID, NID13 = "1199000012345678", "1199000012345"


def _tried(monkeypatch, opens_with=None):
    """Replace pdfplumber.open with a locked PDF; returns the passwords tried."""
    import app.crb_parser as p
    tried = []

    def opened(f, password=""):
        tried.append(password)
        if password != opens_with:
            _locked_open()
        raise ValueError("opened")  # far enough: the parse itself is not under test

    monkeypatch.setattr(p.pdfplumber, "open", opened)
    return tried


@pytest.mark.parametrize("filename,form,tried", [
    # The password as typed, then the national ID in it cut to 13 digits.
    ("r.pdf", {"crb_password": NID}, ["", NID, NID13]),
    # A file named after the applicant's ID needs nothing typed.
    (f"{NID}.pdf", {}, ["", NID13]),
    (f"{NID} (2).pdf", {"crb_password": "x"}, ["", "x", NID13]),
    # Not a 16-digit ID: nothing is guessed.
    (f"{NID[:15]}.pdf", {}, [""]),
])
def test_a_locked_bureau_report_tries_the_national_id(monkeypatch, filename, form, tried):
    seen = _tried(monkeypatch)
    _upload("/api/v1/crb/check", PDF, filename=filename, **form)
    assert seen == tried


def test_the_national_id_rule_can_be_turned_off(monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "CRB_PASSWORD_NID_LENGTH", 0)
    seen = _tried(monkeypatch)
    r = _upload("/api/v1/crb/check", PDF, filename=f"{NID}.pdf")
    assert seen == [""] and r.json()["error"] == "credit_report_password_required"


def test_the_first_password_that_opens_the_pdf_is_used(monkeypatch):
    seen = _tried(monkeypatch, opens_with=NID13)
    r = client.post("/api/v1/applications/assess", data={"crb_password": NID},
                    files={"crb_file": ("c.pdf", io.BytesIO(PDF), "application/pdf")})
    assert seen == ["", NID, NID13]
    assert r.json()["error"] == "credit_report_unreadable", "opened, so not a password error"


# ------------------------------------------------------------- the gate ------
def test_assess_requires_the_bureau_report_first():
    """The enforced flow will not look at a statement without a bureau report."""
    r = client.post(
        "/api/v1/applications/assess",
        files={"statement_file": ("s.pdf", io.BytesIO(PDF), "application/pdf")},
    )
    assert r.status_code == 422


# ------------------------------------------------------------ identity gate --
def test_analyze_reconciles_the_statement_holder_against_the_bureau_applicant():
    """The console underwrites a statement against a bureau file parsed earlier.

    Nothing previously checked that the two were the same person, so a statement
    belonging to someone else would be assessed against another applicant's
    exposure and EMI without a word.
    """
    from app.main import _compare_identity

    same = _compare_identity(
        bureau_name="NIYOMUFASHA BENIE", bureau_phones=["0788437794"],
        statement_name="Benie NIYOMUFASHA", statement_msisdn="250788437794")
    assert same["status"] == "match"
    assert same["name_match"] is True and same["msisdn_on_bureau_profile"] is True

    # Right person, number the bureau has not caught up with.
    lagging = _compare_identity(
        bureau_name="NIYOMUFASHA BENIE", bureau_phones=["0788000000"],
        statement_name="Benie NIYOMUFASHA", statement_msisdn="250788437794")
    assert lagging["status"] == "match_name_only"

    # Different person entirely.
    other = _compare_identity(
        bureau_name="NIYOMUFASHA BENIE", bureau_phones=["0788437794"],
        statement_name="Philemon NIYONSENGA", statement_msisdn="250781944442")
    assert other["status"] == "mismatch"
    assert other["name_match"] is False
    assert "not appear to be the same person" in other["note"]

    # One document carries no name: reported as unassessable, never as a match.
    blank = _compare_identity(
        bureau_name="NIYOMUFASHA BENIE", bureau_phones=[],
        statement_name="", statement_msisdn="250788437794")
    assert blank["status"] == "not_assessed"


def test_identity_mismatch_becomes_a_blocking_edge_case_and_a_severe_flag():
    from app.main import _apply_identity_to_report, _compare_identity

    report = {"edge_cases": [], "red_flags": []}
    _apply_identity_to_report(report, _compare_identity(
        bureau_name="NIYOMUFASHA BENIE", bureau_phones=[],
        statement_name="Philemon NIYONSENGA", statement_msisdn="250781944442"))
    assert report["edge_cases"][0]["code"] == "IDENTITY_MISMATCH"
    assert report["edge_cases"][0]["severity"] == "high"
    assert report["red_flags"][0]["severity"] == "severe"

    # A clean match adds nothing.
    clean = {"edge_cases": [], "red_flags": []}
    _apply_identity_to_report(clean, _compare_identity(
        bureau_name="NIYOMUFASHA BENIE", bureau_phones=["0788437794"],
        statement_name="Benie NIYOMUFASHA", statement_msisdn="250788437794"))
    assert clean["edge_cases"] == [] and clean["red_flags"] == []


def test_assess_does_not_open_the_statement_when_the_bureau_blocks(monkeypatch):
    """The gate holds for the bytes path too: a blocked applicant's statement is
    never read, so a broken statement file cannot turn a block into an error."""
    import app.main as m

    monkeypatch.setattr(m, "parse_credit_report",
                        lambda data, passwords=(): type("R", (), {"pages": 2, "accounts": []})())
    monkeypatch.setattr(m, "assess_credit_report", lambda report, policy: {
        "gate": {"statement_parsing_allowed": False, "reason": "band", "band": "GG",
                 "minimum_band": "DD", "decision": "decline"}})
    monkeypatch.setattr(m, "parse_statement", lambda *a, **k: pytest.fail("statement was parsed"))
    r = client.post(
        "/api/v1/applications/assess",
        files=[("crb_file", ("c.pdf", io.BytesIO(PDF), "application/pdf")),
               ("statement_file", ("s.pdf", io.BytesIO(NOT_PDF), "application/pdf"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "decline" and body["statement"] is None
