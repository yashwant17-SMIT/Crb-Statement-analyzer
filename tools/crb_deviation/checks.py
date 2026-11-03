"""
CRB deviation checks D1-D6, for a one-off back-test of the intake sheet.

The live tool rejects every band below DD (EE to JJ) at the bureau gate. Credit
has proposed deviation points: an EE-JJ applicant can be approved if every
point is within its Allowed limit. This module scores a parsed bureau report
against those points so the existing customers can be split into "rejected on
band, but would pass" and "approved on band, but would fail".

Nothing here is used by the live tool. It only imports the bureau parser and a
few helpers from backend/app so a report is read exactly as the tool reads it.

Each check is Allowed, Refer, Review or Reject. Refer goes to the credit
head; anything beyond Refer is a Reject. Review means the report lacks a date
or figure the rule needs. A customer passes only when every check is Allowed.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.config import Policy  # noqa: E402
from app.crb_analyzer import (  # noqa: E402
    NON_PERFORMING_CLASSES, _adverse_records, _after_cutoff, _date,
)
from app.crb_parser import BAND_LADDER, CreditAccount, ParsedCreditReport  # noqa: E402

ALLOWED, REFER, REVIEW, REJECT = "Allowed", "Refer", "Review", "Reject"
_RANK = {ALLOWED: 0, REFER: 1, REVIEW: 2, REJECT: 3}

# D6: performing / open accounts, per account. Beyond Refer is Reject.
D6_ALLOWED_MAX_DPD = 15            # Allowed: 0-15 days past due ...
D6_ALLOWED_MAX_ARREARS = 150_000   # ... with arrears at most this (RWF)
D6_REFER_MAX_DPD = 90              # Refer: 16-90 days past due ...
D6_REFER_MAX_ARREARS = 100_000     # ... with arrears at most this (RWF)

# D5: a closed NPA that ran or was cleared after the cutoff is Refer up to this
# loan size (principal, RWF), Reject above. Arrears can't be used: the bureau
# prints 0 arrears on every closed account.
D5_REFER_MAX_PRINCIPAL = 50_000

WRITE_OFF = ("WRITTEN OFF", "WRITE OFF", "CHARGED OFF", "CHARGE OFF")
AVG_MONTH_DAYS = 30.4375

# Groups, in the order the summary lists them.
COULD_APPROVE = "Rejected on band - could be approved"
REJECTED_REFER = "Rejected on band - refer to credit head"
REJECTED_REVIEW = "Rejected on band - needs a look"
STAYS_REJECTED = "Rejected on band - stays rejected"
WOULD_REJECT = "Approved on band - would be rejected"
APPROVED_REFER = "Approved on band - refer to credit head"
APPROVED_REVIEW = "Approved on band - needs a look"
NO_CHANGE = "Approved on band - no change"
NOT_ASSESSED = "Not assessed"
GROUPS = [COULD_APPROVE, REJECTED_REFER, REJECTED_REVIEW, STAYS_REJECTED,
          WOULD_REJECT, APPROVED_REFER, APPROVED_REVIEW, NO_CHANGE, NOT_ASSESSED]


TITLES = {"D1": "Fraud / bounced cheque", "D2": "Open NPA", "D4": "Write-off",
          "D5": "Closed NPA", "D6": "Overdue open loans"}


@dataclass
class Finding:
    """One account (or, for D1, one record) behind a check's result."""
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

    @property
    def detail(self) -> str:
        return " ".join([self.summary] + [f.text + "." for f in self.findings])


def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").upper().replace("-", " ").split())


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


def _worst(results: List[str]) -> str:
    return max(results, key=_RANK.__getitem__, default=ALLOWED)


def d1_fraud_cheques(report: ParsedCreditReport, policy: Policy) -> Check:
    """Any fraud case, or a bounced cheque since the cutoff, is a reject (the live tool's rule)."""
    reasons = _adverse_records(report, policy)
    if reasons:
        return Check("D1", REJECT, "Fraud or bounced cheques on file.",
                     [Finding(REJECT, r[0].upper() + r[1:]) for r in reasons])
    return Check("D1", ALLOWED, "No fraud cases or bounced cheques.")


def d2_open_npa(report: ParsedCreditReport, cutoff: datetime) -> Check:
    """An open NPA listed since the cutoff with anything owing is a reject."""
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
    result = _worst([f.result for f in findings])
    owing = sum(f.result == REJECT for f in findings)
    summary = (f"{owing} open non-performing loan(s) with money owing." if owing
               else "No open non-performing loan with money owing.")
    return Check("D2", result, summary, findings)


def d4_write_off(report: ParsedCreditReport) -> Check:
    """Any write-off, paid or unpaid, any amount, any date, is a reject."""
    findings = [Finding(REJECT, f"{account_name(a)}: written off (status {a.account_status or a.classification}, "
                                f"balance {_rwf(a.balance_amount)})", a)
                for a in report.accounts if is_write_off(a)]
    if findings:
        return Check("D4", REJECT, f"{len(findings)} written-off loan(s); any write-off fails.", findings)
    return Check("D4", ALLOWED, "No write-offs.")


