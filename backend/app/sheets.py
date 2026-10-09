"""
Google Sheets export: the underwriting workbook.

The console posts the bureau result and the statement report here once an
assessment finishes. They are written to one spreadsheet laid out as a
working dataset, not just a printout:

    Register          One row per assessment, appended, never overwritten.
                      Key figures as typed columns, plus columns the
                      underwriters own (decision, approved amount, comments)
                      that this module never writes to after the row is added.
    Monthly revenue   One row per statement month     } long format, keyed by
    Bureau accounts   One row per open bureau account } Assessment ID, so they
    Findings          One row per risk finding        } filter and pivot across
                                                        every customer.
    <customer> tabs   The full report for that customer's latest assessment,
                      laid out like the console. Generated: re-assessing the
                      customer replaces it. History lives in the tabs above.

Re-assessing a customer marks their earlier Register rows Current = "no", so a
filter on Current = "yes" is always one row per customer.

Off unless GOOGLE_SHEETS_ID and GOOGLE_SERVICE_ACCOUNT_FILE are set. The
service account must be given Editor access to the spreadsheet. The service
itself still writes nothing to local disk.
"""
from __future__ import annotations

import math
import os
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

SHEET_ID = os.getenv("GOOGLE_SHEETS_ID", "").strip()
KEY_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
API = "https://sheets.googleapis.com/v4/spreadsheets"


class SheetsError(Exception):
    """A failure the console should show the underwriter verbatim."""


def configured() -> bool:
    return bool(SHEET_ID and KEY_FILE)


def spreadsheet_url(sheet_gid: Optional[int] = None) -> str:
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    return f"{url}#gid={sheet_gid}" if sheet_gid is not None else url


# ---------------------------------------------------------------------------
# Typed values. Plain ints are whole RWF amounts or counts; these mark the
# rest so each lands in the sheet as a real number with the right format,
# not as text that cannot be summed or sorted.
# ---------------------------------------------------------------------------
class Pct(float):
    """A fraction, shown as a percentage."""


class Stamp(float):
    """A Sheets date-time serial (days since 1899-12-30)."""

    @classmethod
    def of(cls, dt: datetime) -> "Stamp":
        return cls((dt - datetime(1899, 12, 30)).total_seconds() / 86400)


class Formula(str):
    """Written as a formula rather than as text."""


def _pct(v: Any) -> Optional[Pct]:
    return None if v is None else Pct(v)


def _human(v: Any) -> Any:
    """'not_eligible' -> 'Not eligible'. Leaves free text alone."""
    if isinstance(v, str) and re.fullmatch(r"[a-z][a-z0-9_]*", v):
        return v.replace("_", " ").capitalize()
    return v


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, (list, tuple)):
        return ", ".join(str(_cell(x)) for x in v)
    return v if isinstance(v, Formula) else str(v)


# ---------------------------------------------------------------------------
# Tab naming
# ---------------------------------------------------------------------------
_TITLE_FORBIDDEN = re.compile(r"[\[\]:*?/\\'\"]")


def tab_title(crb: Optional[dict], report: Optional[dict]) -> str:
    """A stable tab name for the customer, so a re-run lands on the same tab.

    The bureau national ID is the identity key when there is a bureau report.
    A statement-only run (bureau skipped) has no national ID, so it keys on
    the statement holder and MSISDN instead.
    """
    applicant = (crb or {}).get("applicant") or {}
    account = (report or {}).get("account") or {}
    name = applicant.get("names") or account.get("customer") or "Unknown applicant"
    ident = applicant.get("national_id") or account.get("msisdn") or ""

    name = " ".join(_TITLE_FORBIDDEN.sub(" ", str(name)).split())
    ident = _TITLE_FORBIDDEN.sub("", str(ident)).strip()
    suffix = f" · {ident}" if ident else ""
    # Sheets caps titles at 100 characters; the identifier is the part that
    # must survive, so the name is what gets shortened.
    return name[: 100 - len(suffix)].rstrip() + suffix


# ---------------------------------------------------------------------------
# The workbook's data tabs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Col:
    header: str
    kind: str = "text"      # text | int | pct | stamp | date | link
    width: int = 120
    human: bool = False     # owned by underwriters; never written after the row is added


REGISTER = "Register"
REGISTER_COLS = [
    Col("Assessment ID", width=170),
    Col("Assessed at", "stamp", 130),
    Col("Customer", "link", 260),
    Col("Applicant", width=200),
    Col("National ID", width=150),
    Col("Statement number", width=130),
    Col("Customer type", width=110),
    Col("Stage", width=230),
    Col("Bureau band", width=95),
    Col("Bureau score", "int", 95),
    Col("Probability of default", "pct", 110),
    Col("Bureau outcome", width=120),
    Col("Identity check", width=130),
    Col("Statement verdict", width=180),
    Col("Gross inflows (RWF)", "int", 130),
    Col("Net revenue (RWF)", "int", 130),
    Col("Avg monthly net revenue (RWF)", "int", 150),
    Col("Monthly EMI (RWF)", "int", 120),
    Col("Net inflow (RWF)", "int", 120),
    Col("Fee cap (RWF)", "int", 110),
    Col("Requested (RWF)", "int", 120),
    Col("Eligible (RWF)", "int", 120),
    Col("Sanctioned (RWF)", "int", 120),
    Col("Eligibility", width=110),
    Col("Severe findings", "int", 90),
    Col("Elevated findings", "int", 90),
    Col("Open bureau balance (RWF)", "int", 140),
    Col("Bureau arrears (RWF)", "int", 130),
    Col("Current", width=80),
    Col("Underwriter decision", width=150, human=True),
    Col("Approved amount (RWF)", "int", 140, human=True),
    Col("Underwriter", width=140, human=True),
    Col("Decision date", "date", 110, human=True),
    Col("Comments", width=320, human=True),
]
DECISIONS = ["Pending", "Approved", "Declined", "Referred", "Withdrawn"]
CURRENT_COL = next(i for i, c in enumerate(REGISTER_COLS) if c.header == "Current")
CUSTOMER_COL = next(i for i, c in enumerate(REGISTER_COLS) if c.header == "Customer")

# Detail tabs. Column B ("Current") is filled by one formula in its header
# cell that looks each Assessment ID up in the Register, so it stays right as
# assessments are superseded without the app rewriting old rows.
_KEY = [Col("Assessment ID", width=170), Col("Current", width=80), Col("Customer", width=240)]
DETAIL_TABS: dict[str, list[Col]] = {
    "Monthly revenue": _KEY + [
        Col("Month", width=90), Col("Days covered", "int", 100), Col("Full month", width=90),
        Col("Gross inflows (RWF)", "int", 130), Col("Self-transfers (RWF)", "int", 130),
        Col("Loan inflows (RWF)", "int", 130), Col("Bank / e-wallet (RWF)", "int", 140),
        Col("Large txns (RWF)", "int", 120), Col("Net revenue (RWF)", "int", 130),
        Col("Net revenue per 30 days (RWF)", "int", 170), Col("In average", width=150),
        Col("Inflow count", "int", 100),
    ],
    "Bureau accounts": _KEY + [
        Col("Ref", width=110), Col("Sector", width=170), Col("Type", width=160),
        Col("Classification", width=130), Col("Status", width=100), Col("Listing date", width=110),
        Col("Repayment terms", width=130), Col("Principal (RWF)", "int", 120),
        Col("Balance (RWF)", "int", 120), Col("Monthly EMI (RWF)", "int", 120),
        Col("Counted in EMI", width=110), Col("Arrears (RWF)", "int", 120),
        Col("Days in arrears", "int", 110), Col("Non-performing", width=120),
    ],
    "Findings": _KEY + [
        Col("Source", width=110), Col("Severity", width=100), Col("Code", width=170),
        Col("Finding", width=300), Col("Detail", width=520),
    ],
}
# Fixed sheet IDs let the tabs be created and formatted in one atomic request.
TAB_IDS = {REGISTER: 910001, "Monthly revenue": 910002, "Bureau accounts": 910003, "Findings": 910004}


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


