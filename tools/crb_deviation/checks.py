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
from dataclasses import dataclass
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


@dataclass
class Check:
    code: str
    result: str
    detail: str


def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").upper().replace("-", " ").split())


def is_write_off(a: CreditAccount) -> bool:
    text = _norm(a.account_status) + " | " + _norm(a.classification)
    return any(w in text for w in WRITE_OFF)


def is_npa(a: CreditAccount) -> bool:
    return bool(a.is_non_performing or _norm(a.classification) in NON_PERFORMING_CLASSES)


def _name(a: CreditAccount) -> str:
    return f"#{a.index} {a.account_type or a.trade_sector or 'account'} (listed {a.listing_date or '?'})"


def _rwf(x: float) -> str:
    return f"RWF {round(x):,}"


def _worst(results: List[str]) -> str:
    return max(results, key=_RANK.__getitem__, default=ALLOWED)


def d1_fraud_cheques(report: ParsedCreditReport, policy: Policy) -> Check:
    """Any fraud case, or a bounced cheque since the cutoff, is a reject (the live tool's rule)."""
    reasons = _adverse_records(report, policy)
    if reasons:
        return Check("D1", REJECT, "; ".join(reasons))
    return Check("D1", ALLOWED, "No fraud cases or bounced cheques.")


def d2_open_npa(report: ParsedCreditReport, cutoff: datetime) -> Check:
    """An open NPA listed since the cutoff with anything owing is a reject."""
    open_npa = [a for a in report.accounts if a.is_open and is_npa(a)]
    counted = [a for a in open_npa if _after_cutoff(a, cutoff)]
    owing = [a for a in counted if a.balance_amount > 0 or a.arrears_amount > 0]
    ignored = [a for a in open_npa if a not in counted]
    note = (" Ignored, listed before the cutoff: " + ", ".join(_name(a) for a in ignored) + "."
            if ignored else "")
    if owing:
        return Check("D2", REJECT, "Open NPA: " + "; ".join(
            f"{_name(a)} balance {_rwf(a.balance_amount)}, arrears {_rwf(a.arrears_amount)}"
            for a in owing) + "." + note)
    return Check("D2", ALLOWED, "No open NPA." + note)


def d4_write_off(report: ParsedCreditReport) -> Check:
    """Any write-off, paid or unpaid, any amount, any date, is a reject."""
    w = [a for a in report.accounts if is_write_off(a)]
    if w:
        return Check("D4", REJECT, "Write-off: " + "; ".join(
            f"{_name(a)} status {a.account_status or a.classification}" for a in w) + ".")
    return Check("D4", ALLOWED, "No write-offs.")


def d5_closed_npa(report: ParsedCreditReport, cutoff: datetime) -> Check:
    """A closed NPA is allowed only when it both matured and was cleared before the cutoff.

    Maturity is listing date + repayment term. The report carries no clearance
    date, so the last payment date stands in for it. A loan cleared before the
    cutoff is allowed even when its maturity cannot be worked out: a facility
    fully repaid by then cannot realistically still be running past it. Only a
    missing clearance date is Review.
    """
    closed = [a for a in report.accounts if not a.is_open and is_npa(a) and not is_write_off(a)]
    if not closed:
        return Check("D5", ALLOWED, "No closed NPA.")
    results, lines = [], []
    for a in closed:
        listed, cleared = _date(a.listing_date), _date(a.last_payment_date)
        term = a.repayment_term_months
        matured = listed + timedelta(days=term * AVG_MONTH_DAYS) if listed and term else None
        unknown = "maturity unknown" + (f" (terms {a.repayment_terms!r})" if a.repayment_terms
                                        else " (no repayment terms)")
        when = (f"matures {matured:%b %Y}" if matured else unknown) + ", " + \
               (f"cleared {cleared:%b %Y}" if cleared else "clearance unknown")
        if (matured and matured >= cutoff) or (cleared and cleared >= cutoff):
            results.append(REJECT)
        elif cleared:
            results.append(ALLOWED)
        else:
            results.append(REVIEW)
        lines.append(f"{_name(a)} {when}: {results[-1]}")
    return Check("D5", _worst(results), "Closed NPA: " + "; ".join(lines) + ".")


def d6_open_accounts(report: ParsedCreditReport, cutoff: datetime) -> Check:
    """Each open performing account listed since the cutoff, on days past due and arrears."""
    accounts = [a for a in report.accounts
                if a.is_open and not is_npa(a) and not is_write_off(a) and _after_cutoff(a, cutoff)]
    results, flagged = [], []
    for a in accounts:
        dpd, arrears = a.days_in_arrears, a.arrears_amount
        why = f"{dpd} days past due, arrears {_rwf(arrears)}"
        if not a.days_in_arrears_reported and arrears > 0:
            # Above both arrears limits it fails whatever the days; below, the days decide.
            over = arrears > max(D6_ALLOWED_MAX_ARREARS, D6_REFER_MAX_ARREARS)
            r, why = (REJECT if over else REVIEW), f"arrears {_rwf(arrears)}, days past due not printed"
        elif dpd <= D6_ALLOWED_MAX_DPD and arrears <= D6_ALLOWED_MAX_ARREARS:
            r = ALLOWED
        elif D6_ALLOWED_MAX_DPD < dpd <= D6_REFER_MAX_DPD and arrears <= D6_REFER_MAX_ARREARS:
            r = REFER
        else:
            r = REJECT
        results.append(r)
        if r != ALLOWED:
            flagged.append(f"{_name(a)} {why}: {r}")
    if not accounts:
        return Check("D6", ALLOWED, "No open performing accounts since the cutoff.")
    if not flagged:
        return Check("D6", ALLOWED, f"{len(accounts)} open account(s), all within "
                     f"{D6_ALLOWED_MAX_DPD} days and {_rwf(D6_ALLOWED_MAX_ARREARS)}.")
    return Check("D6", _worst(results), "; ".join(flagged) + ".")


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
            "overall": overall, "group": group}
