"""
Credit bureau gate.

Process rule (set by credit policy):

    The bureau check runs FIRST. The MoMo statement is parsed only when the
    bureau band is at or above the configured minimum - by default DD, so
    AA, BB, CC and DD proceed and EE through JJ do not.

The band is the only thing that opens or closes the gate. Everything else the
report reveals is reported as a finding, because a band is a population
ranking, not an affordability test.

That distinction matters on real files. An applicant can sit in the AA band
with millions outstanding and months of arrears: the band is flattered by a
long tail of small, perfectly-repaid facilities while two large loans quietly
deteriorate. So findings carry their own severity, and a `blocking` finding
means "do not approve without a human decision" even though the gate is open.
"""
from __future__ import annotations

import re

from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import Policy
from .crb_parser import (
    BAND_SCORE_FLOORS,
    BAND_LADDER,
    CreditAccount,
    ParsedCreditReport,
    _term_months,
)

# Bureau classifications that mean the facility is not performing. These are the
# IFRS-style buckets TransUnion Rwanda prints; "NORMAL RISK" and "WATCH" are not
# in the list because they are still being serviced.
NON_PERFORMING_CLASSES = {"LOSS", "DOUBTFUL", "SUBSTANDARD", "NPA", "NON PERFORMING"}


def _rank(band: Optional[str]) -> Optional[int]:
    if band and band in BAND_LADDER:
        return BAND_LADDER.index(band)
    return None