# ---------------------------------------------------------------------------
# Rows for the data tabs
# ---------------------------------------------------------------------------
def _verdict(report: dict) -> str:
    """Same wording as the console's AssessmentStatus banner."""
    s = report.get("summary") or {}
    flags = report.get("red_flags") or []
    meta, d = report.get("meta") or {}, report.get("disclosures") or {}
    if s.get("revenue_assessed") is False:
        return "Not assessed"
    if any(f.get("severity") == "severe" for f in flags):
        return "Review required"
    if any(f.get("severity") == "elevated" for f in flags):
        return "Assessed — review advised"
    if meta.get("parse_complete") is False or d.get("parse_complete") is False:
        return "Review required"
    return "Assessed — no findings"


def _stage(crb: Optional[dict], report: Optional[dict]) -> str:
    if report and crb:
        return "Bureau and statement"
    if report:
        return "Statement only (no bureau report)"
    gate = (crb or {}).get("gate") or {}
    return "Bureau only — blocked at the gate" if gate.get("statement_parsing_allowed") is False \
        else "Bureau only"


def register_row(aid: str, at: datetime, title: str, gid: int,
                 crb: Optional[dict], report: Optional[dict]) -> list[Any]:
    crb, report = crb or {}, report or {}
    a, g, e = crb.get("applicant") or {}, crb.get("gate") or {}, crb.get("exposure") or {}
    acc, meta = report.get("account") or {}, report.get("meta") or {}
    s = report.get("summary") or {}
    t, el = s.get("totals") or {}, s.get("loan_eligibility") or {}
    flags = report.get("red_flags") or []
    pd = g.get("probability_of_default")
    values = {
        "Assessment ID": aid,
        "Assessed at": Stamp.of(at),
        "Customer": Formula(f'=HYPERLINK("#gid={gid}","{title}")'),
        "Applicant": a.get("names") or acc.get("customer"),
        "National ID": a.get("national_id"),
        "Statement number": acc.get("msisdn"),
        "Customer type": _human(meta.get("customer_type") or el.get("customer_type")),
        "Stage": _stage(crb, report),
        "Bureau band": g.get("band"),
        "Bureau score": g.get("score"),
        "Probability of default": _pct(pd / 100 if pd is not None else None),
        "Bureau outcome": _human(g.get("recommendation") or g.get("decision")),
        "Identity check": _human((report.get("identity_check") or {}).get("status")),
        "Statement verdict": _verdict(report) if report else None,
        "Gross inflows (RWF)": t.get("gross_inflows"),
        "Net revenue (RWF)": t.get("net_revenue"),
        "Avg monthly net revenue (RWF)": el.get("average_monthly_net_revenue"),
        "Monthly EMI (RWF)": el.get("monthly_emi", e.get("scheduled_monthly_payments")),
        "Net inflow (RWF)": el.get("net_inflow"),
        "Fee cap (RWF)": el.get("fee_cap"),
        "Requested (RWF)": meta.get("requested_amount") or el.get("requested_amount"),
        "Eligible (RWF)": el.get("loan_amount_eligibility"),
        "Sanctioned (RWF)": el.get("sanctioned_amount"),
        "Eligibility": _human(el.get("status")),
        "Severe findings": sum(1 for f in flags if f.get("severity") == "severe") if report else None,
        "Elevated findings": sum(1 for f in flags if f.get("severity") == "elevated") if report else None,
        "Open bureau balance (RWF)": e.get("total_open_balance"),
        "Bureau arrears (RWF)": e.get("total_arrears"),
        "Current": "yes",
        "Underwriter decision": "Pending",
    }
    return [values.get(c.header) for c in REGISTER_COLS]


def detail_rows(aid: str, title: str, crb: Optional[dict],
                report: Optional[dict]) -> dict[str, list[list[Any]]]:
    """Rows for each detail tab. Column B (Current) is left empty for the formula."""
    key = [aid, None, title]
    out: dict[str, list[list[Any]]] = {name: [] for name in DETAIL_TABS}

    s = (report or {}).get("summary") or {}
    el = s.get("loan_eligibility") or {}
    considered = {m.get("month"): m for m in el.get("months_considered") or []}
    in_average = {"averaged": "Yes", "excluded_too_short": "No — too short"}
    for r in s.get("rows") or []:
        m = considered.get(r.get("month"), {})
        out["Monthly revenue"].append(key + [
            r.get("month"), r.get("days_covered"), r.get("is_full_month"), r.get("gross_inflows"),
            r.get("self_transfers"), r.get("loan_inflows"), r.get("institutional_credits"),
            r.get("large_transactions"), r.get("net_revenue"), m.get("net_revenue_30d"),
            in_average.get(m.get("status"), "No — outside window") if m else "",
            r.get("inflow_count"),
        ])

    for a in ((crb or {}).get("exposure") or {}).get("accounts") or []:
        out["Bureau accounts"].append(key + [
            a.get("ref"), a.get("trade_sector"), a.get("type"), a.get("classification"),
            a.get("status"), a.get("listing_date"), a.get("repayment_terms"), a.get("principal"),
            a.get("balance"), a.get("emi_contribution"), a.get("counted_in_emi") is not False,
            a.get("arrears"), a.get("days_in_arrears") if a.get("days_in_arrears_reported") else None,
            bool(a.get("non_performing")),
        ])

    f_rows = out["Findings"]
    for f in (crb or {}).get("findings") or []:
        f_rows.append(key + ["Bureau", _human(f.get("severity")), f.get("code"), f.get("title"),
                             f.get("detail")])
    for f in (report or {}).get("red_flags") or []:
        f_rows.append(key + ["Statement", _human(f.get("severity")), f.get("code"), f.get("title"),
                             " ".join(x for x in (f.get("metric"), f.get("summary")) if x)])
    for i in (report or {}).get("edge_cases") or []:
        f_rows.append(key + ["Edge case", _human(i.get("severity")), i.get("code"), i.get("title"),
                             i.get("detail")])
    return out


# ---------------------------------------------------------------------------
# The customer report tab: JSON -> rows, mirroring the console's sections
# ---------------------------------------------------------------------------
class Layout:
    """Accumulates report rows and remembers what each row is, for styling."""

    def __init__(self) -> None:
        self.rows: list[list[Any]] = []
        self.kind: list[str] = []   # banner | meta | title | sub | head | data | total | pair | note | blank

    def _add(self, row: list[Any], kind: str) -> None:
        self.rows.append(row)
        self.kind.append(kind)

    def banner(self, text: str, meta: str) -> None:
        self._add([text], "banner")
        self._add([meta], "meta")

    def section(self, title: str) -> None:
        self._add([], "blank")
        self._add([title], "title")

    def subheading(self, text: str) -> None:
        self._add([text], "sub")

    def table(self, header: list[str], rows: Iterable[Iterable[Any]],
              total: Optional[list[Any]] = None) -> None:
        self._add(list(header), "head")
        for r in rows:
            self._add([_cell(v) for v in r], "data")
        if total is not None:
            self._add([_cell(v) for v in total], "total")

    def pairs(self, items: Iterable[tuple[str, Any]]) -> None:
        for k, v in items:
            self._add([k, _cell(v)], "pair")

    def note(self, text: Any) -> None:
        if text:
            self._add([str(text)], "note")


