"""
MoMo Statement Underwriting API.

    POST /api/v1/analyze        multipart PDF(s) -> full underwriting report (JSON);
                                several files are combined into one outcome
    POST /api/v1/transactions   multipart PDF  -> normalised transaction rows only
    GET  /api/v1/policy                        -> current default credit policy
    POST /api/v1/sheets/save    JSON           -> write an assessment to Google Sheets
    GET  /api/v1/health

Stateless: nothing is written to disk, nothing is retained after the response.
The optional Google Sheets export (app/sheets.py) is the one place results
leave the process, and only when it is configured.
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import sheets
from .analyzer import analyze
from .combine import account_label, combine_reports, group_by_account
from .config import DEFAULT_POLICY, Policy
from .crb_analyzer import assess_credit_report
from .crb_parser import BAND_LADDER, CreditReportParseError, parse_credit_report
from .statements import StatementParseError, parse_statement, provider_options

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("momo-underwriter")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# Optional password gate. Off unless ACCESS_PASSWORD is set.
#
# This exists because the moment this service is reachable from outside the
# machine - a shared office server, an ngrok tunnel, anything - it is serving
# national IDs, dates of birth, phone numbers and full transaction histories to
# whoever has the URL. Tunnel-level auth depends on the tunnel provider and
# plan; this does not.
ACCESS_PASSWORD = os.getenv("ACCESS_PASSWORD", "")
ACCESS_USERNAME = os.getenv("ACCESS_USERNAME", "underwriter")
OPEN_PATHS = {"/api/v1/health"}

app = FastAPI(
    title="MoMo Statement Underwriting API",
    version="1.0.0",
    description=(
        "Turns an MTN Rwanda MoMo statement PDF into a defensible net-revenue "
        "figure with full exclusion detail, mandatory disclosures and red flags."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_password(request: Request, call_next):
    """HTTP Basic over every route except health, when a password is configured.

    Browsers prompt natively and then re-send the header on same-origin fetch
    calls, so the React console needs no login screen of its own.
    """
    if not ACCESS_PASSWORD or request.url.path in OPEN_PATHS:
        return await call_next(request)

    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:  # noqa: BLE001 - malformed header is simply a failure
            user = pwd = ""
        if (secrets.compare_digest(user, ACCESS_USERNAME)
                and secrets.compare_digest(pwd, ACCESS_PASSWORD)):
            return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"error": "unauthorised", "message": "A username and password are required."},
        headers={"WWW-Authenticate": 'Basic realm="Ledger underwriting", charset="UTF-8"'},
    )


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    response.headers["X-Elapsed-Ms"] = f"{(time.perf_counter() - started) * 1000:.0f}"
    log.info("%s %s %s %sms", rid, request.method, request.url.path,
             response.headers["X-Elapsed-Ms"])
    return response


@app.exception_handler(CreditReportParseError)
async def crb_parse_error_handler(_: Request, exc: CreditReportParseError):
    return JSONResponse(
        status_code=422,
        content={
            "error": "credit_report_unreadable",
            "message": str(exc),
            "hint": "Upload the original TransUnion PDF. Note the bureau report and "
                    "the MoMo statement are different documents and go in different slots.",
        },
    )


@app.exception_handler(StatementParseError)
async def parse_error_handler(_: Request, exc: StatementParseError):
    message = str(exc)
    if message.startswith("File could not be opened as a PDF:"):
        message = (
            f"{message} Supported statement formats: "
            + "; ".join(f["label"] for p in provider_options() for f in p["formats"])
            + "."
        )
    return JSONResponse(
        status_code=422,
        content={
            "error": "statement_unreadable",
            "message": message,
            "hint": "Upload the original PDF downloaded from the provider, not a scan, "
                    "screenshot or printout, and check the selected provider matches "
                    "the document.",
        },
    )


async def _read_pdf(file: UploadFile) -> bytes:
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(
            status_code=415,
            detail={"error": "unsupported_type",
                    "message": f"Expected a PDF, received {file.content_type}."},
        )

    # Read in chunks and abort at the ceiling. Reading first and measuring
    # afterwards means a 2 GB POST is fully buffered into memory before being
    # rejected, which is a free denial of service.
    limit = MAX_UPLOAD_MB * 1024 * 1024
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise HTTPException(
                status_code=413,
                detail={"error": "file_too_large",
                        "message": f"Maximum upload size is {MAX_UPLOAD_MB} MB."},
            )
        chunks.append(chunk)
    data = b"".join(chunks)

    if not data:
        raise HTTPException(status_code=400,
                            detail={"error": "empty_file", "message": "The uploaded file is empty."})

    # Content-type is whatever the browser felt like sending; octet-stream is
    # accepted above for that reason. The magic bytes are not negotiable - but,
    # as the PDF spec and every reader allow, they may sit anywhere in the
    # first 1 KB. Files that passed through a text tool arrive with a UTF-8
    # byte-order mark in front of '%PDF-' and are otherwise intact.
    at = data.find(b"%PDF-", 0, 1024)
    if at > 0:
        data = data[at:]
    if at < 0:
        raise HTTPException(
            status_code=415,
            detail={"error": "not_a_pdf",
                    "message": "The uploaded file is not a PDF. Upload the original "
                               "statement PDF, not a screenshot, scan or spreadsheet."},
        )
    return data


def _statement_meta(filename: str, statement, started: float) -> dict:
    return {
        "filename": filename,
        "provider": statement.header.provider,
        "source_format": statement.source_format,
        "pages": statement.pages,
        "rows_parsed": len(statement.transactions),
        "rows_declared": statement.header.declared_txn_count,
        "rows_rejected": len(statement.rejected_rows),
        "parse_complete": statement.parse_complete,
        "balance_reconciles": statement.balance_reconciles,
        "reconciliation": statement.reconciliation_note(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


def _policy_from_form(
    large_txn_threshold: Optional[int] = None,
    min_inflow_count: Optional[int] = None,
    credit_cap: Optional[int] = None,
    min_band: Optional[str] = None,
) -> Policy:
    p = Policy()
    if large_txn_threshold is not None:
        p.large_txn_threshold = large_txn_threshold
    if min_inflow_count is not None:
        p.min_inflow_count = min_inflow_count
    if credit_cap is not None:
        p.credit_cap = credit_cap
    if min_band:
        band = min_band.strip().upper()
        if band not in BAND_LADDER:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_band",
                        "message": f"min_band must be one of {', '.join(BAND_LADDER)}."},
            )
        p.min_band = band
    return p


MAX_STATEMENT_FILES = int(os.getenv("MAX_STATEMENT_FILES", "12"))


def _providers_for(files: List[UploadFile], providers: List[str]) -> List[Optional[str]]:
    """One provider per file. A single provider applies to every file."""
    providers = [p for p in (providers or []) if p] or [None]
    if len(providers) == 1:
        return providers * len(files)
    if len(providers) != len(files):
        raise HTTPException(status_code=400, detail={
            "error": "provider_count_mismatch",
            "message": f"{len(files)} files were sent with {len(providers)} providers. "
                       "Send one provider per file, or a single provider for all of them."})
    return providers


async def _underwrite(files: List[UploadFile], providers: List[str], policy: Policy, *,
                      monthly_emi=None, requested_amount=None, customer_type=None) -> dict:
    """Parse and underwrite one or more statements into one report.

    One account (however many files) returns exactly the single-statement
    report. Several accounts are analysed separately and combined; see
    app/combine.py.
    """
    if len(files) > MAX_STATEMENT_FILES:
        raise HTTPException(status_code=400, detail={
            "error": "too_many_files",
            "message": f"At most {MAX_STATEMENT_FILES} statement files can be assessed together."})
    started = time.perf_counter()
    parsed = []
    for f, prov in zip(files, _providers_for(files, providers)):
        data = await _read_pdf(f)
        try:
            parsed.append((f.filename, parse_statement(data, provider=prov)))
        except StatementParseError as exc:
            if len(files) == 1:
                raise
            raise StatementParseError(f"{f.filename}: {exc}") from exc

    groups = group_by_account(parsed)
    for g in groups:
        st = g["statement"]
        g["label"] = account_label(st)
        g["report"] = analyze(st, policy, monthly_emi=monthly_emi,
                              requested_amount=requested_amount, customer_type=customer_type)
        g["meta"] = _statement_meta(", ".join(g["files"]), st, started)
        g["meta"]["files"] = g["files"]
        g["report"]["meta"] = g["meta"]

    if len(groups) == 1:
        report = groups[0]["report"]
        if len(files) > 1:
            report["meta"]["duplicates_dropped"] = groups[0]["duplicates_dropped"]
            report["meta"]["gaps"] = groups[0]["gaps"]
            for gap in groups[0]["gaps"]:
                report.setdefault("edge_cases", []).insert(0, {
                    "code": "STATEMENT_GAP", "severity": "high",
                    "title": "Gap between statement files",
                    "detail": f"No file covers {gap['from']} to {gap['to']}. Transactions in "
                              "that stretch are missing from every figure.",
                    "evidence": gap})
    else:
        report = combine_reports(groups, policy, monthly_emi=monthly_emi,
                                 requested_amount=requested_amount, customer_type=customer_type)
        metas = [g["meta"] for g in groups]
        report["meta"] = {
            "filename": ", ".join(f.filename for f in files),
            "files": [f.filename for f in files],
            "provider": report["account"]["provider"],
            "source_format": "combined",
            "pages": sum(m["pages"] for m in metas),
            "rows_parsed": sum(m["rows_parsed"] for m in metas),
            "rows_declared": None if any(m["rows_declared"] is None for m in metas)
            else sum(m["rows_declared"] for m in metas),
            "rows_rejected": sum(m["rows_rejected"] for m in metas),
            "parse_complete": all(m["parse_complete"] for m in metas),
            "balance_reconciles": (False if any(m["balance_reconciles"] is False for m in metas)
                                   else None if any(m["balance_reconciles"] is None for m in metas)
                                   else True),
            "reconciliation": report["account"]["reconciliation"],
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    report["_statements"] = [g["statement"] for g in groups]
    report["name_check"] = _name_check(report["_statements"])
    return report


def _name_check(statements, bureau_name: str | None = None,
                bureau_phones: list[str] | None = None) -> dict:
    """Every statement's holder name against the name it must belong to.

    The reference is the bureau applicant when there is one. Without a bureau
    report, several statements are compared against the first one's holder,
    so two people's accounts cannot be combined without someone noticing. Any
    name that does not match needs an underwriter to check it and say why.
    """
    ref_name = bureau_name or next((st.header.customer for st in statements
                                    if st.header.customer), None)
    items = []
    for st in statements:
        c = _compare_identity(bureau_name=ref_name, bureau_phones=bureau_phones or [],
                              statement_name=st.header.customer,
                              statement_msisdn=st.header.msisdn)
        status = ("match" if c["name_match"] else
                  "not_assessed" if c["status"] == "not_assessed" else "mismatch")
        items.append({
            "account": f"{st.header.provider or 'Statement'} · {st.header.msisdn or 'account not stated'}",
            "account_id": st.header.msisdn,
            "holder": st.header.customer,
            "status": status,
            "name_tokens_matched": c["name_tokens_matched"],
        })
    open_items = [i for i in items if i["status"] != "match"]
    return {
        "reference": "bureau" if bureau_name else "first_statement",
        "reference_name": ref_name,
        "items": items,
        "mismatch_count": sum(i["status"] == "mismatch" for i in items),
        "unverifiable_count": sum(i["status"] == "not_assessed" for i in items),
        # One statement and no bureau report: there is nothing to compare.
        "needs_review": bool(open_items) and (bool(bureau_name) or len(items) > 1),
    }


def _identity_for(report: dict, bureau_name, bureau_phones) -> None:
    """Reconcile every account against the bureau applicant; keep the worst."""
    rank = {"mismatch": 0, "not_assessed": 1, "match_name_only": 2, "match": 3}
    checks = [
        _compare_identity(bureau_name=bureau_name, bureau_phones=bureau_phones,
                          statement_name=st.header.customer, statement_msisdn=st.header.msisdn)
        for st in report["_statements"]
    ]
    worst = min(checks, key=lambda c: rank.get(c["status"], 1))
    if len(checks) > 1:
        worst = {**worst, "per_account": checks}
    report["identity_check"] = worst
    report["name_check"] = _name_check(report["_statements"], bureau_name, bureau_phones)
    _apply_identity_to_report(report, worst)


@app.post("/api/v1/crb/check")
async def crb_check(
    file: UploadFile = File(..., description="Credit bureau report PDF"),
    credit_cap: Optional[int] = Form(None),
    min_band: Optional[str] = Form(None),
):
    """Step 1 of the process. Decides whether the statement may be parsed at all."""
    data = await _read_pdf(file)
    started = time.perf_counter()
    report = parse_credit_report(data)
    policy = _policy_from_form(credit_cap=credit_cap, min_band=min_band)
    result = assess_credit_report(report, policy)
    result["meta"] = {
        "filename": file.filename,
        "pages": report.pages,
        "accounts_parsed": len(report.accounts),
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }
    return result


@app.post("/api/v1/applications/assess")
async def assess_application(
    crb_file: UploadFile = File(..., description="Credit bureau report PDF"),
    statement_file: Optional[List[UploadFile]] = File(
        None, description="Statement PDF(s). Repeat the field to combine several."),
    statement_provider: Optional[List[str]] = Form(
        None, description="mtn_momo | bank_of_kigali; one per file, or one for all"),
    large_txn_threshold: Optional[int] = Form(None),
    min_inflow_count: Optional[int] = Form(None),
    credit_cap: Optional[int] = Form(None),
    min_band: Optional[str] = Form(None),
    customer_type: Optional[str] = Form(None),
):
    """The enforced end-to-end flow.

    The bureau report is always assessed first. The statement is parsed only
    when the gate opens - a caller cannot skip the bureau check by supplying
    both files, because the statement is not touched until the band clears.
    """
    policy = _policy_from_form(large_txn_threshold, min_inflow_count, credit_cap, min_band)

    crb_bytes = await _read_pdf(crb_file)
    started = time.perf_counter()
    report = parse_credit_report(crb_bytes)
    crb = assess_credit_report(report, policy)
    crb["meta"] = {
        "filename": crb_file.filename,
        "pages": report.pages,
        "accounts_parsed": len(report.accounts),
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }

    out: dict = {"stage": "credit_bureau", "credit_bureau": crb, "statement": None}

    if not crb["gate"]["statement_parsing_allowed"]:
        out["statement_skipped"] = {
            "reason": crb["gate"]["reason"],
            "band": crb["gate"]["band"],
            "minimum_band": crb["gate"]["minimum_band"],
        }
        out["decision"] = crb["gate"]["decision"]
        return out

    if not statement_file:
        out["statement_skipped"] = {
            "reason": "The gate is open but no statement was supplied. "
                      "Re-send with statement_file to complete the assessment.",
        }
        out["decision"] = "awaiting_statement"
        return out

    crb_exposure = crb.get("exposure", {})
    # Eligibility deducts only known EMI amounts belonging to ongoing repayment
    # accounts. Written-off/settled accounts are excluded by the CRB analyzer.
    # A zero total is valid when there is no ongoing EMI to deduct.
    crb_emi = crb_exposure.get("scheduled_monthly_payments")
    if crb_emi is None:
        crb_emi = 0
    analysis = await _underwrite(statement_file, statement_provider or [], policy,
                                 monthly_emi=crb_emi, customer_type=customer_type)
    analysis.setdefault("summary", {}).setdefault("loan_eligibility", {})["crb_emi_status"] = crb_exposure.get("scheduled_monthly_payments_status")
    out["stage"] = "complete"
    out["statement"] = analysis
    out["decision"] = crb["gate"]["recommendation"]
    _identity_for(analysis, report.profile.names,
                  [p["number"] for p in report.profile.phones])
    analysis.pop("_statements", None)
    out["identity_check"] = analysis["identity_check"]
    return out


def _compare_identity(bureau_name: str | None, bureau_phones: list[str],
                      statement_name: str | None, statement_msisdn: str | None) -> dict:
    """Compare a statement's account holder against the bureau applicant.

    Name is compared on tokens because the bureau prints 'MR TUYISENGE AUDACE'
    where the statement prints 'Audace TUYISENGE'. The MSISDN is checked against
    the bureau phone list, which frequently lags - a miss is reported, never
    treated as proof of fraud. A NAME mismatch is different: it means the two
    documents are probably about two different people, and nothing downstream
    is meaningful until that is resolved.
    """
    drop = {"MR", "MRS", "MS", "MISS", "DR"}
    b = {t for t in (bureau_name or "").upper().split() if t not in drop}
    s_ = {t for t in (statement_name or "").upper().split() if t not in drop}
    overlap = b & s_

    def tail(n: str) -> str:
        d = "".join(ch for ch in (n or "") if ch.isdigit())
        return d[-9:]

    stmt_tail = tail(statement_msisdn)
    phone_tails = {tail(p) for p in (bureau_phones or [])}
    phone_match = bool(stmt_tail) and stmt_tail in phone_tails
    name_match = (len(overlap) >= 2 or (bool(b) and b <= s_) or (bool(s_) and s_ <= b))
    assessable = bool(b) and bool(s_)

    if not assessable:
        status, note = "not_assessed", (
            "One of the two documents does not carry a holder name, so the two cannot "
            "be reconciled. Confirm by hand that they describe the same applicant."
        )
    elif name_match and phone_match:
        status, note = "match", (
            "Name and statement number both reconcile to the bureau profile."
        )
    elif name_match:
        status, note = "match_name_only", (
            "Name matches. The statement number is not on the bureau phone list, which "
            "is common because bureau phone data lags. Confirm the number belongs to "
            "this applicant before treating both documents as one customer."
        )
    else:
        status, note = "mismatch", (
            f"The bureau report is for {bureau_name!r} and the statement is for "
            f"{statement_name!r}. These do not appear to be the same person. Every "
            "figure derived from this pairing - exposure, EMI, affordability, the "
            "sanction - is meaningless until the correct statement is supplied."
        )

    return {
        "status": status,
        "bureau_name": bureau_name,
        "statement_name": statement_name,
        "name_tokens_matched": sorted(overlap),
        "name_match": name_match,
        "statement_msisdn": statement_msisdn,
        "msisdn_on_bureau_profile": phone_match,
        "bureau_phone_count": len(bureau_phones or []),
        "note": note,
    }


def _apply_identity_to_report(report: dict, ident: dict) -> None:
    """Put an identity failure where it cannot be scrolled past.

    A statement belonging to someone else is not a data-quality footnote: the
    exposure, the EMI, the affordability and the sanction are all built on the
    assumption that the two documents describe one person. If they do not, none
    of those numbers mean anything.
    """
    if ident.get("status") == "match":
        return
    severity = "high" if ident.get("status") == "mismatch" else "medium"
    title = ("Statement and bureau report are for different people"
             if ident.get("status") == "mismatch"
             else "Statement holder could not be fully reconciled")
    report.setdefault("edge_cases", []).insert(0, {
        "code": "IDENTITY_MISMATCH" if severity == "high" else "IDENTITY_PARTIAL",
        "severity": severity,
        "title": title,
        "detail": ident.get("note"),
        "evidence": {k: ident[k] for k in
                     ("bureau_name", "statement_name", "name_tokens_matched",
                      "statement_msisdn", "msisdn_on_bureau_profile") if k in ident},
    })
    if ident.get("status") == "mismatch":
        report.setdefault("red_flags", []).insert(0, {
            "code": "IDENTITY_MISMATCH",
            "title": "Statement is not this applicant's",
            "severity": "severe",
            "metric": f"Bureau {ident.get('bureau_name')!r} vs statement {ident.get('statement_name')!r}",
            "summary": ident.get("note"),
            "evidence": {"bureau_name": ident.get("bureau_name"),
                         "statement_name": ident.get("statement_name"),
                         "name_tokens_matched": ident.get("name_tokens_matched")},
        })


@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "version": app.version, "password_protected": bool(ACCESS_PASSWORD)}


@app.get("/api/v1/sheets/status")
async def sheets_status():
    """Lets the console decide whether to offer the Google Sheets export."""
    return {"configured": sheets.configured(),
            "url": sheets.spreadsheet_url() if sheets.configured() else None}


@app.post("/api/v1/sheets/save")
def sheets_save(payload: dict = Body(...)):
    """Write one assessment to its customer's tab, replacing what was there.

    The body is what the console already holds: `credit_bureau` (the
    /crb/check result), `statement` (the /analyze result), either of which may
    be null, and `files` with the two upload names. Sync on purpose: FastAPI
    runs it in a worker thread, so the blocking Google call never stalls the
    event loop.
    """
    if not sheets.configured():
        raise HTTPException(status_code=503, detail={
            "error": "sheets_not_configured",
            "message": "Google Sheets export is not configured on this server.",
            "hint": "Set GOOGLE_SHEETS_ID and GOOGLE_SERVICE_ACCOUNT_FILE in the service settings.",
        })
    crb, report = payload.get("credit_bureau"), payload.get("statement")
    if not isinstance(crb, (dict, type(None))) or not isinstance(report, (dict, type(None))):
        raise HTTPException(status_code=400, detail={
            "error": "invalid_payload", "message": "credit_bureau and statement must be objects or null."})
    try:
        return sheets.save(crb, report, payload.get("files") or {})
    except sheets.SheetsError as exc:
        log.warning("Sheets export failed: %s", exc)
        raise HTTPException(status_code=502, detail={"error": "sheets_failed", "message": str(exc)})


@app.get("/api/v1/providers")
async def get_providers():
    """Populates the console's provider select.

    The operator picks a provider, not a file format: they know which
    institution issued the statement and cannot reliably know which export
    version it is. The format within the provider is detected.
    """
    return {"providers": provider_options()}


@app.get("/api/v1/policy")
async def get_policy():
    return DEFAULT_POLICY.as_dict()


@app.post("/api/v1/analyze")
async def analyze_statement(
    file: List[UploadFile] = File(
        ..., description="Statement PDF. Repeat the field to combine several statements."),
    provider: Optional[List[str]] = Form(
        None, description="mtn_momo | bank_of_kigali; one per file, or one for all"),
    large_txn_threshold: Optional[int] = Form(None),
    min_inflow_count: Optional[int] = Form(None),
    credit_cap: Optional[int] = Form(None),
    monthly_emi: Optional[int] = Form(None),
    requested_amount: Optional[int] = Form(None),
    customer_type: Optional[str] = Form(None),
    bureau_name: Optional[str] = Form(None, description="Applicant name from the bureau report"),
    bureau_phones: Optional[str] = Form(None, description="Comma-separated bureau phone numbers"),
):
    """Parse and underwrite a statement.

    This is the raw engine and applies no bureau gate. For the enforced process
    order use /api/v1/applications/assess, which will not touch the statement
    until the bureau band clears.

    Several files may be sent (repeat `file`, and `provider` once per file or
    once for all). Files of the same account are merged; different accounts are
    analysed separately and combined into one outcome.

    When `bureau_name` is supplied the statement's account holder is reconciled
    against it. The console always sends it, so a statement belonging to a
    different person cannot be underwritten against someone else's bureau file.
    """
    policy = _policy_from_form(large_txn_threshold, min_inflow_count, credit_cap)
    report = await _underwrite(file, provider or [], policy, monthly_emi=monthly_emi,
                               requested_amount=requested_amount, customer_type=customer_type)
    report["meta"]["customer_type"] = customer_type or "new"
    report["meta"]["requested_amount"] = requested_amount
    if bureau_name or bureau_phones:
        _identity_for(report, bureau_name,
                      [p.strip() for p in (bureau_phones or "").split(",") if p.strip()])
    report.pop("_statements", None)
    return report


@app.post("/api/v1/transactions")
async def extract_transactions(
    file: UploadFile = File(...),
    provider: Optional[str] = Form(None, description="mtn_momo | bank_of_kigali"),
):
    """Parse only. Useful for reconciliation or loading into a warehouse."""
    data = await _read_pdf(file)
    statement = parse_statement(data, provider=provider)
    return {
        "account": statement.header.dict(),
        "source_format": statement.source_format,
        "capabilities": statement.capabilities.dict(),
        "pages": statement.pages,
        "count": len(statement.transactions),
        "parse_complete": statement.parse_complete,
        "balance_reconciles": statement.balance_reconciles,
        "reconciliation": statement.reconciliation_note(),
        "rejected_rows": [r.dict() for r in statement.rejected_rows],
        "transactions": [t.dict() for t in statement.transactions],
    }


# ---------------------------------------------------------------------------
# Serve the built console from the API when a frontend build is present.
#
# This keeps a single-box deployment to one process and one port, which
# removes CORS entirely: the console fetches /api/v1/... on its own origin.
# On Windows in particular this matters, because there is no gunicorn to
# supervise a second process. Absent a build, the API runs headless and the
# console is served separately by Vite in development.
#
# Must be mounted last - a mount at "/" would otherwise shadow the API routes.
# ---------------------------------------------------------------------------
_default_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
FRONTEND_DIST = Path(os.getenv("FRONTEND_DIST", _default_dist))

if FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="console")
    log.info("Serving console from %s", FRONTEND_DIST)
else:
    log.info("No frontend build at %s - running API only", FRONTEND_DIST)

    @app.get("/")
    async def root():
        return {
            "service": "MoMo Statement Underwriting API",
            "version": app.version,
            "docs": "/docs",
            "console": "not built - run 'npm run build' in frontend/",
        }
