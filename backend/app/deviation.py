"""
CRB deviation points D1-D5, the first step of the bureau gate.

Credit policy: an applicant may proceed only when every point is within its
Allowed limit. A Refer goes to the approving authority (the statement is
still assessed so the sanction is known); anything beyond Refer is a reject.

Each point is Allowed, Refer, Review or Reject. Review means the report lacks
a date or figure the rule needs, so a person must look; it is handled like a
Refer. The overall result is the worst point. Every threshold is a Policy
field, so the conditions can change without touching this file. Amounts are
arrears unless stated; only loans listed since the cutoff count, except D4.

    D1  Fraud / bounced cheque   any fraud case, or a bounced cheque since the cutoff: Reject
    D2  Open performing loans    per loan, days past due and arrears:
                                 up to 15 days and <= 150,000, or 16-90 days and <= 50,000: Allowed;
                                 16-90 days and <= 75,000: Refer; anything beyond: Reject
    D3  Open NPA                 open and non-performing, or more than 90 days past due:
                                 arrears <= 75,000: Allowed; above: Reject
    D4  Write-off                listed before the cutoff: Allowed, flagged for review unless
                                 at least 10 loans are current and within terms since;
                                 listed since the cutoff: arrears > 75,000 Reject, otherwise Refer
    D5  Closed NPA               term and clearance both ended before 1 Jan 2026: Allowed;
                                 otherwise principal <= 75,000: Allowed; above: Reject

A tax debt is not a loan: an account in the Taxation trade sector (tax
arrears the revenue authority lists with the bureau) is left out of every
point and only reported.

A write-off takes precedence: a written-off loan that is also an open or
closed NPA is judged under D4 only, with its NPA details listed beneath it.
The report has no clearance date; D5 takes the last payment date for it and
the listing date plus the repayment term for the term's end, taking 30 days
for a mobile loan that prints no terms (see _ended). D5's date is its own
(deviation_d5_cleared_by), not the cutoff.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .config import Policy
from .crb_parser import CreditAccount, ParsedCreditReport

ALLOWED, REFER, REVIEW, REJECT = "allowed", "refer", "review", "reject"
RANK = {ALLOWED: 0, REFER: 1, REVIEW: 2, REJECT: 3}
TITLES = {"D1": "Fraud / bounced cheque", "D2": "Open performing loans", "D3": "Open NPA",
          "D4": "Write-off", "D5": "Closed NPA"}

NON_PERFORMING_CLASSES = {"LOSS", "DOUBTFUL", "SUBSTANDARD", "NPA", "NON PERFORMING"}
WRITE_OFF = ("WRITTEN OFF", "WRITE OFF", "CHARGED OFF", "CHARGE OFF")
AVG_MONTH_DAYS = 30.4375


@dataclass
class Finding:
    """One account (or, for D1, one record) behind a point's result."""
    result: str
    text: str
    account: Optional[CreditAccount] = None
    details: List[str] = field(default_factory=list)


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
                "findings": [{"result": f.result, "text": f.text, "details": f.details,
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


def is_tax(a: CreditAccount) -> bool:
    """A tax debt the revenue authority listed with the bureau, not a loan."""
    return "TAXATION" in _norm(a.trade_sector)


def is_npa(a: CreditAccount) -> bool:
    return bool(a.is_non_performing or _norm(a.classification) in NON_PERFORMING_CLASSES)


def is_open_npa(a: CreditAccount, policy: Policy) -> bool:
    """Open and classed non-performing, or past the D2 limit of days overdue."""
    return a.is_open and (is_npa(a) or a.days_in_arrears > policy.deviation_d2_refer_max_dpd)


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


def d2_open_performing(report: ParsedCreditReport, cutoff: datetime, policy: Policy) -> Check:
    ok_dpd, ok_arr = policy.deviation_d2_allowed_max_dpd, policy.deviation_d2_allowed_max_arrears
    late_ok_arr = policy.deviation_d2_late_allowed_max_arrears
    ref_dpd, ref_arr = policy.deviation_d2_refer_max_dpd, policy.deviation_d2_refer_max_arrears
    late = f"{ok_dpd + 1}-{ref_dpd} days"
    accounts = [a for a in report.accounts if a.is_open and not is_open_npa(a, policy)
                and not is_write_off(a) and _after_cutoff(a, cutoff)]
    findings = []
    for a in accounts:
        dpd, arrears = a.days_in_arrears, a.arrears_amount
        what = f"{account_name(a)}: {dpd} days overdue, arrears {_rwf(arrears)}"
        if not a.days_in_arrears_reported and arrears > 0:
            # Within the late Allowed limit it passes whatever the days; above
            # every limit it fails whatever the days; between, the days decide.
            what = f"{account_name(a)}: arrears {_rwf(arrears)}, days overdue not printed"
            if arrears <= min(ok_arr, late_ok_arr):
                continue
            r, why = ((REJECT, "arrears above every limit") if arrears > max(ok_arr, ref_arr)
                      else (REVIEW, "can't tell without the days overdue"))
        elif dpd <= ok_dpd:
            if arrears <= ok_arr:
                continue
            r, why = REJECT, f"above the {_rwf(ok_arr)} limit for up to {ok_dpd} days"
        elif arrears <= late_ok_arr:
            continue
        elif arrears <= ref_arr:
            r, why = REFER, f"{late} overdue, within the Refer limit of {_rwf(ref_arr)}"
        else:
            r, why = REJECT, f"{late} overdue, above the Refer limit of {_rwf(ref_arr)}"
        findings.append(Finding(r, f"{what}; {why}", a))
    if not accounts:
        return Check("D2", ALLOWED, f"No open performing loans listed since {day(cutoff)}.")
    if not findings:
        return Check("D2", ALLOWED, f"{len(accounts)} open loan(s), all within the Allowed limits.")
    return Check("D2", worst([f.result for f in findings]),
                 f"{len(findings)} of {len(accounts)} open loan(s) overdue beyond the Allowed limits.", findings)


def d3_open_npa(report: ParsedCreditReport, cutoff: datetime, policy: Policy) -> Check:
    limit = policy.deviation_d3_allowed_max_arrears
    findings = []
    for a in report.accounts:
        if not is_open_npa(a, policy) or is_write_off(a):
            continue
        what = (f"{account_name(a)}: still open and non-performing "
                f"({a.classification or f'{a.days_in_arrears} days overdue'}), arrears {_rwf(a.arrears_amount)}")
        if not _after_cutoff(a, cutoff):
            findings.append(Finding(ALLOWED, f"{what}; not counted, listed before {day(cutoff)}", a))
        elif a.arrears_amount > limit:
            findings.append(Finding(REJECT, f"{what}; above the {_rwf(limit)} limit", a))
        else:
            findings.append(Finding(ALLOWED, f"{what}; within the {_rwf(limit)} limit", a))
    rejected = sum(f.result == REJECT for f in findings)
    return Check("D3", worst([f.result for f in findings]),
                 f"{rejected} open non-performing loan(s) with arrears above {_rwf(limit)}." if rejected
                 else "No open non-performing loan above the limit.", findings)


def _npa_details(a: CreditAccount, policy: Policy) -> List[str]:
    """What an NPA check would have said about a written-off loan."""
    if not (is_npa(a) or is_open_npa(a, policy)):
        return []
    state = "an open" if a.is_open else "a closed"
    return [f"Also {state} NPA ({a.classification or 'non-performing'}): principal {_rwf(a.principal_amount)}, "
            f"balance {_rwf(a.balance_amount)}, arrears {_rwf(a.arrears_amount)}, "
            f"{a.days_in_arrears} days overdue, last payment {day(a.last_payment_date)}"]


def d4_write_off(report: ParsedCreditReport, cutoff: datetime, policy: Policy) -> Check:
    limit, need = policy.deviation_d4_reject_min_arrears, policy.deviation_d4_min_good_loans
    good = sum(1 for a in report.accounts
               if _after_cutoff(a, cutoff) and not is_npa(a) and not is_write_off(a)
               and a.days_in_arrears == 0 and a.arrears_amount == 0)
    findings = []
    for a in report.accounts:
        if not is_write_off(a):
            continue
        what = (f"{account_name(a)}: written off (status {a.account_status or a.classification}, "
                f"arrears {_rwf(a.arrears_amount)})")
        if not _after_cutoff(a, cutoff):
            # Fewer good loans than policy asks for is flagged for review, not held, for now.
            r = ALLOWED
            why = (f"before {day(cutoff)}, {good} loan(s) current and within terms since "
                   + (f"(at least {need})" if good >= need else f"(fewer than {need}); flagged for review"))
        elif a.arrears_amount > limit:
            r, why = REJECT, f"since {day(cutoff)}, arrears above {_rwf(limit)}"
        else:
            r, why = REFER, f"since {day(cutoff)}, arrears within {_rwf(limit)}"
        findings.append(Finding(r, f"{what}; {why}", a, _npa_details(a, policy)))
    if not findings:
        return Check("D4", ALLOWED, "No write-offs.")
    result = worst([f.result for f in findings])
    summary = {REJECT: f"A write-off since the cutoff with arrears above {_rwf(limit)}.",
               REFER: "A write-off needs the approving authority.",
               ALLOWED: f"Every write-off predates the cutoff, with {good} good loan(s) since"
                        + (f"; fewer than {need}, flagged for review." if good < need else ".")}[result]
    return Check("D4", result, summary, findings)


def d4_flagged(check: Check) -> bool:
    """A write-off before the cutoff without enough good loans since: reported, not held."""
    return check.code == "D4" and any("flagged for review" in f.text for f in check.findings)


def _ended(a: CreditAccount, by: datetime, mobile_loan_days: int) -> tuple:
    """(ended on or after `by`?, how that is known) for a closed NPA; None when nothing says.

    Both the closure date as per the repayment term (listing date + term) and
    the clearance date must fall before `by`. The report prints no clearance
    date: the last payment date stands in for it. A mobile loan often prints
    no terms at all: it is taken to run mobile_loan_days from listing. Either
    date on or after `by` settles it; one date before it, with the other not
    on the report, counts as before. A loan listed on or after `by` with
    neither ended after it (it can't end before it is listed).
    """
    listed, paid = _date(a.listing_date), _date(a.last_payment_date)
    term = a.repayment_term_months
    if listed and term:
        matured, how = listed + timedelta(days=term * AVG_MONTH_DAYS), "term ended"
    elif listed and not a.repayment_terms and "MOBILE LOAN" in a.account_type.upper():
        matured, how = listed + timedelta(days=mobile_loan_days), f"a mobile loan taken as {mobile_loan_days} days, so ended"
    else:
        matured, how = None, ""
    dates = [d for d in (paid, matured) if d]
    basis = ", ".join(filter(None, [f"last payment {day(paid)}" if paid else "no last payment date",
                                    f"{how} {day(matured)}" if matured else None]))
    if dates:
        return max(dates) >= by, basis
    if listed and listed >= by:
        return True, f"{basis}, no term, listed {day(listed)}"
    terms = f"terms printed as '{a.repayment_terms}'" if a.repayment_terms else "no repayment terms printed"
    return None, f"{basis}, term end unknown ({terms})"


def d5_closed_npa(report: ParsedCreditReport, policy: Policy) -> Check:
    """Term and clearance both ended before deviation_d5_cleared_by: Allowed, whatever the size.

    Otherwise a loan (principal) of up to d5_allowed_max_principal is Allowed
    and a larger one is Reject. (Principal, not arrears: the bureau prints 0
    arrears on every closed account.) When the dates can't be worked out
    (see _ended), a small loan is Allowed either way and a larger one is Review.
    """
    limit = policy.deviation_d5_allowed_max_principal
    by = datetime.strptime(policy.deviation_d5_cleared_by, "%Y-%m-%d")
    findings = []
    for a in report.accounts:
        if a.is_open or not is_npa(a) or is_write_off(a):
            continue
        late, basis = _ended(a, by, policy.deviation_d5_mobile_loan_days)
        principal = a.principal_amount
        if late is False:
            r, why = ALLOWED, f"term and clearance ended before {day(by)}"
        elif principal <= limit:
            r, why = ALLOWED, f"loan of {_rwf(principal)}, within {_rwf(limit)}"
        elif late is None:
            r, why = REVIEW, f"loan of {_rwf(principal)} above {_rwf(limit)}; can't tell when it ended"
        else:
            r, why = REJECT, f"ended on or after {day(by)}, loan of {_rwf(principal)} above {_rwf(limit)}"
        findings.append(Finding(r, f"{account_name(a)}: closed NPA, {basis}; {why}", a))
    if not findings:
        return Check("D5", ALLOWED, "No closed non-performing loans.")
    result = worst([f.result for f in findings])
    summary = {REJECT: f"A closed NPA ending on or after {day(by)} is a loan above {_rwf(limit)}.",
               REVIEW: f"A closed NPA above {_rwf(limit)} can't be dated against {day(by)} from the report.",
               ALLOWED: f"Every closed NPA ended before {day(by)} or is a loan of up to {_rwf(limit)}."}[result]
    return Check("D5", result, summary, findings)


def run_checks(report: ParsedCreditReport, policy: Policy, adverse: List[str]) -> List[Check]:
    cutoff = datetime.strptime(policy.crb_history_cutoff, "%Y-%m-%d")
    report = replace(report, accounts=[a for a in report.accounts if not is_tax(a)])
    return [d1_fraud_cheques(adverse), d2_open_performing(report, cutoff, policy),
            d3_open_npa(report, cutoff, policy), d4_write_off(report, cutoff, policy),
            d5_closed_npa(report, policy)]


def assess(report: ParsedCreditReport, policy: Policy, adverse: List[str]) -> Dict[str, Any]:
    """The overall result, a one-line reason, and every point for the report."""
    checks = run_checks(report, policy, adverse)
    result = worst([c.result for c in checks])
    bad = [c for c in checks if c.result != ALLOWED]
    detail = "; ".join(f"{c.code} {c.title}: {c.result.upper()} – " +
                       " ".join([c.summary] + [" ".join([f.text + "."] + [d + "." for d in f.details])
                                               for f in c.findings if f.result != ALLOWED])
                       for c in sorted(bad, key=lambda c: -RANK[c.result]))
    return {"result": result, "detail": detail, "checks": [c.as_dict() for c in checks]}