def _summary(lay: Layout, crb: Optional[dict], report: Optional[dict], files: dict) -> None:
    a = (crb or {}).get("applicant") or {}
    acc = (report or {}).get("account") or {}
    meta = (report or {}).get("meta") or {}
    gate = (crb or {}).get("gate") or {}
    elig = ((report or {}).get("summary") or {}).get("loan_eligibility") or {}

    lay.section("Summary")
    lay.pairs([
        ("Applicant", a.get("names") or acc.get("customer")),
        ("National ID", a.get("national_id")),
        ("Statement holder", acc.get("customer")),
        ("Statement number", acc.get("msisdn")),
        ("Customer type", _human(meta.get("customer_type") or elig.get("customer_type"))),
        ("Stage", _stage(crb, report)),
        ("Bureau outcome", _human(gate.get("recommendation") or gate.get("decision"))),
        ("Statement verdict", _verdict(report) if report else None),
        ("Requested amount (RWF)", meta.get("requested_amount") or elig.get("requested_amount")),
        ("Eligibility", _human(elig.get("status"))),
        ("Sanctioned amount (RWF)", elig.get("sanctioned_amount")),
        ("Bureau report file", files.get("crb") or ((crb or {}).get("meta") or {}).get("filename")),
        ("Statement file", files.get("statement") or meta.get("filename")),
    ])


def _bureau(lay: Layout, crb: dict) -> None:
    g = crb.get("gate") or {}
    e = crb.get("exposure") or {}
    pd = g.get("probability_of_default")

    lay.section("Credit bureau decision")
    lay.pairs([
        ("Band", g.get("band")),
        ("Score", g.get("score")),
        ("Probability of default", _pct(pd / 100 if pd is not None else None)),
        ("Minimum band", g.get("minimum_band")),
        ("Statement parsing allowed", g.get("statement_parsing_allowed")),
        ("Decision", _human(g.get("decision"))),
        ("Recommendation", _human(g.get("recommendation"))),
        ("Reason", g.get("reason")),
        ("Recommendation reason", g.get("recommendation_reason")),
        ("Report age (days)", g.get("report_age_days")),
        ("Maximum report age (days)", g.get("max_report_age_days")),
    ])

    lay.section("What the applicant already owes")
    lay.pairs([
        ("Active loans (open, performing)", e.get("ongoing_loan_count")),
        ("Open accounts", e.get("open_account_count")),
        ("Closed accounts", e.get("closed_account_count")),
        ("Total monthly EMI (RWF)", e.get("scheduled_monthly_payments")),
        ("Monthly EMI status", _human(e.get("scheduled_monthly_payments_status"))),
        ("Total outstanding (RWF)", e.get("total_open_balance")),
        ("Credit cap (RWF)", e.get("credit_cap")),
        ("Cap headroom (RWF)", e.get("cap_headroom")),
        ("Cap breached", e.get("cap_breached")),
        ("In arrears (RWF)", e.get("total_arrears")),
        ("Arrears share of balance", _pct(e.get("arrears_share_of_balance"))),
        ("Worst days in arrears", e.get("worst_days_in_arrears")
         if e.get("worst_days_in_arrears") is not None else "Not reported"),
    ])

    accounts = e.get("accounts") or []
    if accounts:
        lay.subheading("Open accounts")

        def emi(a: dict) -> Any:
            if a.get("counted_in_emi") is False:
                return "Excluded"
            if a.get("scheduled_payment_available") or a.get("calculated_emi_available"):
                return a.get("emi_contribution")
            return "Not reported"

        def last12(a: dict) -> str:
            rb = a.get("recent_repayment_behavior") or {}
            if not rb.get("months_recorded"):
                return "No data"
            s = f"{rb['months_recorded'] - rb.get('months_delayed', 0)}/{rb['months_recorded']} on time"
            if rb.get("months_delayed"):
                detail = ", ".join(f"{d.get('month')} ({d.get('days_late')}d)"
                                   for d in rb.get("delayed_detail") or [])
                s += f"; {rb['months_delayed']} delayed, worst {rb.get('worst_days_late')}d: {detail}"
            return s

        lay.table(
            ["Sector", "Type", "Class", "Listing date", "Repayment terms", "Principal",
             "Balance", "Monthly EMI", "Arrears", "Days", "Last 12 months", "Status"],
            ([a.get("trade_sector"), a.get("type"), a.get("classification"),
              a.get("listing_date"), a.get("repayment_terms"), a.get("principal"),
              a.get("balance"), emi(a), a.get("arrears"),
              a.get("days_in_arrears") if a.get("days_in_arrears_reported") else "",
              last12(a), a.get("status")] for a in accounts),
            total=["Counted toward monthly EMI", "", "", "", "", "", "",
                   e.get("scheduled_monthly_payments"), e.get("total_arrears")],
        )

    findings = crb.get("findings") or []
    lay.section("Bureau findings")
    if findings:
        lay.table(["Severity", "Finding", "Detail"],
                  ([_human(f.get("severity")), f.get("title"), f.get("detail")] for f in findings))
    else:
        lay.note("None observed.")

    p = e.get("portfolio")
    if p:
        perf, npa = p.get("performing") or {}, p.get("non_performing") or {}
        lay.section("Portfolio: performing and non-performing")
        lay.pairs([
            ("Open performing (PA)", perf.get("open_count")),
            ("PA value (RWF)", perf.get("open_value")),
            ("Open non-performing (NPA)", npa.get("open_count")),
            ("NPA value (RWF)", npa.get("open_value")),
            ("Reconciles with bureau summary", p.get("reconciles")),
        ])
        lay.subheading("By sector")
        lay.table(
            ["Sector", "Open PA", "PA value", "Open NPA", "NPA value"],
            ([label,
              (perf.get("by_sector") or {}).get(k, {}).get("open_count"),
              (perf.get("by_sector") or {}).get(k, {}).get("open_value"),
              (npa.get("by_sector") or {}).get(k, {}).get("open_count"),
              (npa.get("by_sector") or {}).get(k, {}).get("open_value")]
             for k, label in (("your_sector", "Your sector"), ("other_sectors", "Other sectors"))),
        )
        if p.get("checks"):
            lay.subheading("Reconciliation")
            lay.table(
                ["Check", "Parsed", "Bureau summary", "Result"],
                ([str(c.get("check", "")).replace("_", " ").capitalize(), c.get("parsed"),
                  c.get("bureau_summary"),
                  "Not stated" if c.get("matches") is None else "Matches" if c["matches"] else "Differs"]
                 for c in p["checks"]),
            )
        lay.note(p.get("note"))

    cheques = crb.get("bounced_cheques") or []
    if cheques:
        lay.section("Bounced cheques")
        lay.table(["Date bounced", "Cheque no.", "Sector", "Reason", "Amount"],
                  ([c.get("date_bounced") or c.get("cheque_date"), c.get("cheque_no"),
                    c.get("trade_sector"), c.get("reason"), c.get("amount")] for c in cheques))

    a = crb.get("applicant") or {}
    lay.section("Applicant profile")
    lay.pairs([
        ("Name", a.get("names")),
        ("National ID", a.get("national_id")),
        ("Date of birth", a.get("date_of_birth")),
        ("Gender", a.get("gender")),
        ("Marital status", a.get("marital_status")),
        ("Employment", a.get("employment")),
        ("Directorships", ", ".join(f"{d.get('company')} ({d.get('title')})"
                                    for d in a.get("directorships") or [])),
        ("Phones on file", ", ".join(p.get("number", "") for p in a.get("phones") or [])),
    ])

    t = crb.get("repayment_track_record") or {}
    if t:
        lay.section("Closed-facility track record")
        lay.table(
            ["Period", "Facilities", "Average principal"],
            ([r.get("period"), r.get("count"), r.get("average_principal")]
             for r in t.get("principal_escalation") or []),
            total=[f"{t.get('closed_small_facilities')} small facilities",
                   t.get("closed_accounts"), t.get("total_small_advanced")],
        )
        lay.note("Every closed facility was repaid without arrears."
                 if t.get("all_repaid_without_arrears")
                 else "Some closed facilities carried arrears.")
        lay.note(t.get("note"))

    notes = list(crb.get("notes") or [])
    if (crb.get("collateral") or {}).get("note"):
        notes.append(crb["collateral"]["note"])
    if notes:
        lay.section("Bureau notes and caveats")
        for n in notes:
            lay.note(n)


