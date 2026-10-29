"""
CRB deviation points D1-D6 for applicants below the minimum band (EE-JJ).

Credit policy: an EE-JJ applicant may proceed only when every point is within
its Allowed limit. A Refer goes to the credit head (the statement is still
assessed so the sanction is known); anything beyond Refer is a reject.

Each point is Allowed, Refer, Review or Reject. Review means the report lacks
a date or figure the rule needs, so a person must look; it is handled like a
Refer. The overall result is the worst point. Every threshold is a Policy
field, so the conditions can change without touching this file.

    D1  Fraud / bounced cheque   any fraud case, or a bounced cheque since the cutoff: Reject
    D2  Open NPA                 any open NPA listed since the cutoff with anything owing: Reject
    D4  Write-off                any write-off, paid or unpaid, any amount, any date: Reject
    D5  Closed NPA               matured and cleared before the cutoff: Allowed;
                                 otherwise principal <= d5_refer_max_principal: Refer, above: Reject
    D6  Open performing loans    per loan listed since the cutoff, days past due and arrears:
                                 0-15 days and <= 150,000: Allowed; 16-90 days and <= 100,000:
                                 Refer; anything beyond: Reject

D3 is not in the policy. The report has no clearance date, so D5 uses the
last payment date in its place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .config import Policy
from .crb_parser import CreditAccount, ParsedCreditReport

ALLOWED, REFER, REVIEW, REJECT = "allowed", "refer", "review", "reject"
RANK = {ALLOWED: 0, REFER: 1, REVIEW: 2, REJECT: 3}
TITLES = {"D1": "Fraud / bounced cheque", "D2": "Open NPA", "D4": "Write-off",
          "D5": "Closed NPA", "D6": "Overdue open loans"}

NON_PERFORMING_CLASSES = {"LOSS", "DOUBTFUL", "SUBSTANDARD", "NPA", "NON PERFORMING"}
WRITE_OFF = ("WRITTEN OFF", "WRITE OFF", "CHARGED OFF", "CHARGE OFF")
AVG_MONTH_DAYS = 30.4375


@dataclass
class Finding:
    """One account (or, for D1, one record) behind a point's result."""
    result: str
    text: str
    account: Optional[CreditAccount] = None


@dataclass
class Check:
    code: str
    result: str
    summary: str
    findings: List[Finding] = field(default_factory=list)

    @property
    def title(self) -> str:
        return TITLES[self.code]

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "title": self.title, "result": self.result, "summary": self.summary,
                "findings": [{"result": f.result, "text": f.text,
                              "account": f.account.index if f.account else None} for f in self.findings]}


def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").upper().replace("-", " ").split())


