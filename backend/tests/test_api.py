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