def _statement(lay: Layout, report: dict) -> None:
    s = report.get("summary") or {}
    t = s.get("totals") or {}
    d = report.get("disclosures") or {}
    flags = report.get("red_flags") or []

    ident = report.get("identity_check")
    if ident:
        lay.section("Identity check")
        lay.pairs([
            ("Status", _human(ident.get("status"))),
            ("Bureau name", ident.get("bureau_name")),
            ("Statement name", ident.get("statement_name")),
            ("Name tokens matched", ident.get("name_tokens_matched")),
            ("Statement number", ident.get("statement_msisdn")),
            ("Number on bureau profile", ident.get("msisdn_on_bureau_profile")),
            ("Note", ident.get("note")),
        ])

    gross, net = t.get("gross_inflows") or 0, t.get("net_revenue")
    lay.section("Statement verdict")
    lay.pairs([
        ("Assessment", _verdict(report)),
        ("Net revenue, period total (RWF)", net if s.get("revenue_assessed") is not False else "Not assessed"),
        ("Gross inflows (RWF)", gross),
        ("Excluded (RWF)", max(gross - (net or 0), 0)),
        ("Full-month run rate (RWF)", s.get("full_month_run_rate")),
        ("Full months", s.get("full_months")),
        ("Partial months", s.get("partial_months")),
        ("Severe findings", sum(1 for f in flags if f.get("severity") == "severe")),
        ("Elevated findings", sum(1 for f in flags if f.get("severity") == "elevated")),
        ("Transactions read", d.get("total_transaction_count")),
        ("Inflows counted", d.get("counted_inflow_count")),
    ])
    if s.get("not_assessed_reason"):
        lay.note(s["not_assessed_reason"])

    edge = report.get("edge_cases") or []
    if edge:
        lay.section("Edge cases: everything that qualifies the numbers")
        lay.table(["Severity", "Title", "Detail"],
                  ([{"high": "Must read", "medium": "Review"}.get(i.get("severity"), "Note"),
                    i.get("title"), i.get("detail")] for i in edge))

    rows = s.get("rows") or []
    lay.section("Net revenue summary (per month, RWF)")
    lay.table(
        ["Month", "Gross inflows", "(−) Self-transfers", "(−) Loan inflows", "(−) Bank / e-wallet",
         "(−) Large txns", "Net revenue", "Inflow count", "Days covered", "Full month"],
        ([r.get("month"), r.get("gross_inflows"), r.get("self_transfers"), r.get("loan_inflows"),
          r.get("institutional_credits"), r.get("large_transactions"), r.get("net_revenue"),
          r.get("inflow_count"), r.get("days_covered"), r.get("is_full_month")] for r in rows),
        total=["Total", t.get("gross_inflows"), t.get("self_transfers"), t.get("loan_inflows"),
               t.get("institutional_credits"), t.get("large_transactions"), t.get("net_revenue"),
               t.get("inflow_count")],
    )

    lay.section("Revenue calculation (RWF)")
    lay.pairs([
        ("Gross inflows", gross),
        ("(−) Self-transfers", t.get("self_transfers")),
        ("(−) Loan inflows", t.get("loan_inflows")),
        ("(−) Bank / e-wallet credits", t.get("institutional_credits")),
        ("(−) Large transactions", t.get("large_transactions")),
        ("Net revenue", net),
        ("Retained", _pct(net / gross) if gross and net is not None else None),
    ])

    e = s.get("loan_eligibility")
    if e:
        lay.section("Loan eligibility: how much can be disbursed?")
        lay.pairs([
            ("Status", _human(e.get("status"))),
            ("Reason", e.get("reason")),
            ("Customer type", _human(e.get("customer_type"))),
            ("Average monthly statement net revenue (RWF)", e.get("average_monthly_inflow")),
            ("Months used", e.get("months_used")),
            ("Monthly EMI (RWF)", e.get("monthly_emi")),
            ("Net inflow (RWF)", e.get("net_inflow")),
            ("Net revenue rate", _pct(e.get("net_revenue_rate"))),
            ("Net revenue (RWF)", e.get("net_revenue")),
            ("Fee cap rate", _pct(e.get("fee_cap_rate"))),
            ("Fee cap (RWF)", e.get("fee_cap")),
            ("Requested amount (RWF)", e.get("requested_amount")),
            ("Eligible amount (RWF)", e.get("loan_amount_eligibility")),
            ("Sanctioned amount (RWF)", e.get("sanctioned_amount")),
            ("Sanction basis", _human(e.get("sanction_basis"))),
            ("Band overlap", e.get("band_overlap_note")),
        ])
        months = e.get("months_considered") or e.get("monthly_inflows") or []
        if months:
            lay.subheading("Months considered")

            def status(m: dict) -> str:
                st = m.get("status")
                if st in (None, "averaged"):
                    return "Averaged"
                return "Excluded — too short" if st == "excluded_too_short" else "Outside window"

            lay.table(
                ["Month", "Days", "Net revenue", "Scale", "Per 30 days", "Status", "Reason"],
                ([m.get("month"), m.get("days_covered"), m.get("net_revenue"),
                  float(m["scale_factor"]) if m.get("scale_factor") else None,
                  m.get("net_revenue_30d"), status(m), m.get("reason")] for m in months),
                total=[f"Average of the {e.get('months_averaged', len(months))} months averaged",
                       "", "", "", e.get("average_monthly_net_revenue")],
            )
            lay.note(e.get("basis_note"))
        bands = e.get("bands") or []
        if bands:
            mb = e.get("matched_band") or {}
            lay.subheading(f"Eligibility bands ({_human(e.get('customer_type') or 'new').lower()} customer)")
            lay.table(
                ["Min fee cap", "Max fee cap", "Loan amount", "Selected"],
                ([b.get("min_fee_cap"),
                  "and above" if b.get("max_fee_cap") is None else b.get("max_fee_cap"),
                  b.get("loan_amount"),
                  "Selected" if mb and b.get("min_fee_cap") == mb.get("min_fee_cap")
                  and b.get("max_fee_cap") == mb.get("max_fee_cap") else ""] for b in bands),
            )

    if d:
        check = d.get("minimum_transaction_check") or {}
        lay.section("Mandatory disclosures")
        lay.table(["Disclosure", "Value", "Note"], [
            ["Total transactions read", d.get("total_transaction_count"),
             f"Statement declares {d.get('declared_transaction_count')}."],
            ["Inflows counted toward revenue", d.get("counted_inflow_count"),
             f"{d.get('inflow_count')} inflows in total."],
            ["Transactions above threshold", d.get("large_transaction_count"),
             f"{d.get('large_inflow_count')} inflows, {d.get('large_outflow_count')} outflows."],
            ["Distinct individuals sending money", d.get("distinct_individual_senders"),
             f"Of {d.get('distinct_senders_total')} senders; "
             f"{d.get('distinct_institution_senders')} are institutions or merchants."],
            ["Minimum-transaction check", _human(check.get("status")),
             check.get("note") or f"Observed {check.get('observed_inflow_count')} "
                                  f"against a threshold of {check.get('threshold')}."],
            ["Active days with inflow", check.get("active_days"),
             f"Credit cap in force: {(report.get('policy') or {}).get('credit_cap'):,} RWF."
             if (report.get("policy") or {}).get("credit_cap") is not None else ""],
        ])
        conc = d.get("concentration") or []
        if conc:
            lay.subheading("Month-concentration check")
            lay.table(
                ["Month", "Net", "Largest counterparty", "Share", "Top 3", "Verdict", "Reasons"],
                ([c.get("month"), c.get("net_revenue"),
                  ((c.get("top_counterparties") or [{}])[0] or {}).get("counterparty"),
                  _pct(c.get("top1_share")), _pct(c.get("top3_share")),
                  "Concentrated" if c.get("flagged") else "Broad",
                  " ".join(c.get("reasons") or [])] for c in conc),
            )

    meta = report.get("meta") or {}
    lay.section("Statement health")
    lay.pairs([
        ("Provider", meta.get("provider")),
        ("Source format", meta.get("source_format")),
        ("Pages", meta.get("pages")),
        ("Rows parsed", meta.get("rows_parsed")),
        ("Rows declared", meta.get("rows_declared")),
        ("Rows rejected", meta.get("rows_rejected")),
        ("Parse complete", meta.get("parse_complete")),
        ("Reconciliation", meta.get("reconciliation")),
        ("Distinct senders", d.get("distinct_senders_total")),
        ("Format limitations", len(d.get("capability_notes") or [])),
    ])

    flow = report.get("monthly_flow") or []
    if flow:
        lay.section("Where the money went (per month, RWF)")
        lay.table(["Month", "Inflow", "Outflow", "Retained", "Passed through"],
                  ([f.get("month"), f.get("inflow"), f.get("outflow"), f.get("retained"),
                    _pct(f.get("passthrough_ratio"))] for f in flow))

    lay.section("Risk findings")
    if flags:
        lay.table(["Severity", "Finding", "Metric", "Summary"],
                  ([_human(f.get("severity")), f.get("title"), f.get("metric"), f.get("summary")]
                   for f in flags))
    else:
        lay.note("None observed.")

    rec = report.get("reconciliation")
    if rec:
        lay.section("Reconciliation: every figure re-derived from its own rows")
        lay.table(["Check", "Computed", "Re-derived", "Result", "Detail"],
                  ([c.get("check"), c.get("computed"), c.get("expected"),
                    "Matches" if c.get("passes") else "DIFFERS", c.get("detail")]
                   for c in rec.get("checks") or []))

    details = report.get("details") or {}
    inst = (details.get("institutional_credits") or {}).get("items") or []
    if inst:
        lay.section("Institutional credits excluded")
        lay.table(["Date", "Txn ID", "Counterparty", "Number", "Type", "Amount", "Fee", "Balance after"],
                  ([str(r.get("timestamp") or "")[:10], r.get("txn_id"), r.get("counterparty"),
                    r.get("msisdn"), r.get("type"), r.get("amount"), r.get("fee"),
                    r.get("balance_after")] for r in inst))

    large = (details.get("large_transactions") or {}).get("inflows") or []
    if large:
        lay.section("Large transactions excluded in full")
        lay.table(["Date", "Counterparty", "Number", "Type", "Amount"],
                  ([str(r.get("timestamp") or "").replace("T", " ")[:16], r.get("counterparty"),
                    r.get("msisdn"), r.get("type"), r.get("amount")] for r in large))

    loans = details.get("loan_inflows") or {}
    if loans.get("ledger"):
        lay.section("Lender ledger")
        lay.table(["Date", "Direction", "Counterparty", "Amount"],
                  ([str(r.get("timestamp") or "").replace("T", " ")[:16], _human(r.get("direction")),
                    r.get("counterparty"), r.get("amount")] for r in loans["ledger"]))
    if loans.get("pattern_matches"):
        lay.section("Pattern-based loan cycles")
        lay.table(["Date", "Counterparty", "Basis", "Principal", "Repayment", "Repayments"],
                  ([str(r.get("timestamp") or "").replace("T", " ")[:16], r.get("counterparty"),
                    r.get("basis"), r.get("principal"), r.get("repayment_total_within_window"),
                    r.get("repayment_count")] for r in loans["pattern_matches"]))

    possible = (details.get("self_transfers") or {}).get("possible_for_review") or []
    if possible:
        lay.section("Possible self-transfers — for review")
        lay.table(["Counterparty", "Number", "In", "Why"],
                  ([r.get("counterparty"), r.get("msisdn"), r.get("amount"), r.get("reason")]
                   for r in possible))

    notes = d.get("capability_notes") or []
    if notes:
        lay.section("Checks this statement format cannot support")
        lay.table(["Check", "Status", "Why"],
                  ([n.get("check"), _human(n.get("status")), n.get("note")] for n in notes))

    if report.get("assumptions"):
        lay.section("Assumptions & data-quality notes")
        for a in report["assumptions"]:
            lay.note(a)


