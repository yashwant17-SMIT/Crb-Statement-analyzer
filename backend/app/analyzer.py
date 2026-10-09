"""
Underwriting engine.

Net monthly revenue = Gross inflows - Self-transfers - Loan inflows - Large txns

Everything the engine cannot prove is reported as "possible" and left OUT of
the deducted total, each exclusion shown separately. The engine never
guesses a counterparty's purpose.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .config import Policy
from .parser import ParsedStatement


# ---------------------------------------------------------------- helpers ---
def _i(x) -> int:
    return int(round(float(x)))


def _share(part: float, whole: float) -> float:
    return round(part / whole, 4) if whole else 0.0


# Patterns short enough to appear by accident inside an ordinary word. These
# must match on a word boundary. "bok" (Bank of Kigali) sits inside the Rwandan
# surname NYIRANGOBOKA, which classified a private individual as a bank; "rra",
# "mtn" and "spenn" are the same hazard.
_BOUNDED_PATTERNS = {
    "bok", "rra", "mtn", "bpr", "ncba", "spenn", "sacco", "ekash",
    "mifi", "mfi", "im bank", "system",
}


def _matches(text: str, patterns) -> Optional[str]:
    """Find the first pattern present in `text`.

    Plain substring matching for anything long enough to be unambiguous; a
    word-boundary match for the handful of short codes that otherwise fire
    inside ordinary names. A false positive here is not cosmetic: it moves a
    real person into the institution bucket and out of the distinct-individuals
    disclosure.
    """
    low = (text or "").lower()
    for pat in patterns:
        if pat in _BOUNDED_PATTERNS:
            if re.search(rf"(?<![a-z]){re.escape(pat)}(?![a-z])", low):
                return pat
        elif pat in low:
            return pat
    return None


def _mask(series: pd.Series, fn) -> pd.Series:
    """Boolean mask that survives an empty frame.

    `Series.apply` on an empty string-dtype column returns a string-dtype
    Series, and combining two of those with `|` raises TypeError. An account
    with inflows but no outflows hits exactly that, so every boolean mask in
    this module goes through here.
    """
    if len(series) == 0:
        return pd.Series(False, index=series.index, dtype=bool)
    return series.apply(fn).astype(bool)


def _frame(statement: ParsedStatement, account: str) -> pd.DataFrame:
    df = pd.DataFrame([t.dict() for t in statement.transactions])
    df["dt"] = pd.to_datetime(df["timestamp"], format="ISO8601")
    df["month"] = df["dt"].dt.to_period("M").astype(str)
    df["day"] = df["dt"].dt.date.astype(str)
    df["fee_num"] = df["fee"].fillna(0).astype(int)

    # Direction. Formats with debit/credit columns state it outright and the
    # adapter fills `direction`; formats that don't leave it null and we compare
    # the party identifiers against the account. A small number of
    # operator-generated rows carry an internal id in the To slot, so that path
    # also falls back to the holder's own display name.
    stated = df["direction"] if "direction" in df.columns else pd.Series(None, index=df.index)
    holder = (statement.header.customer or "").split()[0].lower() if statement.header.customer else ""
    to_is_self = df["to_msisdn"].eq(account)
    if holder:
        to_is_self |= df["to_name"].str.lower().str.startswith(holder) & ~df["from_msisdn"].eq(account)
    derived_in = to_is_self
    derived_out = df["from_msisdn"].eq(account) & ~derived_in

    df["is_inflow"] = np.where(stated.notna(), stated.eq("in"), derived_in)
    df["is_outflow"] = np.where(stated.notna(), stated.eq("out"), derived_out)

    # Nothing may vanish. A row that is neither an inflow nor an outflow is not
    # silently dropped from every figure - it is counted and disclosed.
    df["is_unattributed"] = ~df["is_inflow"] & ~df["is_outflow"]

    df["cp_id"] = np.where(df["is_inflow"], df["from_msisdn"], df["to_msisdn"])
    df["cp_name"] = np.where(df["is_inflow"], df["from_name"], df["to_name"])
    return df


def _float_account(statement, policy: Policy) -> Optional[str]:
    """Does the statement header say this is an agent float account?"""
    acct_type = getattr(statement.header, "account_type", None) or ""
    hit = _matches(acct_type, policy.float_account_patterns)
    return acct_type if hit else None


def _capability_notes(statement, policy: Policy) -> List[Dict[str, str]]:
    """What this format cannot answer, stated plainly rather than left blank.

    A check that could never have fired must not read as a check that passed.
    """
    caps = getattr(statement, "capabilities", None)
    if caps is None:
        return []
    notes = []
    if not caps.counterparty_name:
        notes.append({
            "check": "self_transfer_name_match",
            "status": "substituted",
            "note": "This format carries no counterparty name column, so the sender-name "
                    "test cannot run. Self-transfers are identified by account identity "
                    "instead, which is exact; near-match review is not available.",
        })
        notes.append({
            "check": "distinct_individual_senders",
            "status": "not_assessed",
            "note": "Distinct senders are counted, but individuals cannot be separated "
                    "from merchants without display names.",
        })
        notes.append({
            "check": "merchant_and_institution_classification",
            "status": "not_assessed",
            "note": "Institutional and merchant credits cannot be identified by name, so "
                    "bank and e-wallet credits cannot be excluded from net revenue on "
                    "this format. Net revenue is therefore overstated by whatever they "
                    "amount to.",
        })
    if not caps.balance:
        notes.append({
            "check": "low_liquidity",
            "status": "not_assessed",
            "note": "This format carries no running balance, so liquidity and balance-chain "
                    "checks cannot run.",
        })
    if caps.timestamp_precision == "day":
        notes.append({
            "check": "rapid_round_trips",
            "status": "not_assessed",
            "note": f"Entries are booked by date with no clock time, so the "
                    f"{policy.roundtrip_window_minutes}-minute round-trip window and the "
                    "single-hour concentration test cannot be evaluated. Same-day "
                    "pass-through is still measured.",
        })
    if not caps.txn_type:
        notes.append({
            "check": "agent_float_cash_in",
            "status": "degraded",
            "note": "Transaction type is inferred from narration text rather than stated, "
                    "so CASH_IN clustering is indicative only.",
        })
    if not caps.status:
        notes.append({
            "check": "failed_transactions",
            "status": "not_assessed",
            "note": "This format lists settled entries only and carries no status column; "
                    "reversed or failed attempts are not visible.",
        })
    if caps.cp_id_basis == "derived_name":
        notes.append({
            "check": "counterparty_identity",
            "status": "degraded",
            "note": "Counterparties are keyed on a slug of the narration text, not on an "
                    "identifier. Two people with the same display name collide, and one "
                    "person written two ways splits. Concentration, same-counterparty "
                    "round-trip and loan-cycle figures inherit this limitation.",
        })
    if caps.cp_id_basis == "none":
        notes.append({
            "check": "same_counterparty_round_trip_and_loan_cycle",
            "status": "not_assessed",
            "note": "This format carries no stable counterparty identifier, so same-counterparty "
                    "rapid reversals and pattern-based loan cycles cannot be established reliably.",
        })
    return notes


# ------------------------------------------------------------ classifiers ---
def _classify_inflows(
    inf: pd.DataFrame,
    holder_name: str,
    policy: Policy,
    caps=None,
    account: str = "",
) -> pd.DataFrame:
    """Apply the Step 2 exclusions with the evidence this format actually has.

    The strict rule is a sender-NAME match against the holder. Two of the three
    supported formats carry no usable name column, and on those the same
    question is answered better by identity: a row whose sender is the account
    itself is a self-movement, exactly, with no spelling variants to review.
    Where neither is available the exclusion is reported as unassessable rather
    than silently returning zero.
    """
    inf = inf.copy()
    tokens = {t for t in (holder_name or "").lower().split() if len(t) > 2}
    has_names = getattr(caps, "counterparty_name", True)
    id_based = getattr(caps, "cp_id_basis", "msisdn") in ("msisdn", "account_no") and bool(account)

    def is_self(name: str) -> bool:
        low = (name or "").lower()
        return bool(tokens) and all(t in low for t in tokens)

    def is_near_self(name: str) -> bool:
        low = (name or "").lower()
        return bool(tokens) and any(t in low for t in tokens) and not is_self(name)

    by_name = _mask(inf["cp_name"], is_self) if has_names else _mask(inf["cp_name"], lambda n: False)
    # An inflow whose counterparty key IS the account is the holder moving his
    # own money - the identity form of the same test.
    by_identity = inf["cp_id"].eq(account) if id_based else _mask(inf["cp_id"], lambda n: False)
    # A named own-wallet product (MoKash Savings and similar) belongs to this
    # customer by construction - a savings wallet cannot be a third party's - so
    # it is the holder moving their own money even though the sender string is
    # the product name rather than the holder's name.
    own_wallet_hit = (
        inf["cp_name"].apply(lambda n: _matches(n, policy.own_wallet_patterns))
        if len(inf) else pd.Series(dtype=object, index=inf.index)
    )
    by_own_wallet = own_wallet_hit.notna() if len(inf) else _mask(inf["cp_name"], lambda n: False)

    inf["own_wallet"] = own_wallet_hit
    inf["is_self_transfer"] = by_name | by_identity | by_own_wallet
    inf["self_transfer_basis"] = np.where(
        by_identity, "identity",
        np.where(by_name, "name", np.where(by_own_wallet, "own_wallet_product", "")),
    )
    inf["is_possible_self"] = (
        _mask(inf["cp_name"], is_near_self) if has_names
        else _mask(inf["cp_name"], lambda n: False)
    )
    # A counterparty id that is not a number is a short code - "airtime",
    # "CBA-SAVINGS/SP", "urwegobank" - and marks an institution. That only holds
    # where the id column carries a REAL identifier. On a derived_name basis the
    # id is manufactured from the counterparty's name, so every id is
    # alphabetic and this rule would classify every private individual as an
    # institution. Harmless when institutional credits were only a what-if;
    # now that they are deducted it would zero out the revenue of every
    # statement in that format.
    id_is_identifier = getattr(caps, "cp_id_basis", "msisdn") in ("msisdn", "account_no")
    shortcode = (
        _mask(inf["cp_id"], lambda s: bool(re.match(r"^[A-Za-z]", str(s))))
        | _mask(inf["cp_id"], lambda s: s == "N/A")
    ) if id_is_identifier else _mask(inf["cp_id"], lambda s: False)
    inf["is_institution"] = (
        _mask(inf["cp_name"], lambda n: _matches(n, policy.institution_patterns) is not None)
        | shortcode
    )
    inf["is_merchant"] = _mask(
        inf["cp_name"], lambda n: _matches(n, policy.merchant_patterns) is not None
    )
    inf["lender_hit"] = (
        inf["cp_name"].apply(lambda n: _matches(n, policy.confirmed_lenders))
        if len(inf) else pd.Series(dtype=object, index=inf.index)
    )
    # A type column naming the transaction a loan is stronger evidence than a
    # name match and needs no lender registry entry. The MoMo spreadsheet
    # export prints CUSTOM_LOAN_PAYOUT with no lender name at all.
    by_type = (
        _mask(inf["txn_type"], lambda t: _matches(t, policy.loan_type_patterns) is not None)
        if getattr(caps, "txn_type", True) else _mask(inf["cp_name"], lambda n: False)
    )
    # Exclusion categories must be mutually exclusive or the monthly rows stop
    # summing to net revenue. Precedence: own money, borrowing, third party,
    # then size. An own-wallet transfer is never also a loan disbursement.
    inf["is_loan"] = (inf["lender_hit"].notna() | by_type) & ~inf["is_self_transfer"]
    inf["loan_cycle"] = False
    inf["loan_cycle_evidence"] = None
    # Bank / e-wallet credits: money arriving from banks, SACCOs, e-wallets,
    # remittance operators and system accounts. Not trade. Either the customer
    # is moving their own money in from another institution, or a third party
    # is settling on their behalf; neither is evidence that this account earns.
    # Reported as a what-if under `sensitivity` until now, and deducted from
    # here on.
    inf["is_institutional_credit"] = (
        (inf["is_institution"] & ~inf["is_self_transfer"] & ~inf["is_loan"])
        if policy.exclude_institutional_credits
        else _mask(inf["cp_name"], lambda n: False)
    )
    inf["is_large"] = (
        (inf["amount"] > policy.large_txn_threshold)
        & ~inf["is_self_transfer"] & ~inf["is_loan"] & ~inf["is_institutional_credit"]
    )
    return inf


# -------------------------------------------------------------- sections ----
def _loan_cycle_matches(df: pd.DataFrame, policy: Policy) -> Dict[str, Dict[str, Any]]:
    """Find conservative, pattern-based loan disbursements.

    A transaction is only promoted to a loan exclusion when the statement itself
    supplies a recognizable borrowing shape: a round principal inflow followed
    by round repayment outflows to the *same counterparty* inside the configured
    loan window.  One repayment is enough when it is explicitly labelled as a
    repayment/installment; otherwise two round repayments are required.

    This is still a pattern inference, not proof of a loan, so the evidence is
    surfaced in the report. It is deliberately conservative and never invents a
    lender name.
    """
    if df.empty:
        return {}

    repayment_words = ("loan", "repay", "repayment", "installment", "instalment", "emi", "credit")
    work = df.sort_values("dt")
    matches: Dict[str, Dict[str, Any]] = {}
    window = pd.Timedelta(days=policy.loan_cycle_days)

    inflows = work[
        work["is_inflow"]
        & (work["amount"] >= policy.loan_pattern_min_principal)
        & ((work["amount"] % policy.round_step) == 0)
    ]
    outflows = work[work["is_outflow"] & (work["amount"] > 0)]

    for row in inflows.itertuples():
        cp = row.cp_id
        if cp in (None, "", "N/A"):
            continue
        cp_name = row.cp_name or ""
        lender_like = (
            _matches(cp_name, policy.confirmed_lenders) is not None
            or _matches(cp_name, policy.suspected_lenders) is not None
            or _matches(cp_name, policy.lender_pattern_hints) is not None
            or _matches(row.txn_type, policy.loan_type_patterns) is not None
        )
        if not lender_like:
            continue
        later = outflows[
            (outflows["cp_id"] == cp)
            & (outflows["dt"] > row.dt)
            & (outflows["dt"] <= row.dt + window)
            & ((outflows["amount"] % policy.round_step) == 0)
        ]
        if later.empty:
            continue

        labelled = later[
            _mask(later["txn_type"], lambda t: _matches(t, repayment_words) is not None)
            | _mask(later["cp_name"], lambda n: _matches(n, repayment_words) is not None)
        ]
        # Two or more equal/round repayments are much stronger evidence of a
        # schedule than simply seeing repeated transfers to the same person.
        amount_counts = later["amount"].value_counts()
        repeated_round = later[later["amount"].isin(amount_counts[amount_counts >= 2].index)]
        qualifying = labelled if len(labelled) else repeated_round
        if qualifying.empty:
            continue

        repayment_total = _i(qualifying["amount"].sum())
        if repayment_total <= 0:
            continue
        repayment_ratio = repayment_total / float(row.amount)
        if repayment_ratio < policy.loan_pattern_min_repayment_ratio:
            continue
        if repayment_ratio > policy.loan_pattern_max_repayment_multiple:
            continue
        if len(labelled) == 0 and len(qualifying) < 2:
            continue

        matches[str(row.txn_id)] = {
            "txn_id": row.txn_id,
            "timestamp": row.timestamp,
            "counterparty": row.cp_name or row.cp_id,
            "principal": _i(row.amount),
            "repayment_total_within_window": repayment_total,
            "repayment_ratio": round(repayment_ratio, 4),
            "repayment_count": int(len(qualifying)),
            "loan_cycle_days": policy.loan_cycle_days,
            "basis": (
                "explicit repayment-labelled outflow to the same counterparty"
                if len(later) < 2 else
                "two or more round repayment outflows to the same counterparty"
            ),
            "repayments": [
                {
                    "txn_id": r.txn_id,
                    "timestamp": r.timestamp,
                    "amount": _i(r.amount),
                    "type": r.txn_type,
                }
                for r in qualifying.sort_values("dt").itertuples()
            ],
        }
    return matches


def _monthly_summary(inf: pd.DataFrame, months: List[str]) -> List[Dict[str, Any]]:
    rows = []
    for m in months:
        g = inf[inf["month"] == m]
        gross = _i(g["amount"].sum())
        # The masks were already made mutually exclusive in _classify_inflows,
        # so each category is summed on its own flag. Re-applying the
        # precedence here as well would be a second place to keep in step.
        selfs = _i(g.loc[g["is_self_transfer"], "amount"].sum())
        loans = _i(g.loc[g["is_loan"], "amount"].sum())
        institutional = _i(g.loc[g["is_institutional_credit"], "amount"].sum())
        large = _i(g.loc[g["is_large"], "amount"].sum())
        excl_n = _i(
            g["is_self_transfer"].sum() + g["is_loan"].sum()
            + g["is_institutional_credit"].sum() + g["is_large"].sum()
        )
        rows.append(
            {
                "month": m,
                "gross_inflows": gross,
                "self_transfers": selfs,
                "loan_inflows": loans,
                "institutional_credits": institutional,
                "large_transactions": large,
                "net_revenue": gross - selfs - loans - institutional - large,
                "inflow_count": int(len(g)),
                "counted_inflow_count": int(len(g)) - excl_n,
            }
        )
    return rows


def _period_days_by_month(df: pd.DataFrame, header) -> Dict[str, int]:
    """Days of each calendar month the statement period actually covers.

    Measuring the gap between the first and last transaction understates a
    quiet month: an account that trades on the 5th and the 20th would look
    like a 16-day partial month and be dropped from the run rate. What matters
    is how much of the month the statement claims to report on.
    """
    start = pd.to_datetime(getattr(header, "period_start", None), errors="coerce")
    end = pd.to_datetime(getattr(header, "period_end", None), errors="coerce")
    # Statements without a period header fall back to the span of the data, so a
    # 12-day extract is not mistaken for a whole month.
    if pd.isna(start):
        start = df["dt"].min()
    if pd.isna(end):
        end = df["dt"].max()

    out: Dict[str, int] = {}
    for m, g in df.groupby("month"):
        period = pd.Period(m, freq="M")
        lo = period.start_time if pd.isna(start) else max(period.start_time, start)
        hi = period.end_time if pd.isna(end) else min(period.end_time, end)
        if pd.isna(lo) or pd.isna(hi) or hi < lo:
            lo, hi = g["dt"].min(), g["dt"].max()
        out[m] = int((hi.normalize() - lo.normalize()).days) + 1
    return out


def _statement_freshness(header, policy: Policy) -> Dict[str, Any]:
    """Two document-validity checks credit policy states outright: the
    statement must cover at least a 90-day period and must have been
    generated recently - not merely happen to cover the right months.

    The period check is a FLOOR, not a band: a longer statement is more
    history, never a policy problem, so only a statement shorter than the
    target (minus any configured tolerance, 0 by default) fails it.

    Either check going `False` is a hard stop (see `analyze()`): no revenue
    or sanction figure is stated for a document that fails it. `None` means
    the format or file did not carry the date needed to check at all, which
    is reported rather than assumed passing.
    """
    start = pd.to_datetime(getattr(header, "period_start", None), errors="coerce")
    end = pd.to_datetime(getattr(header, "period_end", None), errors="coerce")
    generated = pd.to_datetime(getattr(header, "generated_at", None), errors="coerce")
    now = pd.Timestamp.now()

    period_days = int((end - start).days) + 1 if pd.notna(start) and pd.notna(end) else None
    # TEMPORARILY DISABLED: the 90-day minimum period requirement. The period
    # length is still computed and reported, but it no longer blocks revenue
    # or the sanction. The rule will be revised; restore the block below then.
    # period_ok = (
    #     period_days >= (policy.statement_period_target_days - policy.statement_period_tolerance_days)
    #     if period_days is not None else None
    # )
    period_ok = None

    generated_age_days = int((now - generated).days) if pd.notna(generated) else None
    generated_ok = (
        generated_age_days <= policy.statement_max_age_days
        if generated_age_days is not None else None
    )

    return {
        "period_start": getattr(header, "period_start", None),
        "period_end": getattr(header, "period_end", None),
        "period_days": period_days,
        "period_days_required": policy.statement_period_target_days,
        "period_tolerance_days": policy.statement_period_tolerance_days,
        "period_ok": period_ok,
        "generated_at": getattr(header, "generated_at", None),
        "generated_age_days": generated_age_days,
        "generated_max_age_days": policy.statement_max_age_days,
        "generated_ok": generated_ok,
        "blocking": (period_ok is False) or (generated_ok is False),
    }


def _freshness_block_reason(freshness: Dict[str, Any]) -> str:
    parts = []
    if freshness.get("period_ok") is False:
        tol = freshness.get("period_tolerance_days") or 0
        parts.append(
            f"it covers only {freshness['period_days']} days, short of the "
            f"{freshness['period_days_required']}-day period"
            + (f" (minus a {tol}-day tolerance)" if tol else "")
            + " credit policy requires"
        )
    if freshness.get("generated_ok") is False:
        parts.append(
            f"it was generated {freshness['generated_age_days']} days ago, "
            f"more than the {freshness['generated_max_age_days']}-day freshness limit"
        )
    return "; and ".join(parts)


def _concentration(inf: pd.DataFrame, months: List[str], policy: Policy, caps=None) -> List[Dict[str, Any]]:
    counted = inf[~inf["is_self_transfer"] & ~inf["is_loan"] & ~inf["is_large"]]
    out = []
    for m in months:
        all_month = inf[inf["month"] == m]
        g = counted[counted["month"] == m]
        net = float(g["amount"].sum())
        gross = float(all_month["amount"].sum())
        by_cp = (
            g.groupby(["cp_id", "cp_name"])["amount"].sum().sort_values(ascending=False)
        )
        top = [
            {
                "counterparty": name or ident,
                "msisdn": ident,
                "amount": _i(amt),
                "share": _share(amt, net),
            }
            for (ident, name), amt in by_cp.head(3).items()
        ]
        top1 = top[0]["share"] if top else 0.0
        top3 = round(sum(t["share"] for t in top), 4)

        # A separate concentration test keeps excluded large credits visible:
        # a month can be dominated by one payment even though that payment is
        # already removed from the base revenue calculation.
        largest = None
        largest_share = 0.0
        if len(all_month):
            r = all_month.sort_values("amount", ascending=False).iloc[0]
            largest_share = _share(float(r["amount"]), gross)
            largest = {
                "txn_id": r["txn_id"],
                "timestamp": r["timestamp"],
                "counterparty": r["cp_name"] or r["cp_id"],
                "amount": _i(r["amount"]),
                "share_of_gross": largest_share,
                "is_large_transaction": bool(r["is_large"]),
            }

        intraday = getattr(caps, "timestamp_precision", "second") != "day"
        by_hour = g.groupby(g["dt"].dt.floor("h"))["amount"].sum() if intraday else pd.Series(dtype=float)
        peak_hour_share = _share(by_hour.max(), net) if len(by_hour) else None

        reasons = []
        if top1 >= policy.concentration_single_share:
            reasons.append(
                f"{top[0]['counterparty']} alone is {top1:.1%} of the month's net."
            )
        if top3 >= policy.concentration_top3_share:
            reasons.append(f"Top three counterparties are {top3:.1%} of the month's net.")
        if largest and largest_share >= policy.concentration_single_txn_share:
            reasons.append(
                f"One payment of {largest['amount']:,} RWF is {largest_share:.1%} of gross inflow."
            )
        if peak_hour_share is not None and peak_hour_share >= 0.20:
            peak = by_hour.idxmax()
            reasons.append(
                f"{peak_hour_share:.1%} of the month's net arrived inside a single hour "
                f"({peak:%Y-%m-%d %H:00}) - one event, not sustained trade."
            )
        out.append(
            {
                "month": m,
                "net_revenue": _i(net),
                "gross_inflows": _i(gross),
                "top_counterparties": top,
                "top1_share": top1,
                "top3_share": top3,
                "largest_inflow": largest,
                "largest_inflow_share": largest_share,
                "peak_hour_share": peak_hour_share,
                "flagged": bool(reasons),
                "reasons": reasons,
            }
        )
    return out


def _loan_ledger(df: pd.DataFrame, policy: Policy) -> List[Dict[str, Any]]:
    hit = lambda n: _matches(n, policy.confirmed_lenders) is not None
    hits = df[
        _mask(df["from_name"], hit) | _mask(df["to_name"], hit)
        | _mask(df["from_msisdn"], hit) | _mask(df["to_msisdn"], hit)
    ]
    return [
        {
            "txn_id": r.txn_id,
            "timestamp": r.timestamp,
            "direction": "disbursement" if r.is_inflow else "repayment",
            "type": r.txn_type,
            "counterparty": (r.from_name if r.is_inflow else r.to_name) or (r.from_msisdn if r.is_inflow else r.to_msisdn),
            "amount": _i(r.amount),
        }
        for r in hits.sort_values("dt").itertuples()
    ]


def _bidirectional(df: pd.DataFrame) -> List[Dict[str, Any]]:
    ins = df[df["is_inflow"]].groupby("cp_id")["amount"].agg(["sum", "size"])
    outs = df[df["is_outflow"]].groupby("cp_id")["amount"].agg(["sum", "size"])
    both = ins.join(outs, lsuffix="_in", rsuffix="_out", how="inner")
    names = (
        df.groupby("cp_id")["cp_name"]
        .agg(lambda s: next((x for x in s if x), ""))
        .to_dict()
    )
    both = both.sort_values("sum_in", ascending=False)
    return [
        {
            "msisdn": idx,
            "counterparty": names.get(idx, "") or idx,
            "inflow_total": _i(r.sum_in),
            "inflow_count": int(r.size_in),
            "outflow_total": _i(r.sum_out),
            "outflow_count": int(r.size_out),
        }
        for idx, r in both.iterrows()
    ]


def _round_trips(df: pd.DataFrame, policy: Policy) -> List[Dict[str, Any]]:
    """Find rapid reversals, with same-counterparty evidence when available."""
    ok = df.sort_values("dt").reset_index(drop=True)
    window = pd.Timedelta(minutes=policy.roundtrip_window_minutes)
    hits = []
    for i, row in ok.iterrows():
        if not bool(row["is_inflow"]) or float(row["amount"]) < policy.roundtrip_min_amount:
            continue
        later = ok[
            (ok["dt"] > row["dt"])
            & (ok["dt"] <= row["dt"] + window)
            & (ok["is_outflow"])
        ]
        if later.empty:
            continue
        moved = float(later["amount"].sum())
        same_cp = later[later["cp_id"] == row["cp_id"]] if row["cp_id"] not in (None, "", "N/A") else later.iloc[0:0]
        same_cp_moved = float(same_cp["amount"].sum())
        if moved >= policy.roundtrip_ratio * float(row["amount"]):
            hits.append(
                {
                    "timestamp": row["timestamp"],
                    "counterparty": row["cp_name"] or row["cp_id"],
                    "inflow": _i(row["amount"]),
                    "outflow_within_window": _i(moved),
                    "outflow_count": int(len(later)),
                    "same_counterparty": bool(len(same_cp) and same_cp_moved >= policy.roundtrip_ratio * float(row["amount"])),
                    "same_counterparty_outflow": _i(same_cp_moved),
                    "same_counterparty_outflow_count": int(len(same_cp)),
                    "matched_outflows": [
                        {
                            "txn_id": r.txn_id,
                            "timestamp": r.timestamp,
                            "counterparty": r.cp_name or r.cp_id,
                            "amount": _i(r.amount),
                            "same_counterparty": r.cp_id == row["cp_id"],
                        }
                        for r in later.sort_values("dt").head(12).itertuples()
                    ],
                }
            )
    return hits


def _passthrough(df: pd.DataFrame, policy: Policy):
    daily = (
        df.groupby(["day", "is_inflow"])["amount"].sum().unstack(fill_value=0)
    )
    daily.columns = [{False: "outflow", True: "inflow"}.get(c, c) for c in daily.columns]
    for col in ("inflow", "outflow"):
        if col not in daily:
            daily[col] = 0
    daily["passthrough"] = daily[["inflow", "outflow"]].min(axis=1)
    flagged = daily[
        (daily["inflow"] >= policy.passthrough_day_min_inflow)
        & (daily["outflow"] >= policy.passthrough_day_ratio * daily["inflow"])
    ].sort_values("inflow", ascending=False)
    return daily, flagged


def _monthly_flow(df: pd.DataFrame, months: List[str]) -> List[Dict[str, Any]]:
    out = []
    for m in months:
        g = df[df["month"] == m]
        gin = _i(g.loc[g["is_inflow"], "amount"].sum())
        gout = _i(g.loc[g["is_outflow"], "amount"].sum())
        out.append(
            {
                "month": m,
                "inflow": gin,
                "outflow": gout,
                "retained": gin - gout,
                "passthrough_ratio": _share(min(gin, gout), gin),
            }
        )
    return out


# ----------------------------------------------------- product eligibility ----
def _loan_eligibility(summary: List[Dict[str, Any]], policy: Policy, monthly_emi: int | float | None,
                      requested_amount: int | float | None = None,
                      customer_type: str | None = None) -> Dict[str, Any]:
    """Apply the statement-side product eligibility rule without inference."""
    months = [r for r in summary if r.get("gross_inflows") is not None]
    target = policy.eligibility_months

    # New and existing customers share every step except the two rates and the
    # sanction table. An unrecognised type gets no figure rather than being
    # priced as one of the two.
    ctype = (customer_type or "new").strip().lower()
    if ctype == "new":
        net_revenue_rate = policy.net_revenue_rate
        fee_cap_rate = policy.fee_cap_rate
        bands = policy.loan_eligibility_bands
    elif ctype == "existing":
        net_revenue_rate = policy.existing_net_revenue_rate
        fee_cap_rate = policy.existing_fee_cap_rate
        bands = policy.existing_loan_eligibility_bands
    else:
        return {
            "status": "not_assessed",
            "reason": f"Unknown customer type '{ctype}'. Expected 'new' or 'existing'.",
            "customer_type": ctype,
            "months_required": target,
            "months_used": [r["month"] for r in months],
        }

    if len(months) < target:
        return {
            "status": "not_assessed",
            "reason": f"At least {target} statement months are required; only {len(months)} are available.",
            "months_required": target,
            "months_used": [r["month"] for r in months],
            "monthly_emi": monthly_emi,
            "monthly_emi_source": "ongoing_crb_emi" if monthly_emi is not None else None,
            "monthly_emi_scope": "open_performing_accounts_only",
        }

    # Every month is put on the same 30-day footing before averaging.
    #
    # Calendar months are 28-31 days and a statement's first and last months are
    # usually partial, so raw monthly totals are not comparable to each other.
    # Averaging them raw let a 10-day stub drag the mean down as though trade
    # had collapsed. Each month is therefore scaled to a 30-day equivalent:
    #     net_30d = net_revenue / days_covered * 30
    #
    # This is an extrapolation on a partial month, and a short window
    # extrapolates badly - ten days of takings is thin evidence for a month of
    # them. Any month covering fewer than `min_days_for_run_rate` days is
    # therefore reported but excluded from the average, and the console names it.
    # Pick the last `target` months that can actually be used, rather than
    # taking the last `target` calendar months and then discarding the unusable
    # ones. Filtering after the window is fixed silently shrinks the average's
    # base: on a May-August statement the window was Jun/Jul/Aug, August was
    # dropped as too short, and the figure was struck on two months while May -
    # a perfectly usable 20-day month sitting right there - was never
    # considered.
    usable = [r for r in months
              if (r.get("days_covered") or 0) >= policy.min_days_for_run_rate]
    thin = [r for r in months if r not in usable]
    used = usable[-target:]
    if not used:
        return {
            "status": "not_assessed",
            "reason": (
                "Every month in the assessment window covers fewer than "
                f"{policy.min_days_for_run_rate} days, which is too short to "
                "extrapolate a monthly figure from. Request a longer statement."
            ),
            "months_required": target,
            "months_used": [],
            "months_excluded_as_too_short": [r["month"] for r in thin],
            "monthly_emi": monthly_emi,
            "monthly_emi_source": "ongoing_crb_emi" if monthly_emi is not None else None,
            "monthly_emi_scope": "open_performing_accounts_only",
        }

    def _normalised(row: Dict[str, Any]) -> int:
        """The month's net revenue on a 30-day footing, rounded once.

        Rounded here rather than at display time so the average is the mean of
        exactly the numbers printed in the months table. Averaging the
        unrounded values and rounding afterwards left the column and its total
        a rupee apart.
        """
        days = row.get("days_covered") or 30
        return round((row["net_revenue"] / days) * 30) if days else 0

    partial_excluded = [r["month"] for r in months if not r.get("is_full_month")]
    thin_excluded = [r["month"] for r in thin]

    # Every month the engine looked at, and what became of it. The average is
    # struck on a subset, and which months fell out - and why - is not
    # something an underwriter should have to infer from a shorter table.
    considered = []
    for row in months:
        days = row.get("days_covered") or 0
        if row in used:
            status, why = "averaged", None
        elif days < policy.min_days_for_run_rate:
            status = "excluded_too_short"
            why = (f"Covers {days} days, so standing it up as a month means "
                   f"multiplying by {30 / days:.1f}x. The floor is "
                   f"{policy.min_days_for_run_rate} days (half a month), where the "
                   f"multiplier is exactly 2.0x - the point at which the observed "
                   f"period is still at least as long as the part being inferred. "
                   f"Past that the figure is mostly arithmetic.")
        else:
            status = "not_in_window"
            why = (f"Usable, but the average is struck on the most recent "
                   f"{target} usable months and this one falls outside them.")
        considered.append({
            "month": row["month"],
            "days_covered": days,
            "is_full_month": row.get("is_full_month"),
            "net_revenue": row["net_revenue"],
            "net_revenue_30d": _normalised(row) if days else None,
            "scale_factor": round(30 / days, 2) if days else None,
            "status": status,
            "reason": why,
        })
    # Eligibility is based on the statement's already-normalized monthly net
    # revenue, not gross credits. This means self-transfers, loan inflows and
    # large transactions have already been removed before the CRB EMI is
    # deducted for affordability.
    avg_statement_net_revenue = sum(_normalised(r) for r in used) / len(used)
    base = {
        "months_required": target,
        "months_used": [r["month"] for r in used],
        "months_averaged": len(used),
        "months_short_of_target": max(target - len(used), 0),
        "basis": "30_day_normalised",
        "basis_note": (
            "Each month is scaled to a 30-day equivalent (net revenue ÷ days covered "
            "× 30) so months of different lengths are comparable, then averaged."
        ),
        "monthly_inflows": [
            {
                "month": r["month"],
                "gross_inflow": r["gross_inflows"],
                "net_revenue": r["net_revenue"],
                "days_covered": r.get("days_covered"),
                "is_full_month": r.get("is_full_month"),
                "net_revenue_30d": _normalised(r),
            }
            for r in used
        ],
        "average_monthly_inflow": round(avg_statement_net_revenue),
        "average_monthly_net_revenue": round(avg_statement_net_revenue),
        "partial_months_normalised": [
            r["month"] for r in used if not r.get("is_full_month")
        ],
        "partial_months_in_statement": partial_excluded,
        "months_excluded_as_too_short": thin_excluded,
        "min_days_for_run_rate": policy.min_days_for_run_rate,
        "min_days_rationale": (
            f"A month is scaled to 30 days by multiplying by 30 ÷ days covered. The "
            f"{policy.min_days_for_run_rate}-day floor is set by that multiplier, not by "
            f"the day count: at {policy.min_days_for_run_rate} days it is "
            f"{30 / policy.min_days_for_run_rate:.1f}x, so no month is averaged on the "
            f"strength of more than doubling it. At 10 days it would be 3.0x and at 5 "
            f"days 6.0x, where the result is mostly arithmetic rather than observed "
            f"trade."
        ),
        "max_scale_factor": round(30 / policy.min_days_for_run_rate, 2),
        "months_considered": considered,
        "requested_amount": round(float(requested_amount)) if requested_amount is not None else None,
    }
    if monthly_emi is None:
        return {**base, "status": "not_assessed",
                "reason": "Complete monthly EMI data is required from the CRB before loan eligibility can be calculated.",
                "monthly_emi": None,
                "monthly_emi_source": None,
                "monthly_emi_scope": "open_performing_accounts_only"}

    # Each figure is derived from the ROUNDED value shown for the step above it,
    # so the chain on screen is the chain that was computed. Carrying unrounded
    # values forward and rounding only for display puts consecutive lines a
    # rupee apart and makes the report look wrong when it is merely inconsistent.
    emi = round(max(float(monthly_emi), 0.0))
    net_inflow = round(avg_statement_net_revenue) - emi
    net_revenue = round(max(net_inflow, 0.0) * net_revenue_rate)
    fee_cap = round(net_revenue * fee_cap_rate)

    # Bounds are inclusive at both ends, and bands are tried in order so any
    # overlap in a configured table resolves to the lower sanction.
    # A band with `max_fee_cap` of None has no upper bound. If the top band is
    # bounded, a fee cap above it is outside policy and refers, rather than
    # silently sanctioning the largest product on offer.
    matches = [b for b in bands
               if b["min_fee_cap"] <= fee_cap
               and (b["max_fee_cap"] is None or fee_cap <= b["max_fee_cap"])]
    band = matches[0] if matches else None
    overlap = len(matches) > 1

    floor, ceiling = bands[0]["min_fee_cap"], bands[-1]["max_fee_cap"]
    if band is None:
        if fee_cap < floor:
            reason = (f"Fee cap of {fee_cap:,.0f} RWF is below the {floor:,} RWF minimum "
                      "eligibility band.")
        elif ceiling is not None and fee_cap > ceiling:
            reason = (f"Fee cap of {fee_cap:,.0f} RWF is above the {ceiling:,} RWF top of "
                      "the sanction table. Policy does not define a product here - refer "
                      "to a credit officer rather than sanctioning the largest band.")
        else:
            reason = (f"Fee cap of {fee_cap:,.0f} RWF falls between two configured "
                      "eligibility bands.")
    else:
        reason = None
    # Two distinct numbers, kept apart. The eligible amount is what the fee cap
    # band supports. The sanctioned amount is what is actually offered: the
    # lower of that and what the customer asked for. Asking for less than you
    # qualify for is honoured; asking for more is capped at the band.
    eligible = band["loan_amount"] if band else None
    req = round(float(requested_amount)) if requested_amount is not None else None
    if eligible is None:
        sanctioned = None
        sanction_basis = "not_eligible"
    elif req is None:
        sanctioned = eligible
        sanction_basis = "eligible_amount_no_request"
    elif req <= eligible:
        sanctioned = req
        sanction_basis = "requested_amount"
    else:
        sanctioned = eligible
        sanction_basis = "eligible_amount_capped"

    return {**base,
        "status": "eligible" if band else "not_eligible",
        "customer_type": ctype,
        "monthly_emi": emi,
        "monthly_emi_source": "ongoing_crb_emi",
        "monthly_emi_scope": "open_performing_accounts_only",
        "net_inflow": net_inflow,
        "net_revenue_rate": net_revenue_rate,
        "net_revenue": net_revenue,
        "fee_cap_rate": fee_cap_rate,
        "fee_cap": fee_cap,
        "band_overlap_resolved": overlap,
        "band_overlap_note": (
            f"Fee cap {fee_cap:,.0f} RWF falls in {len(matches)} overlapping policy "
            f"bands; the lower sanction of {band['loan_amount']:,} RWF was applied."
        ) if overlap and band else None,
        "fee_cap_floor": floor,
        "fee_cap_ceiling": ceiling,
        "loan_amount_eligibility": eligible,
        "configured_loan_amount": eligible,
        "sanctioned_amount": sanctioned,
        "sanction_basis": sanction_basis,
        "sanction_note": (
            None if sanctioned is None else
            f"Customer requested {req:,} RWF against an eligible {eligible:,} RWF; "
            f"the lower figure is sanctioned." if req is not None and req != eligible
            else (f"Requested amount matches the eligible amount." if req is not None
                  else "No requested amount was entered, so the full eligible amount is shown.")
        ),
        "requested_amount": req,
        "requested_amount_cap_applied": bool(band and req is not None and req > eligible),
        "matched_band": band,
        "reason": reason,
        "bands": list(bands),
    }

def _reconciliation(summary: List[Dict[str, Any]], totals: Dict[str, Any],
                    eligibility: Dict[str, Any], disclosures: Dict[str, Any],
                    revenue_assessed: bool = True) -> Dict[str, Any]:
    """Re-derive every published figure from its own rows and report the result.

    Not a substitute for the tests: this runs on the real statement in front of
    the underwriter and puts the arithmetic on the page, so a figure can be
    trusted without re-adding it by hand. Any check that fails is a bug, and the
    console says so rather than presenting the number as final.
    """
    checks: List[Dict[str, Any]] = []

    def check(name: str, left: Any, right: Any, detail: str = "") -> None:
        ok = (left is None and right is None) or (
            left is not None and right is not None and abs(left - right) < 1
        )
        checks.append({"check": name, "computed": left, "expected": right,
                       "passes": ok, "detail": detail})

    for row in summary:
        if row.get("net_revenue") is None:
            continue
        check(
            f"{row['month']} rows foot to net revenue",
            row["net_revenue"],
            row["gross_inflows"] - row["self_transfers"] - row["loan_inflows"]
            - row["institutional_credits"] - row["large_transactions"],
            "gross - self - loans - bank/e-wallet - large",
        )

    for key in ("gross_inflows", "self_transfers", "loan_inflows",
                "institutional_credits", "large_transactions", "net_revenue"):
        rows_total = None
        if all(r.get(key) is not None for r in summary):
            rows_total = sum(r[key] for r in summary)
        check(f"total {key.replace('_', ' ')} equals the sum of months",
              totals.get(key), rows_total)

    counted = disclosures.get("counted_inflow_count")
    inflow = disclosures.get("inflow_count")
    if counted is not None and inflow is not None:
        checks.append({
            "check": "counted inflows do not exceed total inflows",
            "computed": counted, "expected": inflow,
            "passes": counted <= inflow,
            "detail": "inflows remaining after exclusions",
        })

    if eligibility.get("status") in ("eligible", "not_eligible"):
        rows = eligibility.get("monthly_inflows") or []
        if rows:
            # The average is taken over the 30-day normalised figures, not the
            # raw month totals, so it must be re-derived from the same column
            # the table displays. Checking it against raw net revenue was
            # comparing two different quantities and failing correctly.
            for r in rows:
                days = r.get("days_covered")
                if days:
                    check(f"{r['month']} scaled to 30 days",
                          r.get("net_revenue_30d"),
                          round(r["net_revenue"] / days * 30),
                          f"{r['net_revenue']:,} over {days} days")
            check("eligibility average equals the months it lists",
                  eligibility.get("average_monthly_net_revenue"),
                  round(sum(r["net_revenue_30d"] for r in rows) / len(rows)),
                  "mean of the 30-day figures")
        check("net inflow equals average less EMI",
              eligibility.get("net_inflow"),
              (eligibility.get("average_monthly_net_revenue") or 0)
              - (eligibility.get("monthly_emi") or 0))

    # Consistency BETWEEN blocks, not just inside one. Every check above
    # verifies that a figure foots to its own rows; this one catches two blocks
    # that are each internally correct and contradict each other. The float
    # account was exactly that: no revenue figure stated, full sanction offered.
    checks.append({
        "check": "no sanction is offered without a revenue figure",
        "computed": eligibility.get("sanctioned_amount"),
        "expected": None if not revenue_assessed else eligibility.get("sanctioned_amount"),
        "passes": revenue_assessed or eligibility.get("sanctioned_amount") is None,
        "detail": "a loan amount may not outlive the revenue it was derived from",
    })

    failed = [c for c in checks if not c["passes"]]
    return {
        "all_figures_reconcile": not failed,
        "checks_run": len(checks),
        "checks_failed": len(failed),
        "failures": failed,
        "note": ("Every published figure was re-derived from the rows beneath it and "
                 "matched." if not failed else
                 "One or more figures do not agree with the rows they are built from. "
                 "Do not use these numbers until this is resolved."),
        "checks": checks,
    }


def _edge_cases(statement, summary, totals, eligibility, disclosures, details,
                notes, float_type, revenue_blocked, failed_count, policy,
                freshness=None) -> List[Dict[str, Any]]:
    """Everything an underwriter must see before trusting the headline figure.

    These are scattered across the response by design - each lives next to the
    figure it qualifies. Collected here so none of them can be missed by
    scrolling past the section that carries it.
    """
    items: List[Dict[str, Any]] = []

    def add(code, severity, title, detail, evidence=None):
        items.append({"code": code, "severity": severity, "title": title,
                      "detail": detail, "evidence": evidence or {}})

    if freshness and freshness.get("blocking"):
        add("STATEMENT_OUT_OF_SPEC", "high", "Statement does not meet document policy",
            f"No revenue or sanction figure is stated because {_freshness_block_reason(freshness)}. "
            "Request a document that meets both requirements.",
            {"period_start": freshness.get("period_start"), "period_end": freshness.get("period_end"),
             "period_days": freshness.get("period_days"),
             "period_days_required": freshness.get("period_days_required"),
             "generated_at": freshness.get("generated_at"),
             "generated_age_days": freshness.get("generated_age_days"),
             "generated_max_age_days": freshness.get("generated_max_age_days")})
    # The period half of this check is disabled along with the 90-day rule in
    # _statement_freshness; restore `freshness.get("period_ok") is None or` here
    # and the period wording below when that rule comes back.
    elif freshness and freshness.get("generated_ok") is None:
        add("STATEMENT_FRESHNESS_NOT_ASSESSED", "low", "Document freshness not verifiable",
            "This statement format does not carry enough date information to confirm the "
            f"{policy.statement_max_age_days}-day generation-freshness requirement. Confirm "
            "it by hand before relying on this figure.",
            {"period_days": freshness.get("period_days"),
             "generated_age_days": freshness.get("generated_age_days")})

    partial = [r["month"] for r in summary if not r.get("is_full_month")]
    if partial:
        add("PARTIAL_MONTHS", "medium", "Statement contains partial months",
            f"{', '.join(partial)} cover only part of a calendar month. Each month is "
            "scaled to a 30-day equivalent before averaging so month lengths are "
            "comparable, which means a partial month is extrapolated rather than "
            "taken at face value.",
            {"partial_months": [
                {"month": r["month"], "days_covered": r["days_covered"],
                 "gross_inflows": r["gross_inflows"]}
                for r in summary if not r.get("is_full_month")]})

    if eligibility.get("months_short_of_target"):
        add("FEWER_MONTHS_THAN_POLICY", "high", "Averaged on fewer months than policy asks for",
            f"Credit policy asks for {eligibility.get('months_required')} months and only "
            f"{eligibility.get('months_averaged')} could be used "
            f"({', '.join(eligibility.get('months_used') or []) or 'none'}). A shorter base "
            "makes the average more sensitive to a single good or bad month.",
            {"months_used": eligibility.get("months_used"),
             "months_required": eligibility.get("months_required")})

    if eligibility.get("months_excluded_as_too_short"):
        short = [m for m in (eligibility.get("months_considered") or [])
                 if m["status"] == "excluded_too_short"]
        detail = ", ".join(
            f"{m['month']} covers {m['days_covered']} days and would need a "
            f"{m['scale_factor']}x scale-up" for m in short
        ) or ", ".join(eligibility["months_excluded_as_too_short"])
        add("MONTH_TOO_SHORT", "high", "A month was too short to extrapolate",
            f"{detail}. " + (eligibility.get("min_days_rationale") or "") +
            " Configurable as min_days_for_run_rate.",
            {"months": short or eligibility["months_excluded_as_too_short"],
             "floor_days": eligibility.get("min_days_for_run_rate"),
             "max_scale_factor": eligibility.get("max_scale_factor")})

    if eligibility.get("partial_months_normalised"):
        add("MONTHS_NORMALISED", "medium", "Partial months scaled to 30 days",
            f"{', '.join(eligibility['partial_months_normalised'])} were extrapolated to "
            "a 30-day equivalent to sit alongside the full months. The scaled figure is "
            "an estimate of a whole month, not an observed one.",
            {"months": [m for m in (eligibility.get("monthly_inflows") or [])
                        if not m.get("is_full_month")]})

    if eligibility.get("status") == "not_assessed":
        add("ELIGIBILITY_NOT_ASSESSED", "high", "No sanction figure could be produced",
            eligibility.get("reason") or "Eligibility could not be assessed.",
            {"complete_months_available": eligibility.get("complete_months_available"),
             "months_required": eligibility.get("months_required")})

    if revenue_blocked:
        add("REVENUE_NOT_STATED", "high", "No revenue figure is stated",
            f"The account type {float_type!r} is an agent float account, not an income "
            "document. No net revenue and no sanction can be derived from it.",
            {"account_type": float_type})

    if not statement.parse_complete:
        add("PARSE_INCOMPLETE", "high", "Not every declared row was recovered",
            f"The statement declares {statement.header.declared_txn_count} transactions "
            f"and {len(statement.transactions)} were parsed. Reconcile before using any "
            "figure on this page.",
            {"declared": statement.header.declared_txn_count,
             "parsed": len(statement.transactions),
             "rejected": len(getattr(statement, "rejected_rows", []))})

    st = details.get("self_transfers", {})
    if st.get("by_basis", {}).get("own_wallet_product"):
        b = st["by_basis"]["own_wallet_product"]
        add("OWN_WALLET_EXCLUDED", "medium", "Own savings-wallet inflows excluded",
            f"{b['count']} inflows totalling {b['total']:,} RWF arrived from a named "
            "own-wallet product (for example MoKash Savings). These are the customer "
            "moving their own money and are deducted as self-transfers, not counted as "
            "revenue and not counted as borrowing.",
            {"count": b["count"], "total": b["total"]})

    if st.get("possible_for_review"):
        add("NEAR_NAME_MATCH", "medium", "Possible self-transfers not deducted",
            f"{len(st['possible_for_review'])} inflows come from a sender whose name "
            "partly matches the account holder. The engine does not guess: they remain "
            "in net revenue and need a human decision.",
            {"rows": st["possible_for_review"][:10]})

    loans = details.get("loan_inflows", {})
    if loans.get("suspected_not_deducted"):
        add("SUSPECTED_NOT_DEDUCTED", "medium", "Institutions on both sides of the ledger",
            "These counterparties send and receive money and could be a loan, a savings "
            "account or the customer's own current account. They are reported and NOT "
            "deducted as borrowing, though a bank or e-wallet among them is deducted "
            "as an institutional credit.",
            {"counterparties": loans["suspected_not_deducted"]})

    if loans.get("pattern_matches"):
        add("LOAN_BY_PATTERN", "medium", "Borrowing inferred from repayment shape",
            f"{len(loans['pattern_matches'])} inflows were treated as loans on a "
            "disbursement/repayment pattern rather than a named lender. This is an "
            "inference, and the supporting rows are shown.",
            {"matches": loans["pattern_matches"][:10]})

    mtc = disclosures.get("minimum_transaction_check", {})
    if mtc.get("status") == "not_assessed":
        add("NO_MIN_TXN_THRESHOLD", "low", "Minimum-transaction check not configured",
            "Credit policy defines no minimum inflow count, so no pass or fail is "
            "stated. The observed count is reported instead.",
            {"observed_inflow_count": mtc.get("observed_inflow_count")})

    for note in (notes or []):
        add("CHECK_NOT_POSSIBLE", "medium", "A check could not run on this format",
            note if isinstance(note, str) else str(note))

    if failed_count:
        add("FAILED_TRANSACTIONS", "low", "Failed transactions present",
            f"{failed_count} rows carry a non-successful status and are excluded from "
            "every figure. They are not evidence of revenue or of borrowing.",
            {"failed_count": failed_count})

    if disclosures.get("unattributed_count"):
        add("UNATTRIBUTED", "medium", "Rows attributable to neither party",
            f"{disclosures['unattributed_count']} rows could not be assigned to either "
            "side of the account and are counted separately rather than dropped.",
            {"count": disclosures["unattributed_count"],
             "total": disclosures.get("unattributed_total")})

    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(items, key=lambda x: order.get(x["severity"], 3))


# ------------------------------------------------------------------ main ----
def analyze(statement: ParsedStatement, policy: Policy, monthly_emi: int | float | None = None,
            requested_amount: int | float | None = None,
            customer_type: str | None = None) -> Dict[str, Any]:
    account = statement.header.msisdn or ""
    holder = statement.header.customer or ""
    caps = getattr(statement, "capabilities", None)
    notes = _capability_notes(statement, policy)
    float_type = _float_account(statement, policy)

    raw = _frame(statement, account)
    failed = raw[raw["status"] != "SUCCESSFUL"]
    df = raw[raw["status"] == "SUCCESSFUL"].copy()
    unattributed = df[df["is_unattributed"]]

    inf = _classify_inflows(df[df["is_inflow"]], holder, policy, caps, account)
    loan_cycles = _loan_cycle_matches(df, policy)
    if loan_cycles and len(inf):
        inf["loan_cycle"] = inf["txn_id"].astype(str).map(lambda x: x in loan_cycles)
        inf["loan_cycle_evidence"] = inf["txn_id"].astype(str).map(loan_cycles)
        # Precedence again: a pattern match must not pull an own-money transfer
        # into the loan bucket, or the row would be deducted twice.
        inf["is_loan"] = (inf["is_loan"] | inf["loan_cycle"]) & ~inf["is_self_transfer"]
        inf["is_large"] = inf["is_large"] & ~inf["is_loan"]
    outf = df[df["is_outflow"]].copy()

    months = sorted(df["month"].unique())
    days_by_month = _period_days_by_month(df, statement.header)
    summary = _monthly_summary(inf, months)
    for row in summary:
        row["days_covered"] = days_by_month.get(row["month"], 0)
        row["is_full_month"] = row["days_covered"] >= policy.full_month_min_days

    loan_eligibility = _loan_eligibility(summary, policy, monthly_emi, requested_amount, customer_type)

    totals = {
        k: _i(sum(r[k] for r in summary))
        for k in (
            "gross_inflows",
            "self_transfers",
            "institutional_credits",
            "loan_inflows",
            "large_transactions",
            "net_revenue",
        )
    }
    totals["inflow_count"] = int(sum(r["inflow_count"] for r in summary))
    totals["counted_inflow_count"] = int(sum(r["counted_inflow_count"] for r in summary))

    full = [r for r in summary if r["is_full_month"]]
    run_rate = _i(sum(r["net_revenue"] for r in full) / len(full)) if full else None

    # Bank / e-wallet credits are deducted in the monthly rows above. Where the
    # format carries no counterparty name they cannot be identified at all, and
    # that has to be said rather than reported as a clean zero.
    institutional_identifiable = not (caps is not None and not caps.counterparty_name)

    # ---- detail tables ----------------------------------------------------
    def _rows(frame: pd.DataFrame) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for r in frame.sort_values("dt", ascending=False).itertuples():
            row = {
                "txn_id": r.txn_id,
                "timestamp": r.timestamp,
                "type": r.txn_type,
                "counterparty": r.cp_name or r.cp_id,
                "msisdn": r.cp_id,
                "amount": _i(r.amount),
                "fee": _i(r.fee) if pd.notna(getattr(r, "fee", None)) else None,
                "balance_after": _i(r.balance_after) if pd.notna(getattr(r, "balance_after", None)) else None,
                "month": r.month,
            }
            # Why this row was deducted. Every excluded amount must be able to
            # answer that on its own line, not only in a section heading.
            basis = getattr(r, "self_transfer_basis", "") or ""
            if basis:
                row["basis"] = basis
            lender = getattr(r, "lender_hit", None)
            if lender:
                row["matched_lender"] = lender
            if getattr(r, "loan_cycle", False):
                row["basis"] = "repayment_pattern"
                row["pattern_evidence"] = getattr(r, "loan_cycle_evidence", None)
            out.append(row)
        return out

    bidir = _bidirectional(df)
    possible_self_rows = inf[inf["is_possible_self"] & ~inf["is_self_transfer"]].copy()
    possible_self = [
        {
            "txn_id": r.txn_id,
            "timestamp": r.timestamp,
            "counterparty": r.cp_name or r.cp_id,
            "msisdn": r.cp_id,
            "amount": _i(r.amount),
            "month": r.month,
            "match_basis": "partial_name_match",
            "reason": "One or more holder-name tokens matched, but the full strict name did not.",
        }
        for r in possible_self_rows.sort_values("dt", ascending=False).itertuples()
    ]

    large_in = inf[inf["is_large"] & ~inf["is_loan"] & ~inf["is_self_transfer"]]
    large_out = outf[outf["amount"] > policy.large_txn_threshold]

    details = {
        "self_transfers": {
            "deducted": _rows(inf[inf["is_self_transfer"]]),
            # Kept under the old key so an existing consumer does not silently
            # get an empty list; the rows are no longer all name matches.
            "strict_name_match": _rows(inf[inf["is_self_transfer"] & (inf["self_transfer_basis"] == "name")]),
            "by_basis": {
                basis: {
                    "count": int((inf["self_transfer_basis"] == basis).sum()),
                    "total": _i(inf[inf["self_transfer_basis"] == basis]["amount"].sum()),
                }
                for basis in ("name", "identity", "own_wallet_product")
                if (inf["self_transfer_basis"] == basis).any()
            } if len(inf) else {},
            "possible_for_review": possible_self,
            "note": "Own money, deducted on one of three bases: the sender name matches "
                    "the account holder; the sender IS this account; or the sender is a "
                    "named own-wallet product such as MoKash Savings, which cannot belong "
                    "to a third party. Near name matches are listed for review and are "
                    "NOT deducted. Credits from banks, SACCOs and e-wallets are a "
                    "separate exclusion of their own.",
        },
        "loan_inflows": {
            "items": _rows(inf[inf["is_loan"]]),
            "ledger": _loan_ledger(df, policy),
            "pattern_matches": [v for v in loan_cycles.values()],
            "suspected_not_deducted": [
                {
                    "counterparty": b["counterparty"],
                    "msisdn": b["msisdn"],
                    "inflow_total": b["inflow_total"],
                    "outflow_total": b["outflow_total"],
                    "net_paid_out": b["outflow_total"] - b["inflow_total"],
                }
                for b in bidir
                if _matches(b["counterparty"], policy.suspected_lenders)
            ],
        },
        "institutional_credits": {
            "items": _rows(inf[inf["is_institutional_credit"]]),
            "identifiable": institutional_identifiable,
            "note": "Credits from banks, SACCOs, e-wallets, remittance operators and "
                    "system accounts. Deducted from net revenue: either the customer is "
                    "moving their own money in from another institution, or a third "
                    "party is settling on their behalf. Neither is evidence that this "
                    "account earns."
                    if institutional_identifiable else
                    "This format carries no counterparty names, so bank and e-wallet "
                    "credits cannot be identified and none have been deducted. Net "
                    "revenue is overstated by whatever they amount to.",
        },
        "large_transactions": {
            "inflows": _rows(large_in),
            "outflows": [
                {
                    "txn_id": r.txn_id,
                    "timestamp": r.timestamp,
                    "type": r.txn_type,
                    "counterparty": r.cp_name or r.cp_id,
                    "msisdn": r.cp_id,
                    "amount": _i(r.amount),
                    "month": r.month,
                }
                for r in large_out.sort_values("dt", ascending=False).itertuples()
            ],
        },
        "bidirectional_counterparties": bidir[:40],
    }

    # ---- disclosures ------------------------------------------------------
    senders = inf.groupby("cp_id")["cp_name"].first()
    individual_ids = [
        i
        for i in senders.index
        if not _matches(senders[i], policy.institution_patterns)
        and not _matches(senders[i], policy.merchant_patterns)
    ]
    monthly_senders = (
        inf[inf["cp_id"].isin(individual_ids)]
        .groupby("month")["cp_id"]
        .nunique()
        .to_dict()
    )

    observed = int(len(inf))
    if policy.min_inflow_count is None:
        min_check = {
            "observed_inflow_count": observed,
            "observed_total_count": int(len(raw)),
            "active_days": int(df.loc[df["is_inflow"], "day"].nunique()),
            "threshold": None,
            "status": "not_assessed",
            "note": "No minimum-transaction threshold is configured in credit policy, "
                    "so no pass or fail can be stated. Set min_inflow_count to enable.",
        }
    else:
        min_check = {
            "observed_inflow_count": observed,
            "observed_total_count": int(len(raw)),
            "active_days": int(df.loc[df["is_inflow"], "day"].nunique()),
            "threshold": policy.min_inflow_count,
            "status": "pass" if observed >= policy.min_inflow_count else "fail",
            "note": None,
        }

    names_available = caps is None or caps.counterparty_name
    disclosures = {
        "total_transaction_count": int(len(raw)),
        "unattributed_count": int(len(unattributed)),
        "unattributed_total": _i(unattributed["amount"].sum()) if len(unattributed) else 0,
        "declared_transaction_count": statement.header.declared_txn_count,
        "parse_complete": statement.parse_complete,
        "successful_count": int(len(df)),
        "failed_count": int(len(failed)),
        "inflow_count": int(len(inf)),
        "outflow_count": int(len(outf)),
        "counted_inflow_count": totals["counted_inflow_count"],
        "large_transaction_count": int(len(large_in) + len(large_out)),
        "large_inflow_count": int(len(large_in)),
        "large_outflow_count": int(len(large_out)),
        "distinct_senders_total": int(inf["cp_id"].nunique()),
        "distinct_individual_senders": len(individual_ids) if names_available else None,
        "distinct_institution_senders": (
            int(inf["cp_id"].nunique()) - len(individual_ids) if names_available else None
        ),
        "distinct_individual_senders_by_month": (
            {k: int(v) for k, v in monthly_senders.items()} if names_available else {}
        ),
        "distinct_sender_basis": getattr(caps, "cp_id_basis", "msisdn"),
        "minimum_transaction_check": min_check,
        "concentration": _concentration(inf, months, policy, caps),
        "capability_notes": notes,
    }

    # ---- red flags --------------------------------------------------------
    flags = _red_flags(df, inf, outf, failed, months, policy, statement, caps)

    freshness = _statement_freshness(statement.header, policy)
    float_blocked = bool(float_type) and policy.block_revenue_on_float_accounts
    freshness_blocked = bool(freshness.get("blocking"))
    revenue_blocked = float_blocked or freshness_blocked
    if float_type:
        flags.insert(0, {
            "code": "FLOAT_ACCOUNT",
            "title": "Not an income document",
            "severity": "severe",
            "metric": f"Account type is {float_type!r}",
            "summary": (
                "The statement header identifies this as an agent float account. Such an "
                "account exists to hold operating float, and its credits are overwhelmingly "
                "the holder funding his own float from his own other accounts, not trade "
                "revenue. "
                + ("No revenue figure is stated for it. " if float_blocked else
                   "A revenue figure is stated because policy allows it, but it is not "
                   "evidence of income. ")
                + "Assess income from the matching commission account or a personal wallet."
            ),
            "evidence": {
                "account_type": float_type,
                "gross_credits": _i(inf["amount"].sum()),
                "revenue_computation_blocked": float_blocked,
            },
        })

    if freshness_blocked:
        flags.insert(0, {
            "code": "STATEMENT_OUT_OF_SPEC",
            "title": "Statement does not meet document policy",
            "severity": "severe",
            "metric": f"Period {freshness.get('period_days')} days · generated "
                      f"{freshness.get('generated_age_days')} days ago",
            "summary": (
                f"No revenue figure is stated because {_freshness_block_reason(freshness)}. "
                "Request a document that meets both requirements before this statement is "
                "underwritten."
            ),
            "evidence": {k: freshness[k] for k in
                        ("period_start", "period_end", "period_days", "period_days_required",
                         "generated_at", "generated_age_days", "generated_max_age_days")},
        })

    if revenue_blocked:
        for row in summary:
            row["net_revenue"] = None
        totals["net_revenue"] = None
        run_rate = None
        # Eligibility is computed further up, while the revenue figures still
        # exist. Blanking those inputs afterwards left the sanction standing:
        # the report stated that no revenue figure could be given and then
        # offered to disburse against it. A sanction may not outlive the number
        # it was derived from.
        reasons = []
        if float_blocked:
            reasons.append(
                f"the account type {float_type!r} is an agent float account, so no "
                "revenue figure is stated for it"
            )
        if freshness_blocked:
            reasons.append(_freshness_block_reason(freshness))
        blocked_reason = (
            "No sanction can be derived: " + "; and ".join(reasons) + ". A loan amount "
            "computed from these figures would be a figure the engine has just refused "
            "to state."
        )
        if float_blocked and not freshness_blocked:
            blocked_by = "float_account"
        elif freshness_blocked and not float_blocked:
            blocked_by = "statement_out_of_spec"
        else:
            blocked_by = "float_account_and_statement_out_of_spec"
        loan_eligibility = {
            "status": "not_assessed",
            "reason": blocked_reason,
            "blocked_by": blocked_by,
            "account_type": float_type,
            "months_required": loan_eligibility.get("months_required"),
            "months_used": [],
            "monthly_emi": loan_eligibility.get("monthly_emi"),
            "monthly_emi_source": loan_eligibility.get("monthly_emi_source"),
            "monthly_emi_scope": "open_performing_accounts_only",
            "requested_amount": loan_eligibility.get("requested_amount"),
            "sanctioned_amount": None,
            "bands": loan_eligibility.get("bands", []),
        }

    return {
        "account": {
            **statement.header.dict(),
            "source_format": getattr(statement, "source_format", "mtn_momo_v1"),
            "capabilities": caps.dict() if caps is not None else None,
            "is_float_account": bool(float_type),
            "reconciliation": statement.reconciliation_note()
            if hasattr(statement, "reconciliation_note") else None,
            "rejected_row_count": len(getattr(statement, "rejected_rows", [])),
            "pages": statement.pages,
            "closing_balance": _i(df.sort_values("dt")["balance_after"].dropna().iloc[-1])
            if df["balance_after"].notna().any()
            else None,
            "freshness": freshness,
        },
        "policy": policy.as_dict(),
        "summary": {
            "revenue_assessed": not revenue_blocked,
            "not_assessed_reason": loan_eligibility.get("reason") if revenue_blocked else None,
            "rows": summary,
            "totals": totals,
            "full_month_run_rate": run_rate,
            "full_months": [r["month"] for r in full],
            "partial_months": [r["month"] for r in summary if not r["is_full_month"]],
            "loan_eligibility": loan_eligibility,
        },
        "reconciliation": _reconciliation(summary, totals, loan_eligibility, disclosures,
                                          revenue_assessed=not revenue_blocked),
        "edge_cases": _edge_cases(statement, summary, totals, loan_eligibility, disclosures,
                                  details, notes, float_type, revenue_blocked,
                                  int(len(failed)), policy, freshness),
        "details": details,
        "disclosures": disclosures,
        "monthly_flow": _monthly_flow(df, months),
        "red_flags": flags,
        "rejected_rows": [r.dict() for r in getattr(statement, "rejected_rows", [])][:50],
        "assumptions": _assumptions(policy, statement),
    }


def _red_flags(df, inf, outf, failed, months, policy: Policy, statement, caps=None) -> List[Dict[str, Any]]:
    flags: List[Dict[str, Any]] = []
    gross_in = float(inf["amount"].sum())

    # 1. Pass-through / round-tripping
    daily, ptdays = _passthrough(df, policy)
    pt_total = float(daily["passthrough"].sum())
    pt_ratio = _share(pt_total, gross_in)
    intraday = getattr(caps, "timestamp_precision", "second") != "day"
    trips = _round_trips(df, policy) if intraday else []
    same_cp_trips = [t for t in trips if t.get("same_counterparty")]
    if pt_ratio >= 0.60 or len(ptdays) >= 3 or len(trips):
        flags.append(
            {
                "code": "PASS_THROUGH",
                "title": "Circular / round-tripping",
                "severity": "severe" if pt_ratio >= 0.75 else "elevated",
                "metric": f"{pt_ratio:.1%} of gross inflow left the account the same day",
                "summary": (
                    f"{_i(pt_total):,} of {_i(gross_in):,} RWF gross inflow exited on the day "
                    f"it arrived. {len(ptdays)} days saw at least "
                    f"{policy.passthrough_day_ratio:.0%} of the day's inflow leave, and "
                    + (
                        f"{len(trips)} inflows of {policy.roundtrip_min_amount:,}+ were "
                        f"{policy.roundtrip_ratio:.0%} reversed out within "
                        f"{policy.roundtrip_window_minutes} minutes, including "
                        f"{len(same_cp_trips)} same-counterparty match{'es' if len(same_cp_trips) != 1 else ''}. "
                        if intraday else
                        "Entries carry no clock time on this format, so the intra-hour "
                        "round-trip test could not be run. "
                    )
                    + "This is a conduit pattern, not a trading pattern."
                ),
                "evidence": {
                    "passthrough_days": [
                        {
                            "date": d,
                            "inflow": _i(r["inflow"]),
                            "outflow": _i(r["outflow"]),
                        }
                        for d, r in ptdays.head(12).iterrows()
                    ],
                    "rapid_round_trips": trips[:15],
                    "same_counterparty_round_trips": same_cp_trips[:15],
                },
            }
        )

    # 2. Agent / float contamination
    cash_in = inf[inf["txn_type"] == "CASH_IN"]
    if len(cash_in):
        depositors = int(cash_in["cp_id"].nunique())
        top_amounts = cash_in["amount"].value_counts().head(3)
        clustered = int(top_amounts.sum())
        if depositors >= 10:
            flags.append(
                {
                    "code": "AGENT_FLOAT",
                    "title": "Agent / float contamination",
                    "severity": "severe" if depositors >= 30 else "elevated",
                    "metric": f"{len(cash_in)} cash deposits from {depositors} distinct depositors",
                    "summary": (
                        f"{_i(cash_in['amount'].sum()):,} RWF arrived as CASH_IN from "
                        f"{depositors} different people. The statement carries no agent flag "
                        "and no commission credit, so agent float cannot be separated from own "
                        "trade revenue from this file alone. If any of it is third-party float, "
                        "net revenue is overstated by up to that amount. Resolve with the customer "
                        "before lending."
                    ),
                    "evidence": {
                        "cash_in_total": _i(cash_in["amount"].sum()),
                        "distinct_depositors": depositors,
                        "most_common_amounts": [
                            {"amount": _i(a), "occurrences": int(c)}
                            for a, c in top_amounts.items()
                        ],
                        "amount_clustering_note": (
                            f"{clustered} of {len(cash_in)} deposits sit on just three values - "
                            "float denominations or deliberate sizing, not retail takings."
                        ),
                        "busiest_days": [
                            {"date": d, "total": _i(v["amount"]), "count": int(v["n"])}
                            for d, v in cash_in.groupby("day")
                            .agg(amount=("amount", "sum"), n=("amount", "size"))
                            .sort_values("amount", ascending=False)
                            .head(5)
                            .iterrows()
                        ],
                    },
                }
            )

    # 3. Round numbers
    counted = inf[~inf["is_self_transfer"] & ~inf["is_loan"] & ~inf["is_large"]]
    if len(counted):
        rnd = counted["amount"] % policy.round_step == 0
        rnd_k = counted["amount"] % 1000 == 0
        val_share = _share(counted.loc[rnd, "amount"].sum(), counted["amount"].sum())
        if val_share >= policy.round_value_share_flag:
            flags.append(
                {
                    "code": "ROUND_NUMBERS",
                    "title": "Round-number inflows",
                    "severity": "severe" if val_share >= 0.70 else "elevated",
                    "metric": f"{val_share:.1%} of counted inflow value is an exact "
                              f"multiple of {policy.round_step:,}",
                    "summary": (
                        f"{rnd_k.mean():.1%} of counted inflows are exact multiples of 1,000 and "
                        f"{rnd.mean():.1%} are multiples of {policy.round_step:,}. Retail takings "
                        "are rarely round. Compare with the customer's own merchant spending, which "
                        "is not round - income rounder than expenditure indicates transfers, not sales."
                    ),
                    "evidence": {
                        "round_1k_count_share": round(float(rnd_k.mean()), 4),
                        "round_step_count_share": round(float(rnd.mean()), 4),
                        "round_step_value_share": val_share,
                    },
                }
            )

    # 4. Undisclosed borrowing
    loan_rows = _loan_ledger(df, policy)
    loan_cycles = _loan_cycle_matches(df, policy)
    susp = lambda n: _matches(n, policy.suspected_lenders) is not None
    inst_out = outf[_mask(outf["cp_name"], susp) | _mask(outf["cp_id"], susp)]
    if loan_rows or loan_cycles or len(inst_out):
        total_to_lenders = _i(inst_out["amount"].sum()) + _i(
            sum(r["amount"] for r in loan_rows if r["direction"] == "repayment")
        )
        flags.append(
            {
                "code": "UNDISCLOSED_BORROWING",
                "title": "Existing borrowing",
                "severity": "severe" if total_to_lenders >= policy.credit_cap else "elevated",
                "metric": f"{total_to_lenders:,} RWF paid to lenders and credit institutions",
                "summary": (
                    f"{len([r for r in loan_rows if r['direction']=='disbursement'])} confirmed "
                    f"disbursements, {len([r for r in loan_rows if r['direction']=='repayment'])} "
                    f"confirmed repayments, and {len(loan_cycles)} pattern-based loan cycle(s) "
                    "are visible, plus large round payments to banks and SACCOs that a "
                    "statement cannot distinguish from savings. Against a credit cap of "
                    f"{policy.credit_cap:,} RWF, existing indebtedness must be verified with the "
                    "credit bureau before any decision."
                ),
                "evidence": {
                    "confirmed_lender_ledger": loan_rows,
                    "pattern_based_loan_cycles": list(loan_cycles.values()),
                    "payments_to_credit_institutions": [
                        {
                            "timestamp": r.timestamp,
                            "counterparty": r.cp_name or r.cp_id,
                            "amount": _i(r.amount),
                        }
                        for r in inst_out.sort_values("amount", ascending=False).head(12).itertuples()
                    ],
                },
            }
        )

    # 5. Profile deviation
    daily_in = inf.groupby("day")["amount"].sum()
    monthly_in = inf.groupby("month")["amount"].sum()
    profile_evidence = {}
    profile_reasons = []

    if len(daily_in) >= 5:
        mean, sd = float(daily_in.mean()), float(daily_in.std(ddof=0))
        band = mean + policy.profile_sigma * sd
        outliers = daily_in[daily_in > band].sort_values(ascending=False)
        profile_evidence.update({
            "daily_median": _i(daily_in.median()),
            "daily_iqr": [_i(daily_in.quantile(.25)), _i(daily_in.quantile(.75))],
            "daily_mean": _i(mean),
            "daily_sigma": _i(sd),
            "daily_band": _i(band),
            "outlier_days": [
                {"date": d, "inflow": _i(v), "sigma": round((v - mean) / sd, 1) if sd else None}
                for d, v in outliers.head(8).items()
            ],
        })
        if len(outliers):
            profile_reasons.append(
                f"{len(outliers)} days exceed the customer's own mean + {policy.profile_sigma:g}σ daily band."
            )

    if len(monthly_in) >= 3:
        q1 = float(monthly_in.quantile(.25))
        q3 = float(monthly_in.quantile(.75))
        iqr = q3 - q1
        low_band = max(0.0, q1 - 1.5 * iqr)
        high_band = q3 + 1.5 * iqr
        month_outliers = monthly_in[(monthly_in < low_band) | (monthly_in > high_band)].sort_values(ascending=False)
        profile_evidence.update({
            "monthly_median": _i(monthly_in.median()),
            "monthly_iqr": [_i(q1), _i(q3)],
            "monthly_expected_range": [_i(low_band), _i(high_band)],
            "outlier_months": [
                {"month": m, "inflow": _i(v), "direction": "high" if v > high_band else "low"}
                for m, v in month_outliers.items()
            ],
        })
        if len(month_outliers):
            profile_reasons.append(
                f"{len(month_outliers)} months fall outside the customer's own monthly IQR range."
            )

    # Transaction-level deviation is only asserted when there is enough history
    # to establish a meaningful distribution. Large transactions remain visible
    # here even though they are separately excluded from revenue.
    if len(inf) >= 10:
        txn_mean = float(inf["amount"].mean())
        txn_sd = float(inf["amount"].std(ddof=0))
        txn_band = txn_mean + policy.profile_sigma * txn_sd
        txn_outliers = inf[inf["amount"] > txn_band].sort_values("amount", ascending=False)
        profile_evidence.update({
            "transaction_mean": _i(txn_mean),
            "transaction_sigma": _i(txn_sd),
            "transaction_band": _i(txn_band),
            "outlier_transactions": [
                {
                    "txn_id": r.txn_id,
                    "timestamp": r.timestamp,
                    "amount": _i(r.amount),
                    "counterparty": r.cp_name or r.cp_id,
                    "sigma": round((r.amount - txn_mean) / txn_sd, 1) if txn_sd else None,
                }
                for r in txn_outliers.head(12).itertuples()
            ],
        })
        if len(txn_outliers):
            profile_reasons.append(
                f"{len(txn_outliers)} inflow transactions exceed the customer's own transaction-size band."
            )

    if profile_reasons:
        flags.append(
            {
                "code": "PROFILE_DEVIATION",
                "title": "Profile deviation",
                "severity": "elevated",
                "metric": "; ".join(profile_reasons),
                "summary": (
                    "Activity was compared with the customer's own historical daily, monthly, "
                    "and transaction-size distributions where enough observations existed. "
                    "Outliers are evidence for review, not proof of unexplained income."
                ),
                "evidence": profile_evidence,
            }
        )

    # 6. Unattributed credits
    unattributed = inf[
        _mask(inf["cp_id"], lambda v: v in ("N/A", ""))
        | _mask(inf["cp_name"], lambda v: str(v).strip().lower() == "system")
    ]
    if len(unattributed):
        flags.append(
            {
                "code": "UNATTRIBUTED_CREDIT",
                "title": "Unattributed credits",
                "severity": "elevated",
                "metric": f"{len(unattributed)} credit{'s' if len(unattributed) != 1 else ''} "
                          "with no counterparty",
                "summary": (
                    f"{_i(unattributed['amount'].sum()):,} RWF arrived with no identifiable sender. "
                    "Source must be explained before it counts as revenue."
                ),
                "evidence": {
                    "items": [
                        {"timestamp": r.timestamp, "type": r.txn_type, "amount": _i(r.amount)}
                        for r in unattributed.itertuples()
                    ]
                },
            }
        )

    # 7. Liquidity
    bal = (
        df.sort_values("dt")["balance_after"].dropna()
        if getattr(caps, "balance", True) else pd.Series(dtype=float)
    )
    if len(bal):
        near_zero = int((bal < 1000).sum())
        if near_zero >= 5:
            flags.append(
                {
                    "code": "LOW_LIQUIDITY",
                    "title": "No retained buffer",
                    "severity": "elevated",
                    "metric": f"balance below 1,000 RWF on {near_zero} occasions",
                    "summary": (
                        f"Despite {_i(gross_in):,} RWF passing through, the balance falls below "
                        f"1,000 RWF {near_zero} times and closes the period at "
                        f"{_i(bal.iloc[-1]):,} RWF. There is no accumulated buffer to service a loan from."
                    ),
                    "evidence": {"closing_balance": _i(bal.iloc[-1]), "min_balance": _i(bal.min())},
                }
            )

    # 8. Failed transactions
    if len(failed):
        flags.append(
            {
                "code": "FAILED_TXNS",
                "title": "Failed transactions",
                "severity": "informational",
                "metric": f"{len(failed)} failed transactions excluded",
                "summary": "Excluded from every figure above. Repeated failed inbound bank pulls "
                           "often coincide with a near-zero balance.",
                "evidence": {
                    "items": [
                        {
                            "timestamp": r.timestamp,
                            "type": r.txn_type,
                            "counterparty": r.from_name or r.from_msisdn,
                            "amount": _i(r.amount),
                        }
                        for r in failed.sort_values("dt", ascending=False).itertuples()
                    ]
                },
            }
        )

    order = {"severe": 0, "elevated": 1, "informational": 2}
    return sorted(flags, key=lambda f: order.get(f["severity"], 3))


def _assumptions(policy: Policy, statement) -> List[str]:
    """Every assumption behind the figures, including the ones this format forces."""
    caps = getattr(statement, "capabilities", None)
    fmt = getattr(statement, "source_format", "mtn_momo_v1")

    items = [
        "An inflow is any transaction credited to the account holder. Where the format "
        "states debit/credit outright that is used; otherwise the To party is compared "
        "against the account identifier, with a fallback to the holder's display name "
        "for operator-generated rows.",
        f"Figures are derived from the {fmt} parser. The source format is recorded so a "
        "figure can be traced back to the extraction path that produced it.",
        "Gross inflow is stated before fees.",
        f"'Large transaction' means strictly greater than {policy.large_txn_threshold:,} RWF, "
        "so a credit of exactly that amount is not excluded.",
        "Loan inflows are deducted where the lender is confirmed by name, the statement "
        "types the transaction as a loan, or the transaction matches the configured "
        "round-principal/same-counterparty repayment pattern. Pattern matches are "
        "surfaced as evidence because they are behavioural evidence, not documentary proof.",
        "Partial months at the start and end of the period are not annualised. Only months "
        f"covering {policy.full_month_min_days}+ days feed the run rate.",
        "Rows that could not be attributed to the account in either direction are counted "
        "and disclosed rather than dropped from the totals.",
    ]

    if caps is None or caps.status:
        items.append(
            "Only SUCCESSFUL transactions are counted; FAILED rows are excluded from every "
            "figure."
        )
    else:
        items.append(
            "The statement lists settled entries only and carries no status column, so "
            "failed or reversed attempts are not visible in these figures."
        )

    if caps is None or caps.counterparty_name:
        items.append(
            "Self-transfer is tested by sender-name match against the account holder. "
            "Institutional own-account movements are reported separately and are not "
            "deducted from the base net."
        )
    else:
        items.append(
            "This format carries no counterparty names, so self-transfer is tested by "
            "identity: an inflow whose sender is the account itself. That is exact, but "
            "money arriving from a DIFFERENT account belonging to the same person cannot "
            "be recognised and is counted as revenue."
        )

    basis = getattr(caps, "cp_id_basis", "msisdn")
    if basis == "msisdn":
        items.append(
            "Counterparty identity is keyed on MSISDN, not display name; the source "
            "duplicates display names and these are de-duplicated on parse."
        )
    elif basis == "account_no":
        items.append("Counterparty identity is keyed on the account number printed by the bank.")
    else:
        items.append(
            "This format carries no counterparty identifier. Counterparties are keyed on a "
            "slug of the narration text, so distinct-sender and concentration figures are "
            "indicative rather than exact."
        )

    if caps is not None and caps.timestamp_precision == "day":
        items.append(
            "Entries are booked by date with no clock time. Same-day pass-through is "
            "measurable; anything with a sub-day window is not."
        )
    else:
        items.append(
            f"Rapid round-tripping is tested within {policy.roundtrip_window_minutes} minutes. "
            "A generic same-day/within-window reversal is reported, and a stronger "
            "same-counterparty match is separately identified when the format provides "
            "a stable counterparty identifier."
        )
    items.append(
        f"Pattern-based loan detection uses a {policy.loan_cycle_days}-day window, a "
        f"round principal, and same-counterparty round repayments; explicit repayment "
        "labels can qualify a single repayment, otherwise at least two round repayments "
        "are required."
    )

    recon = getattr(statement, "reconciliation_note", None)
    items.append(recon() if callable(recon) else
                 "Row completeness was not independently reconciled.")

    if caps is None or caps.balance:
        items.append(
            "No opening balance is stated on some statement types; where it is absent the "
            "balance chain is reconstructed from the first row's closing balance."
        )
    else:
        items.append(
            "This format carries no running balance, so no balance-chain verification was "
            "possible and liquidity checks were not run."
        )

    items.append(
        "The statement carries no payment narrative sufficient to prove purpose, so the "
        "reason for any individual transfer is inferential, not evidenced."
    )
    return items