def d5_closed_npa(report: ParsedCreditReport, cutoff: datetime) -> Check:
    """A closed NPA is allowed when it both matured and was cleared before the cutoff.

    Otherwise it is Refer up to D5_REFER_MAX_PRINCIPAL loan size, Reject above.

    Maturity is listing date + repayment term. The report carries no clearance
    date, so the last payment date stands in for it. A loan cleared before the
    cutoff is allowed even when its maturity cannot be worked out: a facility
    fully repaid by then cannot realistically still be running past it. Only a
    missing clearance date is Review.
    """
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
            r = REFER if a.principal_amount <= D5_REFER_MAX_PRINCIPAL else REJECT
            why = (f"{' and '.join(late)} on or after {day(cutoff)}, loan of {_rwf(a.principal_amount)} "
                   f"{'within' if r == REFER else 'above'} the Refer limit of {_rwf(D5_REFER_MAX_PRINCIPAL)}")
        elif cleared:
            r, why = ALLOWED, f"cleared before {day(cutoff)}"
        else:
            r, why = REVIEW, "can't tell when it was cleared"
        findings.append(Finding(r, f"{account_name(a)}: closed NPA, {end}, {paid}; {why}", a))
    if not findings:
        return Check("D5", ALLOWED, "No closed non-performing loans.")
    result = _worst([f.result for f in findings])
    summary = {REJECT: f"A closed NPA ran or was cleared after the cutoff on a loan above "
                       f"{_rwf(D5_REFER_MAX_PRINCIPAL)}.",
               REFER: f"A closed NPA ran or was cleared after the cutoff on a loan of up to "
                      f"{_rwf(D5_REFER_MAX_PRINCIPAL)}.",
               REVIEW: "A closed NPA has no last payment date, so the clearance date is unknown.",
               ALLOWED: "Every closed NPA was cleared before the cutoff."}[result]
    return Check("D5", result, summary, findings)


def d6_open_accounts(report: ParsedCreditReport, cutoff: datetime) -> Check:
    """Each open performing account listed since the cutoff, on days past due and arrears."""
    accounts = [a for a in report.accounts
                if a.is_open and not is_npa(a) and not is_write_off(a) and _after_cutoff(a, cutoff)]
    findings = []
    for a in accounts:
        dpd, arrears = a.days_in_arrears, a.arrears_amount
        what = f"{account_name(a)}: {dpd} days overdue, arrears {_rwf(arrears)}"
        if not a.days_in_arrears_reported and arrears > 0:
            # Above both arrears limits it fails whatever the days; below, the days decide.
            over = arrears > max(D6_ALLOWED_MAX_ARREARS, D6_REFER_MAX_ARREARS)
            what = f"{account_name(a)}: arrears {_rwf(arrears)}, days overdue not printed"
            r, why = (REJECT, "arrears above every limit") if over else (REVIEW, "can't tell without the days overdue")
        elif dpd <= D6_ALLOWED_MAX_DPD and arrears <= D6_ALLOWED_MAX_ARREARS:
            continue
        elif D6_ALLOWED_MAX_DPD < dpd <= D6_REFER_MAX_DPD and arrears <= D6_REFER_MAX_ARREARS:
            r, why = REFER, (f"within the Refer limit ({D6_ALLOWED_MAX_DPD + 1}-{D6_REFER_MAX_DPD} days, "
                             f"up to {_rwf(D6_REFER_MAX_ARREARS)})")
        else:
            r, why = REJECT, (f"beyond the Refer limit ({D6_ALLOWED_MAX_DPD + 1}-{D6_REFER_MAX_DPD} days, "
                              f"up to {_rwf(D6_REFER_MAX_ARREARS)})")
        findings.append(Finding(r, f"{what}; {why}", a))
    if not accounts:
        return Check("D6", ALLOWED, f"No open performing loans listed since {day(cutoff)}.")
    if not findings:
        return Check("D6", ALLOWED, f"{len(accounts)} open loan(s), none more than {D6_ALLOWED_MAX_DPD} days "
                     f"overdue or above {_rwf(D6_ALLOWED_MAX_ARREARS)} in arrears.")
    return Check("D6", _worst([f.result for f in findings]),
                 f"{len(findings)} of {len(accounts)} open loan(s) overdue beyond the Allowed limit.", findings)


def run_checks(report: ParsedCreditReport, policy: Optional[Policy] = None) -> List[Check]:
    policy = policy or Policy()
    cutoff = datetime.strptime(policy.crb_history_cutoff, "%Y-%m-%d")
    return [
        d1_fraud_cheques(report, policy),
        d2_open_npa(report, cutoff),
        d4_write_off(report),
        d5_closed_npa(report, cutoff),
        d6_open_accounts(report, cutoff),
    ]


def band_rejected(band: Optional[str], policy: Policy) -> Optional[bool]:
    """True below the minimum band, False at or above it, None when unreadable."""
    if band not in BAND_LADDER:
        return None
    return BAND_LADDER.index(band) > BAND_LADDER.index(policy.min_band)


def evaluate(report: ParsedCreditReport, policy: Optional[Policy] = None) -> Dict:
    """The band decision, each check, the overall deviation result and the group."""
    policy = policy or Policy()
    checks = run_checks(report, policy)
    overall = _worst([c.result for c in checks])
    rejected = band_rejected(report.band, policy)
    if rejected is None:
        group, band_rule = NOT_ASSESSED, "No band"
    elif rejected:
        band_rule = "Rejected"
        group = {ALLOWED: COULD_APPROVE, REFER: REJECTED_REFER, REVIEW: REJECTED_REVIEW,
                 REJECT: STAYS_REJECTED}[overall]
    else:
        band_rule = "Approved"
        group = {ALLOWED: NO_CHANGE, REFER: APPROVED_REFER, REVIEW: APPROVED_REVIEW,
                 REJECT: WOULD_REJECT}[overall]
    return {"band": report.band, "band_rule": band_rule, "checks": checks,
            "overall": overall, "group": group, "report": report}