def build_layout(crb: Optional[dict], report: Optional[dict], files: Optional[dict] = None,
                 aid: str = "", at: Optional[datetime] = None) -> Layout:
    if not crb and not report:
        raise SheetsError("Nothing to save: no bureau result and no statement report.")
    at = at or datetime.now()
    lay = Layout()
    a = (crb or {}).get("applicant") or {}
    name = a.get("names") or ((report or {}).get("account") or {}).get("customer") or "Unknown applicant"
    lay.banner(f"Underwriting report — {name}",
               f"Assessment {aid} · {at:%d %b %Y %H:%M} · Generated by Ledger. This tab is replaced "
               "on the next assessment of this customer; record decisions in the Register tab.")
    _summary(lay, crb, report, files or {})
    if crb:
        _bureau(lay, crb)
    if report:
        _statement(lay, report)
    return lay


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _rgb(hex_: str) -> dict:
    h = hex_.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


INK, MUTED, RULE = _rgb("#1F2933"), _rgb("#6C7783"), _rgb("#D5DBE1")
NAVY, PANEL, HEAD_BG = _rgb("#1B2A3A"), _rgb("#E8EEF5"), _rgb("#F1F3F5")
GOOD, GOOD_BG = _rgb("#1A6B4A"), _rgb("#E3F1EA")
BAD, BAD_BG = _rgb("#A3271F"), _rgb("#FBE7E5")
WARN, WARN_BG = _rgb("#8A5A00"), _rgb("#FDF1D8")
FONT = "Arial"

NUMBER_FORMATS = {
    "int": {"type": "NUMBER", "pattern": "#,##0"},
    "float": {"type": "NUMBER", "pattern": "#,##0.0#"},
    "pct": {"type": "PERCENT", "pattern": "0.0%"},
    "stamp": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm"},
    "date": {"type": "DATE", "pattern": "yyyy-mm-dd"},
}


def _kind(v: Any) -> Optional[str]:
    if isinstance(v, Stamp):
        return "stamp"
    if isinstance(v, Pct):
        return "pct"
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    return None


def _value(v: Any) -> dict:
    if isinstance(v, bool):
        return {"stringValue": "Yes" if v else "No"}
    if isinstance(v, (int, float)):
        return {"numberValue": float(v)}
    if isinstance(v, Formula):
        return {"formulaValue": str(v)}
    return {"stringValue": str(v)}