def _date(value: Optional[str]) -> Optional[datetime]:
    for fmt in ("%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt) if value else None
        except (ValueError, TypeError):
            continue
    return None


def _after_cutoff(a: CreditAccount, cutoff: datetime) -> bool:
    """True unless the listing date is provably before the cutoff (as crb_analyzer)."""
    d = _date(a.listing_date)
    return d is None or d >= cutoff


def is_write_off(a: CreditAccount) -> bool:
    text = _norm(a.account_status) + " | " + _norm(a.classification)
    return any(w in text for w in WRITE_OFF)


def is_npa(a: CreditAccount) -> bool:
    return bool(a.is_non_performing or _norm(a.classification) in NON_PERFORMING_CLASSES)


def day(value) -> str:
    """'11-Feb-2026' or a datetime as '11 Feb 2026'; '?' when missing."""
    d = _date(value) if isinstance(value, str) or value is None else value
    return f"{d.day} {d:%b %Y}" if d else "?"


def account_name(a: CreditAccount) -> str:
    kind = a.account_type or "loan"
    sector = f" ({a.trade_sector})" if a.trade_sector and a.trade_sector.upper() != kind.upper() else ""
    return f"Account #{a.index} {kind}{sector}, listed {day(a.listing_date)}"


def _rwf(x: float) -> str:
    return f"RWF {round(x):,}"


def worst(results: List[str]) -> str:
    return max(results, key=RANK.__getitem__, default=ALLOWED)


def d1_fraud_cheques(adverse: List[str]) -> Check:
    """Any fraud case, or a bounced cheque since the cutoff (crb_analyzer's adverse records)."""
    if adverse:
        return Check("D1", REJECT, "Fraud or bounced cheques on file.",
                     [Finding(REJECT, r[0].upper() + r[1:]) for r in adverse])
    return Check("D1", ALLOWED, "No fraud cases or bounced cheques.")


def d2_open_npa(report: ParsedCreditReport, cutoff: datetime) -> Check:
    findings = []
    for a in report.accounts:
        if not (a.is_open and is_npa(a)):
            continue
        what = (f"{account_name(a)}: still open and non-performing ({a.classification or 'NPA'}), "
                f"balance {_rwf(a.balance_amount)}, arrears {_rwf(a.arrears_amount)}")
        if not _after_cutoff(a, cutoff):
            findings.append(Finding(ALLOWED, f"{what}; not counted, listed before {day(cutoff)}", a))
        elif a.balance_amount > 0 or a.arrears_amount > 0:
            findings.append(Finding(REJECT, what, a))
        else:
            findings.append(Finding(ALLOWED, f"{what}; nothing owing", a))
    owing = sum(f.result == REJECT for f in findings)
    return Check("D2", worst([f.result for f in findings]),
                 f"{owing} open non-performing loan(s) with money owing." if owing
                 else "No open non-performing loan with money owing.", findings)


def d4_write_off(report: ParsedCreditReport) -> Check:
    findings = [Finding(REJECT, f"{account_name(a)}: written off (status {a.account_status or a.classification}, "
                                f"balance {_rwf(a.balance_amount)})", a)
                for a in report.accounts if is_write_off(a)]
    if findings:
        return Check("D4", REJECT, f"{len(findings)} written-off loan(s); any write-off fails.", findings)
    return Check("D4", ALLOWED, "No write-offs.")


def d5_closed_npa(report: ParsedCreditReport, cutoff: datetime, policy: Policy) -> Check:
    """Matured (listing date + term) and cleared before the cutoff is Allowed.

    Otherwise the loan's principal decides: up to d5_refer_max_principal is
    Refer, above it Reject. (Not arrears: the bureau prints 0 arrears on every
    closed account.) A loan cleared before
    the cutoff is Allowed even when its term can't be worked out; a missing
    clearance (last payment) date is Review.
    """
    limit = policy.deviation_d5_refer_max_principal
    findings = []
    for a in report.accounts:
        if a.is_open or not is_npa(a) or is_write_off(a):
            continue
        listed, cleared = _date(a.listing_date), _date(a.last_payment_date)
        term = a.repayment_term_months
        matured = listed + timedelta(days=term * AVG_MONTH_DAYS) if listed and term else None
        end = (f"term ended {matured:%b %Y}" if matured else
               "term end unknown" + (f" (terms printed as '{a.repayment_terms}')" if a.repayment_terms
                                     else " (no repayment terms printed)"))
        paid = f"last payment {cleared:%b %Y}" if cleared else "no last payment date"
        late = [w for w, d in (("term ended", matured), ("last payment", cleared)) if d and d >= cutoff]
        if late:
            principal = a.principal_amount
            r = REFER if principal <= limit else REJECT
            why = (f"{' and '.join(late)} on or after {day(cutoff)}, loan of {_rwf(principal)} "
                   f"{'within' if r == REFER else 'above'} the Refer limit of {_rwf(limit)}")
        elif cleared:
            r, why = ALLOWED, f"cleared before {day(cutoff)}"
        else:
            r, why = REVIEW, "can't tell when it was cleared"
        findings.append(Finding(r, f"{account_name(a)}: closed NPA, {end}, {paid}; {why}", a))
    if not findings:
        return Check("D5", ALLOWED, "No closed non-performing loans.")
    result = worst([f.result for f in findings])
    summary = {REJECT: f"A closed NPA ran or was cleared after the cutoff on a loan above {_rwf(limit)}.",
               REVIEW: "A closed NPA has no last payment date, so the clearance date is unknown.",
               REFER: f"A closed NPA ran or was cleared after the cutoff on a loan of up to {_rwf(limit)}.",
               ALLOWED: "Every closed NPA matured and was cleared before the cutoff."}[result]
    return Check("D5", result, summary, findings)


def d6_open_accounts(report: ParsedCreditReport, cutoff: datetime, policy: Policy) -> Check:
    ok_dpd, ok_arr = policy.deviation_d6_allowed_max_dpd, policy.deviation_d6_allowed_max_arrears
    ref_dpd, ref_arr = policy.deviation_d6_refer_max_dpd, policy.deviation_d6_refer_max_arrears
    limit = f"{ok_dpd + 1}-{ref_dpd} days, up to {_rwf(ref_arr)}"
    accounts = [a for a in report.accounts
                if a.is_open and not is_npa(a) and not is_write_off(a) and _after_cutoff(a, cutoff)]
    findings = []
    for a in accounts:
        dpd, arrears = a.days_in_arrears, a.arrears_amount
        what = f"{account_name(a)}: {dpd} days overdue, arrears {_rwf(arrears)}"
        if not a.days_in_arrears_reported and arrears > 0:
            # Above both arrears limits it fails whatever the days; below, the days decide.
            what = f"{account_name(a)}: arrears {_rwf(arrears)}, days overdue not printed"
            r, why = ((REJECT, "arrears above every limit") if arrears > max(ok_arr, ref_arr)
                      else (REVIEW, "can't tell without the days overdue"))
        elif dpd <= ok_dpd and arrears <= ok_arr:
            continue
        elif ok_dpd < dpd <= ref_dpd and arrears <= ref_arr:
            r, why = REFER, f"within the Refer limit ({limit})"
        else:
            r, why = REJECT, f"beyond the Refer limit ({limit})"
        findings.append(Finding(r, f"{what}; {why}", a))
    if not accounts:
        return Check("D6", ALLOWED, f"No open performing loans listed since {day(cutoff)}.")
    if not findings:
        return Check("D6", ALLOWED, f"{len(accounts)} open loan(s), none more than {ok_dpd} days "
                     f"overdue or above {_rwf(ok_arr)} in arrears.")
    return Check("D6", worst([f.result for f in findings]),
                 f"{len(findings)} of {len(accounts)} open loan(s) overdue beyond the Allowed limit.", findings)


def run_checks(report: ParsedCreditReport, policy: Policy, adverse: List[str]) -> List[Check]:
    cutoff = datetime.strptime(policy.crb_history_cutoff, "%Y-%m-%d")
    return [d1_fraud_cheques(adverse), d2_open_npa(report, cutoff), d4_write_off(report),
            d5_closed_npa(report, cutoff, policy), d6_open_accounts(report, cutoff, policy)]


def assess(report: ParsedCreditReport, policy: Policy, adverse: List[str]) -> Dict[str, Any]:
    """The overall result, a one-line reason, and every point for the report."""
    checks = run_checks(report, policy, adverse)
    result = worst([c.result for c in checks])
    bad = [c for c in checks if c.result != ALLOWED]
    detail = "; ".join(f"{c.code} {c.title}: {c.result.upper()} – " +
                       " ".join([c.summary] + [f.text + "." for f in c.findings if f.result != ALLOWED])
                       for c in sorted(bad, key=lambda c: -RANK[c.result]))
    return {"result": result, "detail": detail, "checks": [c.as_dict() for c in checks]}