def _date(value: Optional[str]) -> Optional[datetime]:
    for fmt in ("%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt) if value else None
        except (ValueError, TypeError):
            continue
    return None


def _report_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse the report's own 'Generated' timestamp, e.g. '13 Aug 2026 07:55 AM'."""
    for fmt in ("%d %b %Y %I:%M %p", "%d-%b-%Y %I:%M %p", "%d %b %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt) if value else None
        except (ValueError, TypeError):
            continue
    return None


def _cutoff_date(policy: Policy) -> datetime:
    return datetime.strptime(policy.crb_history_cutoff, "%Y-%m-%d")


def _after_cutoff(a: CreditAccount, cutoff: datetime) -> bool:
    """True unless the account's listing date is provably before the cutoff.

    An unreadable or missing listing date is NOT treated as old - suppressing a
    live finding needs proof it is stale, not merely the absence of a date.
    """
    d = _date(a.listing_date)
    return d is None or d >= cutoff


def _recent_repayment_behavior(a: CreditAccount, months: int = 12) -> Dict[str, Any]:
    """Last `months` entries of the 24-month grid, summarised for underwriting.

    `payment_history[0]` is the most recent month (see the DETERIORATING
    finding below, which relies on the same ordering). Each entry's status is
    the bureau's own days-past-due figure for that month where it is numeric;
    non-numeric codes (no record yet, account not open) are not counted as
    either an on-time or a delayed instalment.
    """
    recorded = [(h["month"], int(h["status"])) for h in (a.payment_history or [])[:months]
                if h["status"].isdigit()]
    delayed = [{"month": m, "days_late": d} for m, d in recorded if d > 0]
    return {
        "months_recorded": len(recorded),
        "months_on_time": len(recorded) - len(delayed),
        "months_delayed": len(delayed),
        "worst_days_late": max((d["days_late"] for d in delayed), default=0),
        "delayed_detail": delayed,
    }


def _cheque_after_cutoff(c: Dict[str, Any], cutoff: datetime) -> bool:
    """Same rule as _after_cutoff: only a readable date before the cutoff excludes."""
    for key in ("date_bounced", "cheque_date"):
        try:
            return datetime.strptime(c.get(key) or "", "%d-%b-%Y") >= cutoff
        except ValueError:
            continue
    return True


def _adverse_records(report: ParsedCreditReport, policy: Policy) -> List[str]:
    """Bounced cheques and fraud cases are a hard decline, not a refer.

    Cheques bounced before the history cutoff are old history and do not
    count. A summary count with no detail recovered cannot be dated, so it
    counts. Fraud cases carry no date on the report and always count.
    """
    def total(key: str) -> float:
        v = report.summary.get(key, {})
        return (v.get("your_sector") or 0) + (v.get("other_sectors") or 0)

    cutoff = _cutoff_date(policy)
    reasons: List[str] = []
    if total("fraudulent_cases") > 0:
        reasons.append(f"{int(total('fraudulent_cases'))} fraud case(s) on file")
    cheques = report.bounced_cheques
    recent = [c for c in cheques if _cheque_after_cutoff(c, cutoff)]
    if recent:
        reasons.append(f"{len(recent)} bounced cheque(s) since {policy.crb_history_cutoff}")
    elif total("bounced_cheques") > 0 and not cheques:
        reasons.append(f"{int(total('bounced_cheques'))} bounced cheque(s) counted in the "
                       "summary with no detail to date them")
    return reasons


def _finding(code, title, severity, detail, evidence=None) -> Dict[str, Any]:
    return {
        "code": code, "title": title, "severity": severity,
        "detail": detail, "evidence": evidence or {},
    }


def assess_credit_report(report: ParsedCreditReport, policy: Policy) -> Dict[str, Any]:
    band = report.band
    rank, floor = _rank(band), _rank(policy.min_band)
    open_accounts = [a for a in report.accounts if a.is_open]

    # ---------------------------------------------------------- the gate --
    if rank is None:
        gate = {
            "statement_parsing_allowed": False,
            "decision": "refer",
            "reason": "No bureau band could be read from the report. A human must "
                      "confirm the band before the statement is assessed.",
        }
    elif rank <= floor:
        gate = {
            "statement_parsing_allowed": True,
            "decision": "proceed",
            "reason": f"Band {band} meets the minimum of {policy.min_band}.",
        }
    else:
        gate = {
            "statement_parsing_allowed": False,
            "decision": "decline",
            "reason": f"Band {band} is below the minimum of {policy.min_band}. "
                      "The statement is not assessed.",
        }
    gate.update({
        "band": band,
        "score": report.score,
        "minimum_band": policy.min_band,
        "band_ladder": BAND_LADDER,
        "bands_accepted": BAND_LADDER[: (floor or 0) + 1],
        "band_score_ranges": _band_ranges(),
        "probability_of_default": report.probability_of_default,
    })

    # ------------------------------------------------------- freshness ---
    # A stale pull is a gate failure in its own right, independent of the
    # band: the applicant's position may have moved since the report was
    # generated. Checked after the band so a band decline keeps its own
    # reason as the headline, with the freshness problem appended rather
    # than overwriting it.
    report_dt = _report_datetime(report.report_date)
    now = datetime.now()
    report_age_days = (now - report_dt).days if report_dt else None
    report_is_current = (report_age_days <= policy.crb_max_age_days) if report_dt else None

    band_open = gate["statement_parsing_allowed"]
    gate["statement_parsing_allowed"] = band_open and report_is_current is True
    if report_is_current is not True:
        if report_is_current is False:
            freshness_reason = (
                f"The bureau report was generated {report_age_days} days ago "
                f"({report.report_date}); credit policy requires a report no older "
                f"than {policy.crb_max_age_days} days. Pull a fresh report before "
                "this applicant is assessed."
            )
            freshness_decision = "decline"
        else:
            freshness_reason = (
                "The bureau report's generation date could not be read, so its age "
                f"cannot be confirmed against the {policy.crb_max_age_days}-day "
                "freshness policy. A human must confirm the report is current "
                "before the statement is assessed."
            )
            freshness_decision = "refer"
        if band_open:
            gate["decision"] = freshness_decision
            gate["reason"] = freshness_reason
        else:
            gate["reason"] = f"{gate['reason']} Additionally: {freshness_reason}"

    # ------------------------------------------------- adverse records ---
    # Credit policy: any bounced cheque or fraud case ends the application.
    adverse = _adverse_records(report, policy)
    if adverse:
        adverse_reason = (
            f"Adverse record on the bureau file: {'; '.join(adverse)}. Credit policy "
            "declines any applicant with a bounced cheque or fraud case."
        )
        if gate["statement_parsing_allowed"]:
            gate["decision"] = "decline"
            gate["reason"] = adverse_reason
        else:
            gate["decision"] = "decline"
            gate["reason"] = f"{gate['reason']} Additionally: {adverse_reason}"
        gate["statement_parsing_allowed"] = False
    gate["adverse_records"] = adverse

    gate.update({
        "report_date": report.report_date,
        "report_age_days": report_age_days,
        "max_report_age_days": policy.crb_max_age_days,
        "report_is_current": report_is_current,
    })

    # ------------------------------------------------------- exposure ----
    # Backstop for programmatically constructed CreditAccount objects: the parser
    # normally populates these fields, but the analyzer can safely derive the same
    # principal-only estimate when an explicit term is present.
    for a in open_accounts:
        if not a.scheduled_payment_available and not a.calculated_emi_available:
            (inst, freq, months, basis, unusable) = _term_months(a.repayment_terms or "")
            if months and a.principal_amount > 0:
                a.repayment_instalments = a.repayment_instalments or inst
                a.repayment_frequency = a.repayment_frequency or freq
                a.repayment_term_months = months
                a.term_basis = a.term_basis or basis
                a.calculated_emi = a.principal_amount / months
                a.calculated_emi_available = True
            elif unusable and not a.term_unusable_reason:
                a.term_unusable_reason = unusable

    total_open = sum(a.balance_amount for a in open_accounts)
    total_arrears = sum(a.arrears_amount for a in open_accounts)

    # Eligibility EMI is NOT the EMI of every open bureau account.
    #
    # Two kinds of open account are excluded. A written-off, settled or
    # charged-off facility is not an ongoing monthly obligation. And, by
    # default, neither is a non-performing one: a facility classified LOSS or
    # DOUBTFUL, months past its term with arrears exceeding its balance, is not
    # being repaid, so a synthetic "monthly EMI" for it describes a schedule
    # that is not happening. Both are reported rather than deducted.
    def _non_performing(a: CreditAccount) -> bool:
        classification = (a.classification or "").strip().upper()
        return bool(a.is_non_performing or classification in NON_PERFORMING_CLASSES)

    def _ongoing_repayment(a: CreditAccount) -> bool:
        if not a.is_open:
            return False
        status = (a.account_status or "").strip().upper().replace("-", " ")
        classification = (a.classification or "").strip().upper().replace("-", " ")
        excluded = {
            "WRITTEN OFF", "WRITE OFF", "CHARGED OFF", "CHARGE OFF",
            "SETTLED", "PAID", "ACCOUNT CLOSED", "CLOSED", "INACTIVE",
        }
        if status in excluded or classification in excluded:
            return False
        if _non_performing(a) and not policy.deduct_emi_on_non_performing:
            return False
        return True

    ongoing_emi_accounts = [a for a in open_accounts if _ongoing_repayment(a)]
    excluded_emi_accounts = [a for a in open_accounts if a not in ongoing_emi_accounts]
    emi_reported_accounts = [a for a in ongoing_emi_accounts if a.scheduled_payment_available]
    emi_calculated_accounts = [a for a in ongoing_emi_accounts if (not a.scheduled_payment_available and a.calculated_emi_available)]
    emi_missing_accounts = [a for a in ongoing_emi_accounts if (not a.scheduled_payment_available and not a.calculated_emi_available)]

    # What each account actually contributes to the headline, rounded once, here.
    # The headline is then the sum of exactly the numbers shown in the table.
    # Rounding the total separately from the rows put them a rupee apart on the
    # reference file - 67,638 + 3,429 displayed against a stated 71,066.
    def _emi_contribution(a: CreditAccount) -> int:
        if a not in ongoing_emi_accounts:
            return 0
        if a.scheduled_payment_available:
            return round(a.scheduled_payment)
        if a.calculated_emi_available:
            return round(a.calculated_emi)
        return 0

    emi_contributions = {id(a): _emi_contribution(a) for a in open_accounts}
    scheduled = sum(emi_contributions.values())

    # Non-performing open facilities, held out of the EMI sum but never out of
    # sight: their balance and arrears are real money owed.
    unserviced = [a for a in open_accounts
                  if _non_performing(a) and not policy.deduct_emi_on_non_performing]

    # An unreported Days In Arrears is not zero days late. NPA blocks in this
    # format carry no Days In Arrears field at all, and defaulting it to 0 put
    # "0 days late" on screen beside a million in arrears.
    days_reported = [a for a in open_accounts if a.days_in_arrears_reported]
    worst_days = max((a.days_in_arrears for a in days_reported), default=None)

    if not ongoing_emi_accounts:
        emi_status = "no_ongoing_emi" if open_accounts else "no_open_loans"
    elif not emi_missing_accounts and not emi_calculated_accounts:
        emi_status = "complete"
    elif not emi_missing_accounts:
        emi_status = "calculated"
    elif emi_reported_accounts or emi_calculated_accounts:
        emi_status = "partial"
    else:
        emi_status = "not_available"

    open_pa = [a for a in open_accounts if not _non_performing(a)]
    open_npa = [a for a in open_accounts if _non_performing(a)]

    exposure = {
        "open_account_count": len(open_accounts),
        # Active loans, in the bureau's own sense: open and performing. This used
        # to be a duplicate of open_account_count, which counted the
        # non-performing ones as active too.
        "ongoing_loan_count": len(open_pa),
        "closed_account_count": len(report.accounts) - len(open_accounts),
        "portfolio": _portfolio(report, open_pa, open_npa),
        "unserviced_obligations": {
            "count": len(unserviced),
            "balance": round(sum(a.balance_amount for a in unserviced)),
            "arrears": round(sum(a.arrears_amount for a in unserviced)),
            "deducted_from_eligibility": policy.deduct_emi_on_non_performing,
            "note": (
                "Non-performing facilities are excluded from the monthly EMI "
                "deduction because they are not being repaid on any schedule. "
                "Excluding them leaves MORE revenue available, so they are "
                "paired with a blocking finding - never read this as the "
                "facility being harmless."
            ) if unserviced and not policy.deduct_emi_on_non_performing else None,
            "accounts": [
                {"index": a.index, "classification": a.classification,
                 "balance": round(a.balance_amount), "arrears": round(a.arrears_amount),
                 "last_payment": a.last_payment_date}
                for a in unserviced
            ],
        },
        "total_open_balance": round(total_open),
        "total_arrears": round(total_arrears),
        "arrears_share_of_balance": round(total_arrears / total_open, 4) if total_open else 0.0,
        "worst_days_in_arrears": worst_days,
        "worst_days_in_arrears_reported": bool(days_reported),
        "days_in_arrears_missing_count": len(open_accounts) - len(days_reported),
        "scheduled_monthly_payments": round(scheduled),
        "scheduled_monthly_payments_status": emi_status,
        "emi_scope": "open_performing_accounts_only",
        "ongoing_emi_account_count": len(ongoing_emi_accounts),
        "emi_accounts_with_reported_payment": len(emi_reported_accounts),
        "emi_accounts_with_calculated_payment": len(emi_calculated_accounts),
        "emi_accounts_missing_payment": len(emi_missing_accounts),
        "emi_accounts_excluded_from_eligibility": len(excluded_emi_accounts),
        "emi_missing_account_indices": [a.index for a in emi_missing_accounts],
        "emi_calculated_account_indices": [a.index for a in emi_calculated_accounts],
        "emi_excluded_account_indices": [a.index for a in excluded_emi_accounts],
        "credit_cap": policy.credit_cap,
        "cap_headroom": round(policy.credit_cap - total_open),
        "cap_breached": total_open > policy.credit_cap,
        "accounts": [
            {
                "index": a.index, "trade_sector": a.trade_sector, "type": a.account_type,
                "classification": a.classification, "status": a.account_status,
                "principal": round(a.principal_amount), "balance": round(a.balance_amount),
                "arrears": round(a.arrears_amount), "days_in_arrears": a.days_in_arrears,
                "worst_arrear_days": a.worst_arrear_days,
                "scheduled_payment": round(a.scheduled_payment) if a.scheduled_payment_available else (round(a.calculated_emi) if a.calculated_emi_available else None),
                # What this account adds to scheduled_monthly_payments. Zero for
                # anything excluded. `scheduled_payment` above is what the bureau
                # says or what the term implies, which is NOT the same number for
                # an excluded account - sum this column, never that one.
                "emi_contribution": emi_contributions.get(id(a), 0),
                "scheduled_payment_available": a.scheduled_payment_available,
                "scheduled_payment_printed_zero": a.scheduled_payment_printed_zero,
                "scheduled_payment_period_assumed_monthly": a.scheduled_payment_period_assumed_monthly,
                "calculated_emi": round(a.calculated_emi) if a.calculated_emi_available else None,
                "calculated_emi_available": a.calculated_emi_available,
                "repayment_terms": a.repayment_terms,
                "repayment_term_months": (round(a.repayment_term_months, 1)
                                          if a.repayment_term_months else None),
                "term_basis": a.term_basis,
                "term_unusable_reason": a.term_unusable_reason,
                "non_performing": _non_performing(a),
                "counted_in_emi": a in ongoing_emi_accounts,
                "emi_source": "reported" if a.scheduled_payment_available else ("calculated_principal_only" if a.calculated_emi_available else "unavailable"),
                "days_in_arrears_reported": a.days_in_arrears_reported,
                "listing_date": a.listing_date, "last_payment": a.last_payment_date,
                "ref": a.account_ref,
                # Last 12 months of the bureau's own 24-month grid, summarised:
                # how many instalments fell due, how many were late, and by how
                # many days each - the behaviour a band alone cannot show.
                "recent_repayment_behavior": _recent_repayment_behavior(a),
            }
            for a in sorted(open_accounts, key=lambda x: -x.balance_amount)
        ],
    }

    findings = _findings(report, policy, open_accounts, exposure, gate)

    blocking = [f for f in findings if f["severity"] == "blocking"]
    if gate["statement_parsing_allowed"] and blocking:
        gate["recommendation"] = "refer"
        gate["recommendation_reason"] = (
            f"The band opens the gate, but {len(blocking)} blocking finding"
            f"{'s' if len(blocking) != 1 else ''} must be cleared by a credit officer "
            "before approval. Band measures ranking against the population, not "
            "capacity to take on more debt."
        )
    elif gate["statement_parsing_allowed"]:
        gate["recommendation"] = "proceed"
        gate["recommendation_reason"] = "No blocking findings on the bureau file."
    else:
        gate["recommendation"] = gate["decision"]
        gate["recommendation_reason"] = gate["reason"]

    return {
        "bureau": {
            "source": report.bureau,
            "subscriber": report.subscriber,
            "report_date": report.report_date,
            "pdf_id": report.pdf_id,
            "pages": report.pages,
        },
        "applicant": report.profile.dict(),
        "gate": gate,
        "score": {
            "score": report.score,
            "band": band,
            "probability_of_default": report.probability_of_default,
            "adverse_action_reasons": report.adverse_action_reasons,
        },
        "exposure": exposure,
        "summary": report.summary,
        "repayment_track_record": _track_record(report, policy),
        "enquiries": _enquiry_profile(report),
        "collateral": {
            "distinct_entries": len(report.collaterals),
            "duplicates_removed": report.collateral_duplicates_removed,
            "items": report.collaterals,
            "note": (
                f"{report.collateral_duplicates_removed} identical duplicate entries were "
                "removed. Summing the raw list would overstate security "
                f"{report.collateral_duplicates_removed + 1}-fold."
            ) if report.collateral_duplicates_removed else None,
        },
        "insurance_policies": report.insurance_policies,
        "bounced_cheques": report.bounced_cheques,
        "findings": findings,
        "notes": _notes(report, policy),
    }


def _portfolio(report, open_pa: List, open_npa: List) -> Dict[str, Any]:
    """Performing vs non-performing, parsed detail against the bureau's own totals.

    The SUMMARY block on page 1 states Open PA, Open NPA and their open values
    independently of the account detail pages. That makes it a checksum: if the
    parser recovers a different number of open accounts, or a different open
    value, something was mis-sectioned. A phantom account swept in from the
    Bounced Cheques block is exactly the failure this catches.
    """
    s = report.summary

    def stated(key: str) -> Optional[float]:
        v = s.get(key)
        if not v:
            return None
        return (v.get("your_sector") or 0) + (v.get("other_sectors") or 0)

    pa_value = sum(a.balance_amount for a in open_pa)
    npa_value = sum(a.balance_amount for a in open_npa)
    stated_pa_count, stated_npa_count = stated("open_pa"), stated("open_npa")
    stated_pa_value, stated_npa_value = stated("pa_open_total_value"), stated("npa_open_total_value")
    stated_closed = stated("closed_pa")
    if stated_closed is not None and stated("closed_npa") is not None:
        stated_closed += stated("closed_npa")

    checks: List[Dict[str, Any]] = []

    def check(label: str, parsed: float, declared: Optional[float], tolerance: float = 1.0) -> None:
        if declared is None:
            checks.append({"check": label, "parsed": round(parsed), "bureau_summary": None,
                           "matches": None, "note": "Not printed in the summary block."})
            return
        checks.append({"check": label, "parsed": round(parsed),
                       "bureau_summary": round(declared),
                       "matches": abs(parsed - declared) <= tolerance})

    check("open_performing_accounts", len(open_pa), stated_pa_count, 0)
    check("open_non_performing_accounts", len(open_npa), stated_npa_count, 0)
    check("open_performing_value", pa_value, stated_pa_value)
    check("open_non_performing_value", npa_value, stated_npa_value)
    check("closed_accounts", len(report.accounts) - len(open_pa) - len(open_npa), stated_closed, 0)

    mismatched = [c for c in checks if c["matches"] is False]

    return {
        "performing": {
            "open_count": len(open_pa),
            "open_value": round(pa_value),
            "bureau_open_count": int(stated_pa_count) if stated_pa_count is not None else None,
            "bureau_open_value": round(stated_pa_value) if stated_pa_value is not None else None,
            "by_sector": _by_sector(s, "open_pa", "pa_open_total_value"),
        },
        "non_performing": {
            "open_count": len(open_npa),
            "open_value": round(npa_value),
            "bureau_open_count": int(stated_npa_count) if stated_npa_count is not None else None,
            "bureau_open_value": round(stated_npa_value) if stated_npa_value is not None else None,
            "by_sector": _by_sector(s, "open_npa", "npa_open_total_value"),
        },
        "reconciles": not mismatched,
        "checks": checks,
        "note": "Counts and values recovered from the account pages, checked against "
                "the figures the bureau states in its own SUMMARY block. A mismatch "
                "means the account sections were mis-read, not that the applicant "
                "changed.",
    }


def _by_sector(summary: Dict[str, Any], count_key: str, value_key: str) -> Dict[str, Any]:
    """The bureau's own your-sector / other-sectors split, reported as printed."""
    counts = summary.get(count_key) or {}
    values = summary.get(value_key) or {}
    return {
        "your_sector": {"open_count": counts.get("your_sector"),
                        "open_value": values.get("your_sector")},
        "other_sectors": {"open_count": counts.get("other_sectors"),
                          "open_value": values.get("other_sectors")},
    }


def _band_ranges() -> List[Dict[str, Any]]:
    """The report's own score-band index, as a table.

    The band letters alone say nothing about what score produced them. These
    floors are lifted from the report's index page, so the ladder can show the
    range each band covers instead of ten bare letters.
    """
    out: List[Dict[str, Any]] = []
    for i, (band, floor) in enumerate(BAND_SCORE_FLOORS):
        ceiling = 900 if i == 0 else BAND_SCORE_FLOORS[i - 1][1] - 1
        out.append({
            "band": band,
            "min_score": floor,
            "max_score": ceiling,
            "range": f"{floor}-{ceiling}",
        })
    return out


def _findings(report, policy, open_accounts, exposure, gate) -> List[Dict[str, Any]]:
    f: List[Dict[str, Any]] = []
    s = report.summary
    cutoff = _cutoff_date(policy)

    def total(key: str) -> float:
        v = s.get(key, {})
        return (v.get("your_sector") or 0) + (v.get("other_sectors") or 0)

    # Hard credit events. Accounts listed before the history cutoff are old
    # news, not the applicant's current position, and are excluded here -
    # though never on the strength of a missing date; see _after_cutoff.
    npa_all = [a for a in report.accounts if a.is_non_performing]
    npa_recent = [a for a in npa_all if _after_cutoff(a, cutoff)]
    npa_excluded = [a for a in npa_all if a not in npa_recent]
    if npa_recent:
        f.append(_finding(
            "NPA", "Non-performing account on file", "blocking",
            "A non-performing account is a hard decline under most credit policies.",
            {"accounts": [{"index": a.index, "classification": a.classification,
                           "listing_date": a.listing_date} for a in npa_recent],
             "excluded_pre_cutoff": [{"index": a.index, "listing_date": a.listing_date}
                                     for a in npa_excluded] or None}))
    elif npa_excluded:
        f.append(_finding(
            "NPA_PRE_CUTOFF", "Non-performing account predates the history cutoff", "informational",
            f"{len(npa_excluded)} non-performing account(s) list before "
            f"{policy.crb_history_cutoff}, credit policy's history cutoff, and are "
            "excluded from the NPA finding. They remain in the exposure balances above.",
            {"accounts": [{"index": a.index, "classification": a.classification,
                           "listing_date": a.listing_date} for a in npa_excluded]}))
    elif total("non-performing_accounts") > 0:
        # The bureau's summary counts an NPA that no account block was parsed
        # as non-performing. There is no date to test the cutoff against, so
        # this is not silently dropped - it is a parse-integrity gap, not
        # evidence of nothing.
        f.append(_finding(
            "NPA", "Bureau summary states a non-performing account not found on the account pages",
            "blocking",
            "The SUMMARY block counts a non-performing account, but no account "
            "record was parsed as non-performing. Reconcile against the PDF before "
            "treating this file as clean.",
            {"stated_in_summary": total("non-performing_accounts")}))
    if total("fraudulent_cases") > 0:
        f.append(_finding("FRAUD", "Fraud record on file", "blocking",
                          "Decline and escalate to the fraud team."))
    old_cheques = [c for c in report.bounced_cheques if not _cheque_after_cutoff(c, cutoff)]
    cheques = [c for c in report.bounced_cheques if _cheque_after_cutoff(c, cutoff)]
    if old_cheques:
        f.append(_finding(
            "BOUNCED_CHEQUE_PRE_CUTOFF", "Bounced cheques predate the history cutoff",
            "informational",
            f"{len(old_cheques)} cheque(s) bounced before {policy.crb_history_cutoff} "
            "and are excluded as old history.",
            {"excluded_pre_cutoff": old_cheques}))
    if cheques or (total("bounced_cheques") > 0 and not report.bounced_cheques):
        stated = int(total("bounced_cheques"))
        detail = "Payment failure history is on the bureau record."
        if cheques:
            worst = max(c["amount"] for c in cheques)
            detail = (
                f"{len(cheques)} cheque(s) returned unpaid, largest {worst:,.0f} RWF. "
                "A dishonoured cheque is a failure to fund a payment the applicant "
                "had already committed to."
            )
        evidence: Dict[str, Any] = {"stated_in_summary": stated,
                                    "detail_records_parsed": len(cheques)}
        if cheques:
            evidence["cheques"] = [
                {"date_bounced": c["date_bounced"], "amount": round(c["amount"]),
                 "reason": c["reason"], "trade_sector": c["trade_sector"],
                 "cheque_no": c["cheque_no"]}
                for c in cheques
            ]
        if stated and not cheques:
            evidence["note"] = (
                "The summary counts bounced cheques but no detail block was "
                "recovered. Read the report directly before relying on this."
            )
        f.append(_finding("BOUNCED_CHEQUE", "Bounced cheques on file", "blocking",
                          detail, evidence))

    # Parse integrity. The bureau's own summary is the checksum; if the account
    # pages do not agree with it, no figure below can be trusted.
    portfolio = exposure["portfolio"]
    if not portfolio["reconciles"]:
        bad = [c for c in portfolio["checks"] if c["matches"] is False]
        f.append(_finding(
            "SUMMARY_MISMATCH", "Parsed accounts do not match the bureau summary", "blocking",
            f"{len(bad)} of {len(portfolio['checks'])} checks disagree with the figures "
            "the bureau states in its own summary block. Reconcile against the PDF "
            "before using any exposure or EMI figure from this report.",
            {"checks": bad}))
    if report.skipped_account_blocks:
        f.append(_finding(
            "BLOCKS_SKIPPED", "Non-account blocks were skipped", "informational",
            f"{len(report.skipped_account_blocks)} block(s) matched the account "
            "pattern but carried no account fields, and were not counted as accounts.",
            {"blocks": report.skipped_account_blocks}))

    # Non-performing facilities held out of the EMI deduction. Facilities
    # listed before the history cutoff are old history, like the NPA finding.
    uns = exposure["unserviced_obligations"]
    recent_indices = {a.index for a in open_accounts if _after_cutoff(a, cutoff)}
    uns_recent = [a for a in uns["accounts"] if a["index"] in recent_indices]
    uns_old = [a for a in uns["accounts"] if a["index"] not in recent_indices]
    if uns_recent and not uns["deducted_from_eligibility"]:
        bal = sum(a["balance"] for a in uns_recent)
        arr = sum(a["arrears"] for a in uns_recent)
        f.append(_finding(
            "UNSERVICED_OBLIGATION", "Open debt with no active repayment schedule", "blocking",
            f"{len(uns_recent)} non-performing facility(ies) hold {bal:,} RWF "
            f"outstanding with {arr:,} RWF in arrears. No monthly EMI is "
            "deducted for them because none is being paid. This raises apparent "
            "affordability, so it must be cleared by a credit officer rather than "
            "read as capacity.",
            {"accounts": uns_recent, "excluded_pre_cutoff": uns_old or None}))
    elif uns_old and not uns["deducted_from_eligibility"]:
        f.append(_finding(
            "UNSERVICED_OBLIGATION_PRE_CUTOFF",
            "Unserviced debt predates the history cutoff", "informational",
            f"{len(uns_old)} non-performing facility(ies) list before "
            f"{policy.crb_history_cutoff} and are excluded from the unserviced-debt "
            "finding. Their balances remain in the exposure figures above.",
            {"accounts": uns_old}))

    # Scheduled Payments has no stated period in this report format.
    assumed = [a for a in exposure["accounts"]
               if a.get("scheduled_payment_period_assumed_monthly")]
    if assumed:
        f.append(_finding(
            "EMI_PERIOD_ASSUMED", "Reported payment may not be monthly", "warning",
            f"{len(assumed)} account(s) report a scheduled payment against a "
            "non-monthly repayment schedule. The bureau does not state the period "
            "of the Scheduled Payments figure; it is taken as monthly, which "
            "understates the obligation if it is per-instalment.",
            {"accounts": [{"index": a["index"], "repayment_terms": a["repayment_terms"],
                           "scheduled_payment": a["scheduled_payment"]} for a in assumed]}))

    # Accounts whose scheduled payment was printed as zero against a live balance.
    placeholder = [a for a in exposure["accounts"] if a.get("scheduled_payment_printed_zero")]
    if placeholder:
        f.append(_finding(
            "EMI_PLACEHOLDER_ZERO", "Scheduled payment printed as zero", "informational",
            f"{len(placeholder)} account(s) print a scheduled payment of 0.00 while "
            "carrying a live balance. A zero is treated as unreported rather than as "
            "an obligation of zero, and a principal-only estimate is used where a "
            "term is available.",
            {"accounts": [{"index": a["index"], "balance": a["balance"],
                           "emi_source": a["emi_source"]} for a in placeholder]}))

    # EMI data quality: never imply a complete monthly obligation when the bureau
    # did not print a scheduled payment for every open account.
    if exposure["ongoing_emi_account_count"] and exposure["scheduled_monthly_payments_status"] not in {"complete", "calculated"}:
        if exposure["scheduled_monthly_payments_status"] == "not_available":
            detail = (
                "The bureau lists ongoing repayment accounts but does not provide a "
                "scheduled payment or a safely calculable term for any of them. "
                "No EMI deduction is applied for those accounts."
            )
        else:
            detail = (
                f"The bureau reports scheduled payments for "
                f"{exposure['emi_accounts_with_reported_payment']} reported + "
                f"{exposure['emi_accounts_with_calculated_payment']} calculated of "
                f"{exposure['ongoing_emi_account_count']} ongoing repayment accounts. "
                f"Only those known ongoing EMI amounts ({exposure['scheduled_monthly_payments']:,} RWF) "
                "are deducted from statement net revenue."
            )
        f.append(_finding(
            "EMI_DATA_INCOMPLETE", "Monthly EMI data is incomplete", "warning",
            detail,
            {
                "reported_emi_accounts": exposure["emi_accounts_with_reported_payment"],
                "calculated_emi_accounts": exposure["emi_accounts_with_calculated_payment"],
                "missing_emi_accounts": exposure["emi_accounts_missing_payment"],
                "reported_monthly_emi": exposure["scheduled_monthly_payments"],
                "missing_account_indices": exposure["emi_missing_account_indices"],
            },
        ))

    # Exposure against the cap.
    if exposure["cap_breached"]:
        f.append(_finding(
            "CAP_BREACH", "Existing exposure exceeds the credit cap", "blocking",
            f"{exposure['total_open_balance']:,} RWF is outstanding across "
            f"{exposure['open_account_count']} open accounts against a cap of "
            f"{policy.credit_cap:,} RWF - "
            f"{exposure['total_open_balance'] / policy.credit_cap:.1f}x the ceiling.",
            {"total_open_balance": exposure["total_open_balance"],
             "credit_cap": policy.credit_cap}))

    # From here on, only accounts listed on or after the history cutoff feed
    # the finding - an account excluded on cutoff grounds is old news, not the
    # applicant's current position, but its money is still real and stays in
    # the exposure balances above.
    open_recent = [a for a in open_accounts if _after_cutoff(a, cutoff)]

    # Arrears.
    recent_arrears_rows = [a for a in exposure["accounts"]
                           if a["arrears"] > 0 and a["index"] in recent_indices]
    excluded_arrears_rows = [a for a in exposure["accounts"]
                             if a["arrears"] > 0 and a["index"] not in recent_indices]
    recent_arrears_total = round(sum(a["arrears"] for a in recent_arrears_rows))
    if recent_arrears_total > 0:
        sev = "blocking" if recent_arrears_total > policy.max_arrears_refer else "warning"
        f.append(_finding(
            "ARREARS", "Accounts in arrears", sev,
            f"{recent_arrears_total:,} RWF is in arrears on accounts listed on or after "
            f"{policy.crb_history_cutoff}. Arrears are money already owed and unpaid, "
            "not future capacity.",
            {"accounts": recent_arrears_rows,
             "excluded_pre_cutoff": excluded_arrears_rows or None}))
    elif excluded_arrears_rows:
        f.append(_finding(
            "ARREARS_PRE_CUTOFF", "Arrears predate the history cutoff", "informational",
            f"{len(excluded_arrears_rows)} account(s) in arrears list before "
            f"{policy.crb_history_cutoff} and are excluded from the arrears finding. "
            "They remain in the exposure balance above.",
            {"accounts": excluded_arrears_rows}))

    late = [a for a in open_recent
            if a.days_in_arrears_reported and a.days_in_arrears >= policy.max_days_in_arrears]
    if late:
        worst_recent = max(a.days_in_arrears for a in late)
        f.append(_finding(
            "DELINQUENT", "Open accounts past due", "blocking",
            f"{len(late)} open account(s) at {policy.max_days_in_arrears}+ days past due, "
            f"worst {worst_recent} days. Status may still read "
            "CURRENT - that is a billing state, not a health check.",
            {"accounts": [{"index": a.index, "type": a.account_type,
                           "days_in_arrears": a.days_in_arrears,
                           "classification": a.classification,
                           "status": a.account_status} for a in late]}))

    # Deteriorating trend from the 24-month grids.
    worsening = []
    for a in open_recent:
        nums = [(h["month"], int(h["status"])) for h in a.payment_history if h["status"].isdigit()]
        if len(nums) >= 2 and nums[0][1] > nums[-1][1] and nums[0][1] > 0:
            worsening.append({"index": a.index, "type": a.account_type,
                              "history": [{"month": m, "days": d} for m, d in nums[:6]]})
    if worsening:
        f.append(_finding(
            "DETERIORATING", "Arrears deepening, not recovering", "warning",
            "The 24-month grids show days-past-due increasing on at least one open account.",
            {"accounts": worsening}))

    # Adverse classifications. The history cutoff covers NPAs and arrears only,
    # so a performing account's classification counts whatever its listing
    # date; a pre-cutoff non-performing one is already reported as old history.
    watch = [a for a in open_accounts
             if (not a.is_non_performing or _after_cutoff(a, cutoff))
             and a.classification and a.classification.upper() not in ("", "NORMAL RISK")]
    if watch:
        f.append(_finding(
            "ADVERSE_CLASSIFICATION", "Adverse bureau classification", "warning",
            "One or more open accounts are classified worse than normal risk.",
            {"accounts": [{"index": a.index, "classification": a.classification,
                           "balance": round(a.balance_amount)} for a in watch]}))

    # Enquiry velocity.
    recent = total("enquiries_-_last_30_days") + total("enquiries_-_last_31_-_60_days")
    if recent >= policy.max_enquiries_90d:
        f.append(_finding(
            "ENQUIRY_VELOCITY", "Frequent credit shopping", "warning",
            f"{int(recent)} bureau enquiries in the last 60 days, "
            f"{len(report.enquiries)} on file. Repeated applications often precede distress.",
            {"recent_60_days": int(recent), "total_listed": len(report.enquiries)}))

    # Score / reality divergence - the trap this whole module exists to catch.
    # Arrears and days late count only accounts listed on or after the history
    # cutoff; the cap is today's exposure and counts every open account.
    worst = max((a.days_in_arrears for a in open_recent if a.days_in_arrears_reported),
                default=None)
    if gate.get("statement_parsing_allowed") and (
        exposure["cap_breached"]
        or (worst is not None and worst >= policy.max_days_in_arrears)
        # Days in arrears is frequently absent on non-performing blocks in this
        # format. Real arrears against a flattering band is the same divergence.
        or recent_arrears_total > policy.max_arrears_refer
    ):
        f.append(_finding(
            "SCORE_DIVERGENCE", "Band does not reflect the account detail", "blocking",
            f"Band {report.band} with a stated {report.probability_of_default}% default "
            "probability sits alongside real arrears and cap breach. The band is a "
            "population ranking, flattered here by a long tail of small closed "
            "facilities. Do not approve on the band alone.",
            {"band": report.band, "score": report.score,
             "probability_of_default": report.probability_of_default,
             "adverse_action_reasons": report.adverse_action_reasons}))

    # Unpaid insurance.
    unpaid = [p for p in report.insurance_policies
              if p.get("balance_on_policy", 0) > 0 or p.get("status", "").upper() == "UNPAID"]
    if unpaid:
        f.append(_finding(
            "INSURANCE_ARREARS", "Unpaid insurance premium", "informational",
            "An insurance policy is unpaid or in arrears - a further committed outflow.",
            {"policies": unpaid}))

    # Thin sector history.
    own = s.get("performing_accounts", {}).get("your_sector")
    if own is not None and own == 0:
        f.append(_finding(
            "NO_SECTOR_HISTORY", "No history in your sector", "informational",
            "The applicant has no prior accounts with this lender's sector, so the "
            "band rests entirely on other sectors' data.",
            {"your_sector_performing_accounts": own}))

    order = {"blocking": 0, "warning": 1, "informational": 2}
    return sorted(f, key=lambda x: order.get(x["severity"], 3))


def _track_record(report, policy) -> Dict[str, Any]:
    cutoff = _cutoff_date(policy)
    all_closed = [a for a in report.accounts if not a.is_open]
    closed = [a for a in all_closed if _after_cutoff(a, cutoff)]
    excluded = len(all_closed) - len(closed)
    small = [a for a in closed if a.principal_amount and a.principal_amount <= 500_000]
    by_period: Dict[str, List[float]] = {}
    for a in small:
        d = _date(a.listing_date)
        if d:
            by_period.setdefault(f"{d.year}-Q{(d.month - 1) // 3 + 1}", []).append(a.principal_amount)
    return {
        "closed_accounts": len(closed),
        "closed_accounts_excluded_pre_cutoff": excluded,
        "closed_small_facilities": len(small),
        "total_small_advanced": round(sum(a.principal_amount for a in small)),
        "all_repaid_without_arrears": all(a.arrears_amount == 0 and a.days_in_arrears == 0
                                          for a in closed),
        "principal_escalation": [
            {"period": k, "count": len(v), "average_principal": round(sum(v) / len(v))}
            for k, v in sorted(by_period.items())
        ],
        "note": "Small-facility discipline and large-facility discipline are different "
                "signals. A clean run of micro-loans does not evidence capacity to "
                "service a multi-million instalment loan."
                + (f" {excluded} closed facility(ies) listed before "
                   f"{policy.crb_history_cutoff} are excluded as old history."
                   if excluded else ""),
    }


def _enquiry_profile(report) -> Dict[str, Any]:
    from collections import Counter
    reasons = Counter(e["reason"] for e in report.enquiries)
    years = Counter(e["date"][-4:] for e in report.enquiries)
    return {
        "total_listed": len(report.enquiries),
        "by_reason": dict(reasons),
        "by_year": dict(sorted(years.items())),
        "items": report.enquiries,
    }


def _notes(report, policy) -> List[str]:
    notes = [
        f"The gate is band-only: {', '.join(BAND_LADDER[:BAND_LADDER.index(policy.min_band) + 1])} "
        f"proceed to statement parsing, "
        f"{', '.join(BAND_LADDER[BAND_LADDER.index(policy.min_band) + 1:])} do not.",
        "Account Status of CURRENT is a billing state, not a health indicator. "
        "Days in arrears and classification are used instead.",
        "Monthly EMI uses bureau Scheduled Payments where reported. When Scheduled Payments "
        "are missing but the bureau gives an explicit repayment term in months, a principal-only "
        "monthly estimate (principal divided by term) may be calculated and is labelled as an estimate. "
        "No interest rate, fees or other repayment costs are invented. Accounts with neither a reported "
        "payment nor a usable term remain unavailable and prevent a complete EMI assessment.",
    ]
    if report.collateral_duplicates_removed:
        notes.append(
            f"{report.collateral_duplicates_removed} duplicate collateral entries removed; "
            "the raw list repeats one security once per facility.")
    if report.profile.employment:
        notes.append(
            f"Employment is recorded as '{report.profile.employment}'. Bureau employment "
            "fields are frequently placeholder text - do not treat as verified income.")
    notes.append(
        "The bureau phone list is not a KYC match. Confirm separately that the MoMo "
        "statement number belongs to this applicant before the two documents are "
        "treated as one customer.")
    return notes