def _cell_data(v: Any, fmt: Optional[dict] = None) -> dict:
    out: dict = {}
    if v not in ("", None):
        out["userEnteredValue"] = _value(v)
        k = _kind(v)
        if k:
            fmt = {"horizontalAlignment": "RIGHT", **(fmt or {}), "numberFormat": NUMBER_FORMATS[k]}
    if fmt:
        out["userEnteredFormat"] = fmt
    return out


def _text(size: int = 10, bold: bool = False, color: dict = INK, italic: bool = False) -> dict:
    return {"fontFamily": FONT, "fontSize": size, "bold": bold, "italic": italic,
            "foregroundColor": color}


def _border(color: dict = RULE, style: str = "SOLID") -> dict:
    return {"style": style, "color": color}


STYLE = {
    "banner": {"backgroundColor": NAVY, "textFormat": _text(15, True, _rgb("#FFFFFF")),
               "verticalAlignment": "MIDDLE", "padding": {"left": 10}},
    "meta": {"textFormat": _text(9, color=MUTED, italic=True), "wrapStrategy": "WRAP",
             "verticalAlignment": "TOP"},
    "title": {"backgroundColor": PANEL, "textFormat": _text(11, True, NAVY),
              "verticalAlignment": "MIDDLE", "borders": {"bottom": _border(NAVY, "SOLID_MEDIUM")}},
    "sub": {"textFormat": _text(10, True, MUTED)},
    "head": {"backgroundColor": HEAD_BG, "textFormat": _text(9, True, MUTED),
             "borders": {"bottom": _border(_rgb("#9AA5B1"))}, "wrapStrategy": "WRAP",
             "verticalAlignment": "BOTTOM"},
    "data": {"textFormat": _text(10), "borders": {"bottom": _border()}, "wrapStrategy": "WRAP",
             "verticalAlignment": "TOP"},
    "total": {"textFormat": _text(10, True), "borders": {"top": _border(INK, "SOLID_MEDIUM")},
              "verticalAlignment": "TOP"},
    "pair_key": {"textFormat": _text(10, color=MUTED), "borders": {"bottom": _border()},
                 "verticalAlignment": "TOP"},
    "pair_value": {"textFormat": _text(10, True), "borders": {"bottom": _border()},
                   "wrapStrategy": "WRAP", "verticalAlignment": "TOP", "horizontalAlignment": "LEFT"},
    "note": {"textFormat": _text(9, color=MUTED, italic=True), "wrapStrategy": "WRAP",
             "verticalAlignment": "TOP"},
}

MIN_COLS = 8
CHAR_PX = 7          # average Arial 10 character width, for width and height estimates
LINE_PX = 17


def _report_widths(lay: Layout, n_cols: int) -> list[int]:
    """Size each column to its table content, so no cell is a sliver or a runway."""
    widths = [90] * n_cols
    widths[0] = 230
    for row, kind in zip(lay.rows, lay.kind):
        if kind not in ("head", "data", "total"):
            continue
        for i, v in enumerate(row):
            text = f"{v:,.0f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
            cap = 360 if kind == "data" else 160
            widths[i] = max(widths[i], min(len(text) * CHAR_PX + 18, cap))
    return widths


def _numeric_columns(lay: Layout, head_at: int) -> set[int]:
    """Columns of the table headed at `head_at` whose body holds numbers. Their
    headings are right-aligned to sit over the right-aligned figures."""
    cols: set[int] = set()
    for row, kind in zip(lay.rows[head_at + 1:], lay.kind[head_at + 1:]):
        if kind not in ("data", "total"):
            break
        cols |= {i for i, v in enumerate(row) if _kind(v)}
    return cols


def _report_requests(gid: int, lay: Layout) -> list[dict]:
    """Every request that rewrites a customer tab, as one atomic batch."""
    n_cols = max(MIN_COLS, max(len(r) for r in lay.rows))
    n_rows = len(lay.rows) + 2
    widths = _report_widths(lay, n_cols)
    full = sum(widths)

    rows, merges, heights = [], [], {}
    for i, (row, kind) in enumerate(zip(lay.rows, lay.kind)):
        if kind == "pair":
            key, val = row[0], row[1] if len(row) > 1 else ""
            cells = [_cell_data(key, STYLE["pair_key"]), _cell_data(val, STYLE["pair_value"])]
            cells += [{"userEnteredFormat": STYLE["pair_value"]}] * (n_cols - 2)
            merges.append((i, 1, n_cols))
            if isinstance(val, str):
                heights[i] = len(val) / max((full - widths[0]) / CHAR_PX, 1)
        elif kind in ("banner", "meta", "title", "sub", "note"):
            style = STYLE[kind]
            cells = [_cell_data(row[0] if row else "", style)] + [{"userEnteredFormat": style}] * (n_cols - 1)
            merges.append((i, 0, n_cols))
            if kind in ("note", "meta"):
                heights[i] = len(str(row[0])) / max(full / (CHAR_PX - 1), 1)
        elif kind == "head":
            style, right = STYLE[kind], _numeric_columns(lay, i)
            cells = [_cell_data(v, {**style, "horizontalAlignment": "RIGHT"} if c in right else style)
                     for c, v in enumerate(row)]
            cells += [{"userEnteredFormat": style}] * (n_cols - len(row))
        elif kind in ("data", "total"):
            style = STYLE[kind]
            cells = [_cell_data(v, style) for v in row]
            cells += [{"userEnteredFormat": style}] * (n_cols - len(row))
        else:
            cells = []
        rows.append({"values": cells})

    rng = {"sheetId": gid, "startRowIndex": 0, "endRowIndex": n_rows, "startColumnIndex": 0,
           "endColumnIndex": n_cols}
    ops: list[dict] = [
        # Unmerge the whole tab before shrinking it: a merge left over from a
        # longer earlier report would otherwise straddle the cut.
        {"unmergeCells": {"range": {"sheetId": gid}}},
        {"updateSheetProperties": {
            "properties": {"sheetId": gid, "gridProperties": {
                "rowCount": n_rows, "columnCount": n_cols, "frozenRowCount": 1,
                "hideGridlines": True}},
            "fields": "gridProperties(rowCount,columnCount,frozenRowCount,hideGridlines)"}},
        {"updateCells": {"range": rng, "rows": rows, "fields": "userEnteredValue,userEnteredFormat"}},
    ]
    ops += [{"mergeCells": {"mergeType": "MERGE_ALL", "range": {
        "sheetId": gid, "startRowIndex": r, "endRowIndex": r + 1,
        "startColumnIndex": a, "endColumnIndex": b}}} for r, a, b in merges]
    ops += [{"updateDimensionProperties": {
        "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
        "properties": {"pixelSize": w}, "fields": "pixelSize"}} for i, w in enumerate(widths)]
    # Let Sheets fit the table rows, then set merged rows by hand: Sheets does
    # not grow a row to fit wrapped text in a merged cell.
    ops.append({"autoResizeDimensions": {"dimensions": {
        "sheetId": gid, "dimension": "ROWS", "startIndex": 0, "endIndex": n_rows}}})
    fixed = {0: 40}
    for i, kind in enumerate(lay.kind):
        if kind == "title":
            fixed[i] = 28
        elif kind == "blank":
            fixed[i] = 12
        elif i in heights:
            fixed[i] = max(21, math.ceil(heights[i]) * LINE_PX + 6)
    ops += [{"updateDimensionProperties": {
        "range": {"sheetId": gid, "dimension": "ROWS", "startIndex": r, "endIndex": r + 1},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}} for r, px in fixed.items()]
    return ops


