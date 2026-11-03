"""
CRB deviation checks D1-D5, for a one-off back-test of the intake sheet.

The rules are the live tool's own (backend/app/deviation.py), with the same
Policy limits, so the back-test shows exactly what the live gate would do.
This module only decides how each customer compares with the band rule:

  - rejected on band (EE-JJ) today, but every point is Allowed
  - approved on band (AA-DD) today, but a point is a Reject
  - no band, score or loans on the report: approved with a note, as live

Each check is Allowed, Refer, Review or Reject. Refer goes to the approving
authority; anything beyond Refer is a Reject. Review means the report lacks
a date or figure the rule needs. A customer passes only when every check is Allowed.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.config import Policy  # noqa: E402
from app.crb_analyzer import _adverse_records  # noqa: E402
from app.crb_parser import BAND_LADDER, ParsedCreditReport  # noqa: E402
from app.deviation import (  # noqa: E402,F401  (re-exported for report.py)
    ALLOWED, RANK as _RANK, REFER, REJECT, REVIEW, TITLES, Check, _after_cutoff, day, run_checks as _run,
    worst as _worst,
)

# Groups, in the order the summary lists them.
COULD_APPROVE = "Rejected on band - could be approved"
REJECTED_REFER = "Rejected on band - refer to approving authority"
REJECTED_REVIEW = "Rejected on band - needs a look"
STAYS_REJECTED = "Rejected on band - stays rejected"
WOULD_REJECT = "Approved on band - would be rejected"
APPROVED_REFER = "Approved on band - refer to approving authority"
APPROVED_REVIEW = "Approved on band - needs a look"
NO_CHANGE = "Approved on band - no change"
NO_CREDIT_DATA = "No credit data - approved"
NOT_ASSESSED = "Not assessed"
GROUPS = [COULD_APPROVE, REJECTED_REFER, REJECTED_REVIEW, STAYS_REJECTED,
          WOULD_REJECT, APPROVED_REFER, APPROVED_REVIEW, NO_CHANGE, NO_CREDIT_DATA, NOT_ASSESSED]


def run_checks(report: ParsedCreditReport, policy: Optional[Policy] = None) -> List[Check]:
    policy = policy or Policy()
    return _run(report, policy, _adverse_records(report, policy))


def missing_data(report: ParsedCreditReport) -> List[str]:
    """What the live gate's no-credit-data rule found missing (empty when the report is complete)."""
    return [w for w, gone in (("no band", report.band not in BAND_LADDER), ("no score", report.score is None),
                              ("no loans", not report.accounts)) if gone]


def band_rejected(band: Optional[str], policy: Policy) -> Optional[bool]:
    """True below the minimum band, False at or above it, None when unreadable."""
    if band not in BAND_LADDER:
        return None
    return BAND_LADDER.index(band) > BAND_LADDER.index(policy.min_band)


def evaluate(report: ParsedCreditReport, policy: Optional[Policy] = None) -> Dict:
    """The band decision, each check, the overall deviation result and the group.

    A report with no band, score or loans is approved without the checks, as
    the live gate does; "note" says what was missing.
    """
    policy = policy or Policy()
    rejected = band_rejected(report.band, policy)
    band_rule = {None: "No band", True: "Rejected", False: "Approved"}[rejected]
    missing = missing_data(report)
    if missing:
        note = (f"The CRB report has {', '.join(missing)}. Credit policy approves a file with no credit "
                "data whatever the band; the deviation checks were not run.")
        return {"band": report.band, "band_rule": band_rule, "checks": [], "overall": ALLOWED,
                "group": NO_CREDIT_DATA, "note": note, "report": report}
    checks = run_checks(report, policy)
    overall = _worst([c.result for c in checks])
    if rejected:
        group = {ALLOWED: COULD_APPROVE, REFER: REJECTED_REFER, REVIEW: REJECTED_REVIEW,
                 REJECT: STAYS_REJECTED}[overall]
    else:
        group = {ALLOWED: NO_CHANGE, REFER: APPROVED_REFER, REVIEW: APPROVED_REVIEW,
                 REJECT: WOULD_REJECT}[overall]
    return {"band": report.band, "band_rule": band_rule, "checks": checks,
            "overall": overall, "group": group, "note": "", "report": report}


def cutoff(policy: Policy) -> datetime:
    return datetime.strptime(policy.crb_history_cutoff, "%Y-%m-%d")