def _outcome_color(crb: Optional[dict], report: Optional[dict]) -> dict:
    el = (((report or {}).get("summary") or {}).get("loan_eligibility")) or {}
    gate = (crb or {}).get("gate") or {}
    if gate.get("statement_parsing_allowed") is False or el.get("status") == "not_eligible":
        return BAD
    if el.get("status") == "eligible" and gate.get("recommendation") not in ("refer",):
        return GOOD
    return WARN


def _data_tab_setup(name: str, cols: list[Col]) -> list[dict]:
    """Create one data tab with its header, formats, filter and guard rails."""
    gid = TAB_IDS[name]
    n = len(cols)
    index = list(TAB_IDS).index(name)
    ops: list[dict] = [{"addSheet": {"properties": {
        "sheetId": gid, "title": name, "index": index,
        "gridProperties": {"rowCount": 1000, "columnCount": n, "frozenRowCount": 1,
                           "frozenColumnCount": 3 if name == REGISTER else 1},
        "tabColor": NAVY}}}]

    header_cells = []
    for i, c in enumerate(cols):
        fmt = {**STYLE["head"], "textFormat": _text(10, True, _rgb("#FFFFFF")),
               "backgroundColor": _rgb("#5B6B7C") if c.human else NAVY,
               "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}
        if name != REGISTER and c.header == "Current":
            # One formula fills the whole column from the Register.
            last = _col_letter(CURRENT_COL)
            # FILTER keeps the spill to rows that have data, so appended rows
            # still land directly under the last assessment.
            value = Formula(f'={{"Current";ARRAYFORMULA(IFERROR(VLOOKUP(FILTER(A2:A,A2:A<>""),'
                            f"'{REGISTER}'!A:{last},{CURRENT_COL + 1},FALSE),\"\"))}}")
        else:
            value = c.header
        header_cells.append(_cell_data(value, fmt))
    ops.append({"updateCells": {
        "range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": n},
        "rows": [{"values": header_cells}], "fields": "userEnteredValue,userEnteredFormat"}})
    ops.append({"updateDimensionProperties": {
        "range": {"sheetId": gid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 44}, "fields": "pixelSize"}})

    body = {"sheetId": gid, "startRowIndex": 1}
    ops.append({"repeatCell": {"range": body, "cell": {"userEnteredFormat": {
        "textFormat": _text(10), "verticalAlignment": "TOP"}},
        "fields": "userEnteredFormat(textFormat,verticalAlignment)"}})
    for i, c in enumerate(cols):
        col = {**body, "startColumnIndex": i, "endColumnIndex": i + 1}
        ops.append({"updateDimensionProperties": {
            "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": c.width}, "fields": "pixelSize"}})
        if c.kind in NUMBER_FORMATS:
            ops.append({"repeatCell": {"range": col, "cell": {"userEnteredFormat": {
                "numberFormat": NUMBER_FORMATS[c.kind]}},
                "fields": "userEnteredFormat.numberFormat"}})
        if c.width >= 300:
            ops.append({"repeatCell": {"range": col, "cell": {"userEnteredFormat": {
                "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat.wrapStrategy"}})
        if c.human:
            ops.append({"repeatCell": {"range": col, "cell": {"userEnteredFormat": {
                "backgroundColor": _rgb("#FFFBEA")}}, "fields": "userEnteredFormat.backgroundColor"}})

    ops.append({"setBasicFilter": {"filter": {"range": {"sheetId": gid}}}})
    # Light zebra striping that follows the data as rows are appended.
    ops.append({"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [{**body, "startColumnIndex": 0, "endColumnIndex": n}],
        "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                      "values": [{"userEnteredValue": "=AND($A2<>\"\",ISEVEN(ROW()))"}]},
                        "format": {"backgroundColor": _rgb("#F7F9FB")}}}}})
    # Superseded assessments read as greyed out.
    cur = _col_letter(CURRENT_COL if name == REGISTER else 1)
    ops.append({"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [{**body, "startColumnIndex": 0, "endColumnIndex": n}],
        "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                      "values": [{"userEnteredValue": f'=${cur}2="no"'}]},
                        "format": {"textFormat": {"foregroundColor": _rgb("#9AA5B1")}}}}}})

    app_cols = [i for i, c in enumerate(cols) if not c.human]
    ops.append({"addProtectedRange": {"protectedRange": {
        "range": {"sheetId": gid, "startColumnIndex": 0, "endColumnIndex": max(app_cols) + 1},
        "description": "Written by Ledger. Edit the underwriter columns instead.",
        "warningOnly": True}}})

    if name == REGISTER:
        ops += _register_rules(gid, cols)
    elif name == "Findings":
        for text, fg, bg in (("Severe", BAD, BAD_BG), ("Blocking", BAD, BAD_BG), ("High", BAD, BAD_BG),
                             ("Elevated", WARN, WARN_BG), ("Warning", WARN, WARN_BG),
                             ("Medium", WARN, WARN_BG)):
            ops.append(_text_rule(gid, 4, text, fg, bg))
    return ops


def _text_rule(gid: int, col: int, text: str, fg: dict, bg: dict) -> dict:
    return {"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [{"sheetId": gid, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1}],
        "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": text}]},
                        "format": {"backgroundColor": bg, "textFormat": {"foregroundColor": fg, "bold": True}}}}}}


def _register_rules(gid: int, cols: list[Col]) -> list[dict]:
    idx = {c.header: i for i, c in enumerate(cols)}
    body = {"sheetId": gid, "startRowIndex": 1}
    ops = [
        {"setDataValidation": {"range": {**body, "startColumnIndex": idx["Underwriter decision"],
                                         "endColumnIndex": idx["Underwriter decision"] + 1},
                               "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
                                   {"userEnteredValue": d} for d in DECISIONS]},
                                   "strict": True, "showCustomUi": True}}},
        {"setDataValidation": {"range": {**body, "startColumnIndex": idx["Approved amount (RWF)"],
                                         "endColumnIndex": idx["Approved amount (RWF)"] + 1},
                               "rule": {"condition": {"type": "NUMBER_GREATER_THAN_EQ", "values": [
                                   {"userEnteredValue": "0"}]}, "strict": True,
                                   "inputMessage": "Amount in RWF"}}},
        {"setDataValidation": {"range": {**body, "startColumnIndex": idx["Decision date"],
                                         "endColumnIndex": idx["Decision date"] + 1},
                               "rule": {"condition": {"type": "DATE_IS_VALID"}, "strict": True}}},
    ]
    for text, fg, bg in (("Approved", GOOD, GOOD_BG), ("Declined", BAD, BAD_BG),
                         ("Referred", WARN, WARN_BG), ("Pending", MUTED, HEAD_BG)):
        ops.append(_text_rule(gid, idx["Underwriter decision"], text, fg, bg))
    for text, fg, bg in (("Eligible", GOOD, GOOD_BG), ("Not eligible", BAD, BAD_BG),
                         ("Not assessed", WARN, WARN_BG)):
        ops.append(_text_rule(gid, idx["Eligibility"], text, fg, bg))
    for text, fg, bg in (("Proceed", GOOD, GOOD_BG), ("Refer", WARN, WARN_BG),
                         ("Decline", BAD, BAD_BG), ("Block", BAD, BAD_BG)):
        ops.append(_text_rule(gid, idx["Bureau outcome"], text, fg, bg))
    ops.append(_text_rule(gid, idx["Identity check"], "Mismatch", BAD, BAD_BG))
    return ops


def _append(gid: int, cols: list[Col], rows: list[list[Any]]) -> dict:
    """Append rows that carry their column's format and validation with them.

    Formats set on the tab at creation only reach the rows that existed then;
    a row appended past them would otherwise show raw serials and lose the
    decision dropdown.
    """
    validation = {
        "Underwriter decision": {"condition": {"type": "ONE_OF_LIST", "values": [
            {"userEnteredValue": d} for d in DECISIONS]}, "strict": True, "showCustomUi": True},
        "Approved amount (RWF)": {"condition": {"type": "NUMBER_GREATER_THAN_EQ", "values": [
            {"userEnteredValue": "0"}]}, "strict": True},
        "Decision date": {"condition": {"type": "DATE_IS_VALID"}, "strict": True},
    }

    def cell(c: Col, v: Any) -> dict:
        v = _cell(v)
        out: dict = {"userEnteredValue": _value(v)} if v not in ("", None) else {}
        fmt: dict = {"verticalAlignment": "TOP"}
        if c.kind in NUMBER_FORMATS:
            fmt["numberFormat"] = NUMBER_FORMATS[c.kind]
        if c.human:
            fmt["backgroundColor"] = _rgb("#FFFBEA")
        if c.width >= 300:
            fmt["wrapStrategy"] = "WRAP"
        out["userEnteredFormat"] = fmt
        if c.header in validation and c.human:
            out["dataValidation"] = validation[c.header]
        return out

    return {"appendCells": {
        "sheetId": gid,
        "fields": "userEnteredValue,dataValidation,userEnteredFormat(numberFormat,backgroundColor,"
                  "wrapStrategy,verticalAlignment)",
        "rows": [{"values": [cell(c, v) for c, v in zip(cols, r)]} for r in rows]}}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
_session = None
_session_lock = threading.Lock()
# Saves are serialised within a worker so two of them never race on creating
# the same tab. Across workers, a lost race on addSheet is detected and retried.
_write_lock = threading.Lock()


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            try:
                from google.auth.transport.requests import AuthorizedSession
                from google.oauth2 import service_account
            except ImportError as exc:  # pragma: no cover - dependency missing
                raise SheetsError("google-auth is not installed. Re-run setup.sh.") from exc
            try:
                creds = service_account.Credentials.from_service_account_file(KEY_FILE, scopes=SCOPES)
            except (OSError, ValueError) as exc:
                raise SheetsError(f"Cannot read the service account key at {KEY_FILE}: {exc}") from exc
            _session = AuthorizedSession(creds)
        return _session


def _call(session, method: str, url: str, **kw) -> dict:
    try:
        res = session.request(method, url, timeout=30, **kw)
    except Exception as exc:  # noqa: BLE001 - network failures all read the same to a user
        raise SheetsError(f"Could not reach Google Sheets: {exc}") from exc
    if res.status_code >= 400:
        try:
            msg = res.json().get("error", {}).get("message") or res.text
        except ValueError:
            msg = res.text
        if res.status_code in (403, 404):
            msg += (" — check GOOGLE_SHEETS_ID and that the spreadsheet is shared with the "
                    "service account as an Editor.")
        raise SheetsError(f"Google Sheets refused the write ({res.status_code}): {msg}")
    return res.json() if res.content else {}


def _tabs(session) -> dict[str, int]:
    meta = _call(session, "GET", f"{API}/{SHEET_ID}",
                 params={"fields": "sheets.properties(sheetId,title)"})
    return {s["properties"]["title"].casefold(): s["properties"]["sheetId"]
            for s in meta.get("sheets", [])}


def _batch(session, ops: list[dict]) -> dict:
    return _call(session, "POST", f"{API}/{SHEET_ID}:batchUpdate", json={"requests": ops})


def _ensure_data_tabs(session, tabs: dict[str, int]) -> None:
    missing = [n for n in TAB_IDS if n.casefold() not in tabs]
    if not missing:
        return
    cols = {REGISTER: REGISTER_COLS, **DETAIL_TABS}
    ops = [op for n in missing for op in _data_tab_setup(n, cols[n])]
    try:
        _batch(session, ops)
    except SheetsError as exc:
        # Another worker created them first; that is fine.
        if "already exists" not in str(exc):
            raise
    tabs.update({n.casefold(): TAB_IDS[n] for n in missing})


def _customer_tab(session, tabs: dict[str, int], title: str) -> tuple[int, bool]:
    if title.casefold() in tabs:
        return tabs[title.casefold()], False
    try:
        res = _batch(session, [{"addSheet": {"properties": {"title": title}}}])
    except SheetsError as exc:
        if "already exists" not in str(exc):
            raise
        return _tabs(session)[title.casefold()], False
    gid = res["replies"][0]["addSheet"]["properties"]["sheetId"]
    _batch(session, [{"addProtectedRange": {"protectedRange": {
        "range": {"sheetId": gid},
        "description": "Generated by Ledger and replaced on re-assessment. Record decisions in the Register.",
        "warningOnly": True}}}])
    return gid, True


def _superseded(session, title: str) -> list[int]:
    """Register rows (0-based) still marked current for this customer."""
    cust, cur = _col_letter(CUSTOMER_COL), _col_letter(CURRENT_COL)
    res = _call(session, "GET", f"{API}/{SHEET_ID}/values:batchGet",
                params={"ranges": [f"'{REGISTER}'!{cust}2:{cust}", f"'{REGISTER}'!{cur}2:{cur}"],
                        "valueRenderOption": "FORMATTED_VALUE"})
    ranges = res.get("valueRanges") or [{}, {}]
    names = [r[0] if r else "" for r in ranges[0].get("values", [])]
    current = [r[0] if r else "" for r in ranges[1].get("values", [])]
    key = title.casefold()
    return [i + 1 for i, n in enumerate(names)
            if n.casefold() == key and (current[i] if i < len(current) else "") == "yes"]


def new_assessment_id(at: datetime) -> str:
    return f"A{at:%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"


def save(crb: Optional[dict], report: Optional[dict], files: Optional[dict] = None) -> dict:
    """Record one assessment: a Register row, its detail rows, and the customer tab."""
    if not configured():
        raise SheetsError("Google Sheets export is not configured on this server.")
    if not crb and not report:
        raise SheetsError("Nothing to save: no bureau result and no statement report.")

    at = datetime.now().replace(microsecond=0)
    aid = new_assessment_id(at)
    title = tab_title(crb, report)
    lay = build_layout(crb, report, files, aid, at)
    session = _get_session()

    with _write_lock:
        tabs = _tabs(session)
        _ensure_data_tabs(session, tabs)
        gid, created = _customer_tab(session, tabs, title)
        stale = _superseded(session, title)

        ops = _report_requests(gid, lay)
        ops.append({"updateSheetProperties": {
            "properties": {"sheetId": gid, "tabColor": _outcome_color(crb, report)},
            "fields": "tabColor"}})
        ops += [{"updateCells": {
            "range": {"sheetId": TAB_IDS[REGISTER], "startRowIndex": r, "endRowIndex": r + 1,
                      "startColumnIndex": CURRENT_COL, "endColumnIndex": CURRENT_COL + 1},
            "rows": [{"values": [{"userEnteredValue": {"stringValue": "no"}}]}],
            "fields": "userEnteredValue"}} for r in stale]
        ops.append(_append(TAB_IDS[REGISTER], REGISTER_COLS,
                           [register_row(aid, at, title, gid, crb, report)]))
        for name, rows in detail_rows(aid, title, crb, report).items():
            if rows:
                ops.append(_append(TAB_IDS[name], DETAIL_TABS[name], rows))
        _batch(session, ops)

    return {
        "assessment_id": aid,
        "tab": title,
        "created": created,
        "superseded": len(stale),
        "url": spreadsheet_url(gid),
        "register_url": spreadsheet_url(TAB_IDS[REGISTER]),
    }
