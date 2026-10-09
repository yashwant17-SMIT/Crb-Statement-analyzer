"""
Underwriting across more than one statement.

An applicant may hand over several files: two or three monthly MTN exports of
the same wallet, or an MTN wallet and a Bank of Kigali account, or both. The
underwriting outcome is one figure for the applicant, so the files are brought
together in two steps:

1. Files for the SAME account are merged into one statement. Rows that appear
   in two overlapping exports are kept once. The account is then analysed as
   if it had arrived as a single file, so rules that need continuity (round
   trips, loan cycles, pass-through) see the whole history.

2. DIFFERENT accounts are analysed on their own - each with the rules and
   capabilities of its own format - and the results are combined month by
   month. Money the applicant moved between their own accounts is removed so
   it is not counted once on the way out and again on the way in.

The combined report has the same shape as a single-statement report, plus an
`accounts` list carrying each account's own figures, so the console renders it
unchanged and can still show where each franc came from.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from . import analyzer
from .analyzer import _i, _share
from .config import Policy
from .statements import ParsedStatement, StatementHeader

# Codes the per-account reports raise about the sanction. The sanction is now
# struck on the combined months, so these are re-derived once for the whole
# application rather than repeated - and contradicted - per account.
_ELIGIBILITY_CODES = {
    "FEWER_MONTHS_THAN_POLICY", "MONTH_TOO_SHORT", "MONTHS_NORMALISED",
    "ELIGIBILITY_NOT_ASSESSED",
}
_SUM_KEYS = ("gross_inflows", "self_transfers", "loan_inflows",
             "institutional_credits", "large_transactions", "net_revenue",
             "inflow_count", "counted_inflow_count")


# --------------------------------------------------------------- merging ----
@dataclass
class MergedStatement(ParsedStatement):
    """Several exports of one account, read as one statement."""
    parts: List[ParsedStatement] = field(default_factory=list)
    duplicates_dropped: int = 0

    @property
    def parse_complete(self) -> bool:
        return all(p.parse_complete for p in self.parts)

    @property
    def balance_reconciles(self) -> Optional[bool]:
        # Each export reconciles against its own printed totals. Across files
        # the identity is broken by any gap or overlap, which says nothing
        # about whether an amount was misread.
        results = [p.balance_reconciles for p in self.parts]
        if any(r is False for r in results):
            return False
        return None if any(r is None for r in results) else True

    def reconciliation_note(self) -> str:
        return " ".join(f"File {i}: {p.reconciliation_note()}"
                        for i, p in enumerate(self.parts, 1))


def _txn_key(t) -> Tuple:
    # Same id is the same row. Formats without a stable id fall back to the
    # full row content, which two genuine transactions do not share.
    if t.txn_id:
        return ("id", str(t.txn_id))
    return ("row", t.timestamp, t.amount, t.direction, t.from_msisdn, t.to_msisdn,
            t.balance_after)


def merge_same_account(parts: List[ParsedStatement]) -> ParsedStatement:
    if len(parts) == 1:
        return parts[0]
    parts = sorted(parts, key=lambda p: p.header.period_start or "")
    seen, txns, dropped = set(), [], 0
    for p in parts:
        for t in p.transactions:
            k = _txn_key(t)
            if k in seen:
                dropped += 1
                continue
            seen.add(k)
            txns.append(t)
    txns.sort(key=lambda t: t.timestamp)

    first, last = parts[0].header, parts[-1].header
    starts = [p.header.period_start for p in parts if p.header.period_start]
    ends = [p.header.period_end for p in parts if p.header.period_end]
    generated = [p.header.generated_at for p in parts if p.header.generated_at]
    declared = [p.header.declared_txn_count for p in parts]
    header = StatementHeader(
        customer=next((p.header.customer for p in parts if p.header.customer), None),
        msisdn=first.msisdn,
        contact_phone=next((p.header.contact_phone for p in parts if p.header.contact_phone), None),
        email=next((p.header.email for p in parts if p.header.email), None),
        period_start=min(starts) if starts else None,
        period_end=max(ends) if ends else None,
        # The oldest export decides freshness: a stale file does not become
        # fresh by being uploaded next to a recent one.
        generated_at=min(generated) if generated else None,
        declared_txn_count=(sum(declared) if None not in declared and not dropped else None),
        opening_balance=first.opening_balance,
        closing_balance=last.closing_balance,
        account_type=next((p.header.account_type for p in parts if p.header.account_type), None),
        provider=first.provider,
    )
    return MergedStatement(
        header=header,
        transactions=txns,
        pages=sum(p.pages for p in parts),
        source_format=parts[0].source_format,
        capabilities=parts[0].capabilities,
        rejected_rows=[r for p in parts for r in p.rejected_rows],
        parts=parts,
        duplicates_dropped=dropped,
    )


def _period_gaps(parts: List[ParsedStatement]) -> List[Dict[str, str]]:
    """Stretches between consecutive exports of one account that no file covers."""
    gaps = []
    spans = sorted((p.header.period_start, p.header.period_end) for p in parts
                   if p.header.period_start and p.header.period_end)
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
        a = pd.to_datetime(prev_end).normalize()
        b = pd.to_datetime(next_start).normalize()
        if (b - a).days > 1:
            gaps.append({"from": str((a + pd.Timedelta(days=1)).date()),
                         "to": str((b - pd.Timedelta(days=1)).date())})
    return gaps


def group_by_account(items: List[Tuple[str, ParsedStatement]]) -> List[Dict[str, Any]]:
    """[(filename, statement)] -> one group per distinct account, upload order kept."""
    groups: Dict[Tuple, Dict[str, Any]] = {}
    for i, (name, st) in enumerate(items):
        acct = st.header.msisdn
        key = (st.header.provider, acct) if acct else ("__unknown__", i)
        g = groups.setdefault(key, {"files": [], "parts": []})
        g["files"].append(name)
        g["parts"].append(st)
    out = []
    for g in groups.values():
        merged = merge_same_account(g["parts"])
        out.append({
            "files": g["files"],
            "statement": merged,
            "duplicates_dropped": getattr(merged, "duplicates_dropped", 0),
            "gaps": _period_gaps(g["parts"]),
        })
    return out


def account_label(statement: ParsedStatement) -> str:
    h = statement.header
    return f"{h.provider or 'Statement'} · {h.msisdn or 'account not stated'}"


# ---------------------------------------------------- cross-account moves ----
def _counted_inflows(statement: ParsedStatement, report: Dict[str, Any]) -> pd.DataFrame:
    """Successful inflows this account's report still counts as revenue."""
    if not statement.transactions:
        return pd.DataFrame()
    df = analyzer._frame(statement, statement.header.msisdn or "")
    df = df[(df["status"] == "SUCCESSFUL") & df["is_inflow"]]
    d = report.get("details", {})
    excluded = {str(r["txn_id"]) for r in (
        d.get("self_transfers", {}).get("deducted", [])
        + d.get("loan_inflows", {}).get("items", [])
        + d.get("institutional_credits", {}).get("items", [])
        + d.get("large_transactions", {}).get("inflows", [])
    )}
    return df[~df["txn_id"].astype(str).isin(excluded)]


def _outflows(statement: ParsedStatement) -> pd.DataFrame:
    if not statement.transactions:
        return pd.DataFrame()
    df = analyzer._frame(statement, statement.header.msisdn or "")
    return df[(df["status"] == "SUCCESSFUL") & df["is_outflow"]]


def _has_ids(statement: ParsedStatement) -> bool:
    return getattr(statement.capabilities, "cp_id_basis", "msisdn") in ("msisdn", "account_no")


def _digits(v: Any) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def _same_id(a: Any, b: Any) -> bool:
    # MoMo numbers print with and without the 250 prefix.
    da, db = _digits(a), _digits(b)
    return bool(da) and bool(db) and (da == db or da[-9:] == db[-9:] and min(len(da), len(db)) >= 9)


def _to_institution(row, statement: ParsedStatement, policy: Policy) -> bool:
    """Did this outflow go to a bank or wallet rather than to a person?"""
    if analyzer._matches(row["cp_name"] or "", policy.institution_patterns):
        return True
    return _has_ids(statement) and bool(re.match(r"^[A-Za-z]", str(row["cp_id"] or "")))


def find_inter_account_transfers(accounts: List[Dict[str, Any]],
                                 policy: Policy) -> Dict[int, List[Dict[str, Any]]]:
    """Inflows on one account that are the applicant's own outflow from another.

    Amount equality alone is not evidence: two busy wallets share hundreds of
    same-day amounts by coincidence. So a pair is only matched on evidence
    that the money went between the two accounts:

    * where both formats key counterparties on a real identifier (MoMo to
      MoMo), the inflow's sender must BE the other account, or the outflow's
      recipient must be this one;
    * otherwise (a bank format with no counterparty id), exact amount within
      one calendar day - bank entries carry no clock time - AND the outflow
      went to a bank or wallet, never to a person.

    Each outflow is used at most once. Only rows still counted as revenue are
    considered: a transfer this account's own rules already excluded (as a
    bank or MoMo credit, say) is not deducted a second time.
    """
    found: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    if len(accounts) < 2:
        return found
    outs = {i: _outflows(a["statement"]) for i, a in enumerate(accounts)}
    used: set = set()
    for i, a in enumerate(accounts):
        inf = _counted_inflows(a["statement"], a["report"])
        own_id = a["statement"].header.msisdn
        for r in inf.sort_values("dt").itertuples():
            for j, o in outs.items():
                if j == i or o.empty:
                    continue
                other = accounts[j]["statement"]
                cand = o[(o["amount"] == r.amount)
                         & ((o["dt"].dt.normalize() - r.dt.normalize()).abs() <= pd.Timedelta(days=1))]
                cand = cand[[(j, str(t)) not in used for t in cand["txn_id"]]]
                if cand.empty:
                    continue
                if _has_ids(a["statement"]) and _has_ids(other):
                    if not _same_id(r.cp_id, other.header.msisdn):
                        cand = cand[[_same_id(c, own_id) for c in cand["cp_id"]]]
                else:
                    cand = cand[[_to_institution(row, other, policy)
                                 for _, row in cand.iterrows()]]
                if cand.empty:
                    continue
                hit = cand.iloc[0]
                used.add((j, str(hit["txn_id"])))
                found[i].append({
                    "txn_id": r.txn_id,
                    "timestamp": r.timestamp,
                    "type": r.txn_type,
                    "counterparty": r.cp_name or r.cp_id,
                    "msisdn": r.cp_id,
                    "amount": _i(r.amount),
                    "fee": None,
                    "balance_after": _i(r.balance_after) if pd.notna(r.balance_after) else None,
                    "month": r.month,
                    "basis": "inter_account",
                    "account": a["label"],
                    "matched_outflow": {
                        "account": accounts[j]["label"],
                        "txn_id": hit["txn_id"],
                        "timestamp": hit["timestamp"],
                        "amount": _i(hit["amount"]),
                    },
                })
                break
    return found


# ------------------------------------------------------------- combining ----
def _tag(rows: List[Dict[str, Any]], label: str) -> List[Dict[str, Any]]:
    return [{**r, "account": label} for r in rows]


def _combine_rows(accounts: List[Dict[str, Any]], policy: Policy) -> List[Dict[str, Any]]:
    by_month: Dict[str, Dict[str, Any]] = {}
    for a in accounts:
        for r in a["report"]["summary"]["rows"]:
            m = by_month.setdefault(r["month"], {
                "month": r["month"], **{k: 0 for k in _SUM_KEYS},
                "days_covered": 0, "is_full_month": True, "by_account": [],
            })
            for k in _SUM_KEYS:
                m[k] += r[k] or 0
            # Where two accounts cover a month for different lengths, the
            # month is scaled on the LONGER one. The shorter account's share is
            # therefore not extrapolated - an understatement, never an
            # overstatement.
            m["days_covered"] = max(m["days_covered"], r.get("days_covered") or 0)
            m["is_full_month"] = m["is_full_month"] and bool(r.get("is_full_month"))
            m["by_account"].append({"account": a["label"], "net_revenue": r["net_revenue"],
                                    "gross_inflows": r["gross_inflows"],
                                    "days_covered": r.get("days_covered")})
    rows = [by_month[k] for k in sorted(by_month)]
    for r in rows:
        r["is_full_month"] = r["is_full_month"] and r["days_covered"] >= policy.full_month_min_days
        r["accounts_contributing"] = len(r["by_account"])
    return rows


def _combine_flow(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    flow: Dict[str, Dict[str, int]] = defaultdict(lambda: {"inflow": 0, "outflow": 0})
    for a in accounts:
        for r in a["report"].get("monthly_flow", []):
            flow[r["month"]]["inflow"] += r["inflow"]
            flow[r["month"]]["outflow"] += r["outflow"]
    out = []
    for m in sorted(flow):
        gin, gout = flow[m]["inflow"], flow[m]["outflow"]
        out.append({"month": m, "inflow": gin, "outflow": gout, "retained": gin - gout,
                    "passthrough_ratio": _share(min(gin, gout), gin)})
    return out


def _sum_opt(values: List[Optional[int]]) -> Optional[int]:
    return None if any(v is None for v in values) else sum(values)


def _combine_details(accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
    ds = [(a["label"], a["report"]["details"]) for a in accounts]
    first = ds[0][1]
    by_basis: Dict[str, Dict[str, int]] = {}
    for _, d in ds:
        for basis, v in d["self_transfers"].get("by_basis", {}).items():
            b = by_basis.setdefault(basis, {"count": 0, "total": 0})
            b["count"] += v["count"]
            b["total"] += v["total"]
    cat = lambda f: [row for label, d in ds for row in _tag(f(d), label)]  # noqa: E731
    return {
        "self_transfers": {
            "deducted": cat(lambda d: d["self_transfers"]["deducted"]),
            "strict_name_match": cat(lambda d: d["self_transfers"]["strict_name_match"]),
            "by_basis": by_basis,
            "possible_for_review": cat(lambda d: d["self_transfers"]["possible_for_review"]),
            "note": first["self_transfers"]["note"] + " Across accounts, money the applicant "
                    "moved from one of their own accounts into another is also deducted "
                    "(basis: inter_account).",
        },
        "loan_inflows": {
            "items": cat(lambda d: d["loan_inflows"]["items"]),
            "ledger": cat(lambda d: d["loan_inflows"]["ledger"]),
            "pattern_matches": cat(lambda d: d["loan_inflows"]["pattern_matches"]),
            "suspected_not_deducted": cat(lambda d: d["loan_inflows"]["suspected_not_deducted"]),
        },
        "institutional_credits": {
            "items": cat(lambda d: d["institutional_credits"]["items"]),
            "identifiable": all(d["institutional_credits"]["identifiable"] for _, d in ds),
            "note": " ".join(dict.fromkeys(
                f"{label}: {d['institutional_credits']['note']}" if
                not d["institutional_credits"]["identifiable"] else
                d["institutional_credits"]["note"] for label, d in ds)),
        },
        "large_transactions": {
            "inflows": cat(lambda d: d["large_transactions"]["inflows"]),
            "outflows": cat(lambda d: d["large_transactions"]["outflows"]),
        },
        "bidirectional_counterparties": cat(lambda d: d["bidirectional_counterparties"])[:40],
    }


def _combine_disclosures(accounts: List[Dict[str, Any]], totals: Dict[str, Any],
                         policy: Policy) -> Dict[str, Any]:
    ds = [(a["label"], a["report"]["disclosures"]) for a in accounts]
    s = lambda k: sum(d.get(k) or 0 for _, d in ds)  # noqa: E731
    by_month: Dict[str, int] = defaultdict(int)
    for _, d in ds:
        for m, v in (d.get("distinct_individual_senders_by_month") or {}).items():
            by_month[m] += v
    observed = s("inflow_count")
    mtc = dict(ds[0][1].get("minimum_transaction_check") or {})
    mtc.update({
        "observed_inflow_count": observed,
        "observed_total_count": s("total_transaction_count"),
        "active_days": sum((d.get("minimum_transaction_check") or {}).get("active_days") or 0
                           for _, d in ds),
    })
    if policy.min_inflow_count is not None:
        mtc["status"] = "pass" if observed >= policy.min_inflow_count else "fail"
    return {
        "total_transaction_count": s("total_transaction_count"),
        "unattributed_count": s("unattributed_count"),
        "unattributed_total": s("unattributed_total"),
        "declared_transaction_count": _sum_opt([d.get("declared_transaction_count") for _, d in ds]),
        "parse_complete": all(d.get("parse_complete") is not False for _, d in ds),
        "successful_count": s("successful_count"),
        "failed_count": s("failed_count"),
        "inflow_count": observed,
        "outflow_count": s("outflow_count"),
        "counted_inflow_count": totals["counted_inflow_count"],
        "large_transaction_count": s("large_transaction_count"),
        "large_inflow_count": s("large_inflow_count"),
        "large_outflow_count": s("large_outflow_count"),
        # Senders are counted per account and added. The same person paying
        # into both accounts is counted twice: account identifiers differ by
        # format, so they cannot be matched across accounts.
        "distinct_senders_total": s("distinct_senders_total"),
        "distinct_individual_senders": _sum_opt([d.get("distinct_individual_senders") for _, d in ds]),
        "distinct_institution_senders": _sum_opt([d.get("distinct_institution_senders") for _, d in ds]),
        "distinct_individual_senders_by_month": dict(sorted(by_month.items())),
        "distinct_sender_basis": "per_account_sum",
        "minimum_transaction_check": mtc,
        "concentration": [row for label, d in ds for row in _tag(d.get("concentration") or [], label)],
        "capability_notes": [
            {**n, "note": f"{label}: {n['note']}"}
            for label, d in ds for n in (d.get("capability_notes") or [])
        ],
    }


def combine_reports(accounts: List[Dict[str, Any]], policy: Policy,
                    monthly_emi=None, requested_amount=None,
                    customer_type=None) -> Dict[str, Any]:
    """accounts: [{label, files, statement, report, duplicates_dropped, gaps}]."""
    # Accounts whose own report refuses to state revenue (agent float, stale
    # document) contribute nothing to the combined figure. Their findings are
    # still carried through.
    included = [a for a in accounts if a["report"]["summary"].get("revenue_assessed", True)]
    blocked = [a for a in accounts if a not in included]

    transfers = find_inter_account_transfers(included, policy)
    for i, a in enumerate(included):
        moved = transfers.get(i, [])
        a["inter_account_transfers"] = moved
        if not moved:
            continue
        per_month: Dict[str, List[int]] = defaultdict(list)
        for t in moved:
            per_month[t["month"]].append(t["amount"])
        for r in a["report"]["summary"]["rows"]:
            amts = per_month.get(r["month"], [])
            r["self_transfers"] += sum(amts)
            r["net_revenue"] -= sum(amts)
            r["counted_inflow_count"] -= len(amts)
        t = a["report"]["summary"]["totals"]
        t["self_transfers"] += sum(x["amount"] for x in moved)
        t["net_revenue"] -= sum(x["amount"] for x in moved)
        t["counted_inflow_count"] -= len(moved)

    rows = _combine_rows(included, policy) if included else []
    totals = {k: _i(sum(r[k] for r in rows)) for k in _SUM_KEYS}
    full = [r for r in rows if r["is_full_month"]]
    run_rate = _i(sum(r["net_revenue"] for r in full) / len(full)) if full else None
    revenue_assessed = bool(included)

    if revenue_assessed:
        eligibility = analyzer._loan_eligibility(rows, policy, monthly_emi, requested_amount,
                                                 customer_type)
    else:
        eligibility = {
            "status": "not_assessed",
            "reason": "No sanction can be derived: none of the uploaded statements yields a "
                      "revenue figure. " + " ".join(
                          f"{a['label']}: {a['report']['summary'].get('not_assessed_reason')}"
                          for a in blocked),
            "blocked_by": "all_accounts_blocked",
            "months_required": policy.eligibility_months,
            "months_used": [],
            "monthly_emi": monthly_emi,
            "requested_amount": requested_amount,
            "sanctioned_amount": None,
        }
        for k in ("gross_inflows", "self_transfers", "loan_inflows",
                  "institutional_credits", "large_transactions", "net_revenue"):
            totals[k] = None if k == "net_revenue" else totals[k]

    details = _combine_details(accounts)
    moved_all = [t for a in included for t in a.get("inter_account_transfers", [])]
    details["self_transfers"]["deducted"] = moved_all + details["self_transfers"]["deducted"]
    if moved_all:
        details["self_transfers"]["by_basis"]["inter_account"] = {
            "count": len(moved_all), "total": sum(t["amount"] for t in moved_all)}
    details["inter_account_transfers"] = moved_all

    disclosures = _combine_disclosures(accounts, totals, policy)

    # ---- findings, each labelled with the account it came from ------------
    red_flags, edge_cases = [], []
    for a in accounts:
        rep = a["report"]
        for f in rep.get("red_flags", []):
            red_flags.append({**f, "account": a["label"], "title": f"{f['title']} — {a['label']}"})
        for e in rep.get("edge_cases", []):
            if e["code"] in _ELIGIBILITY_CODES:
                continue
            edge_cases.append({**e, "account": a["label"], "title": f"{e['title']} — {a['label']}"})

    # Sanction-level findings, re-derived on the combined months.
    stub = ParsedStatement(header=StatementHeader(), transactions=[], pages=0)
    for e in analyzer._edge_cases(stub, rows, totals, eligibility, disclosures, details,
                                  [], None, False, 0, policy, None):
        if e["code"] in _ELIGIBILITY_CODES:
            edge_cases.append(e)

    def add(code, severity, title, detail, evidence=None):
        edge_cases.append({"code": code, "severity": severity, "title": title,
                           "detail": detail, "evidence": evidence or {}})

    add("COMBINED_STATEMENTS", "medium", f"{len(accounts)} accounts assessed together",
        "Net revenue and the sanction are struck on the combined monthly totals of: "
        + "; ".join(f"{a['label']} ({len(a['files'])} file{'s' if len(a['files']) > 1 else ''})"
                    for a in accounts) + ".",
        {"accounts": [{"account": a["label"], "files": a["files"]} for a in accounts]})
    if blocked:
        add("ACCOUNT_EXCLUDED_FROM_COMBINED", "high", "An account was left out of the combined figure",
            "; ".join(f"{a['label']}: {a['report']['summary'].get('not_assessed_reason')}"
                      for a in blocked),
            {"accounts": [a["label"] for a in blocked]})
    if moved_all:
        add("INTER_ACCOUNT_TRANSFERS", "medium", "Transfers between the applicant's own accounts deducted",
            f"{len(moved_all)} inflows totalling {sum(t['amount'] for t in moved_all):,} RWF match an "
            "outflow of the same amount from another uploaded account within a day. They are the "
            "applicant moving their own money and are deducted as self-transfers so they are not "
            "counted twice.",
            {"rows": moved_all[:10]})
    uneven = [r["month"] for r in rows if r["accounts_contributing"] < len(included)]
    if len(included) > 1 and uneven:
        add("UNEVEN_ACCOUNT_COVERAGE", "medium", "Not every account covers every month",
            f"{', '.join(uneven)} include only some of the uploaded accounts, so the combined "
            "figure for those months is understated. Align the statement periods for a like-for-like "
            "average.",
            {"months": [{"month": r["month"], "accounts": [b["account"] for b in r["by_account"]]}
                        for r in rows if r["month"] in uneven]})
    unequal_days = [r["month"] for r in rows
                    if len({b["days_covered"] for b in r["by_account"]}) > 1]
    if unequal_days:
        add("UNEQUAL_DAYS_IN_MONTH", "low", "Accounts cover a month for different lengths",
            f"In {', '.join(unequal_days)} the accounts cover different numbers of days. The "
            "month is scaled to 30 days on the longest coverage, so the shorter account is "
            "not extrapolated (conservative).")
    for a in accounts:
        if a.get("duplicates_dropped"):
            add("DUPLICATE_ROWS_MERGED", "low", f"Overlapping files merged — {a['label']}",
                f"{a['duplicates_dropped']} rows appeared in more than one uploaded file and were "
                "counted once.", {"files": a["files"]})
        if a.get("gaps"):
            add("STATEMENT_GAP", "high", f"Gap between statement files — {a['label']}",
                "No file covers " + ", ".join(f"{g['from']} to {g['to']}" for g in a["gaps"])
                + ". Transactions in that stretch are missing from every figure.",
                {"gaps": a["gaps"], "files": a["files"]})
    holders = {(a["statement"].header.customer or "").strip().upper()
               for a in accounts if a["statement"].header.customer}
    if len(holders) > 1:
        add("ACCOUNT_HOLDERS_DIFFER", "high", "Statements name different account holders",
            "The uploaded statements print different holder names: "
            + "; ".join(f"{a['label']}: {a['statement'].header.customer!r}" for a in accounts)
            + ". Confirm every account belongs to this applicant before relying on the combined figure.")
    order = {"high": 0, "medium": 1, "low": 2}
    edge_cases.sort(key=lambda x: order.get(x["severity"], 3))

    # ---- account block: what the console shows as "the statement" ---------
    heads = [a["report"]["account"] for a in accounts]
    fresh = [h.get("freshness") or {} for h in heads]
    ages = [f.get("generated_age_days") for f in fresh if f.get("generated_age_days") is not None]
    starts = [h["period_start"] for h in heads if h.get("period_start")]
    ends = [h["period_end"] for h in heads if h.get("period_end")]
    p_start, p_end = (min(starts) if starts else None), (max(ends) if ends else None)
    period_days = ((pd.to_datetime(p_end) - pd.to_datetime(p_start)).days + 1
                   if p_start and p_end else None)
    oldest = max(range(len(fresh)), key=lambda i: fresh[i].get("generated_age_days") or -1)
    account = {
        **heads[0],
        "customer": next((h.get("customer") for h in heads if h.get("customer")), None),
        "msisdn": " + ".join(h.get("msisdn") or "?" for h in heads),
        "provider": " + ".join(dict.fromkeys(h.get("provider") or "?" for h in heads)),
        "period_start": p_start,
        "period_end": p_end,
        "source_format": "combined",
        "is_float_account": any(h.get("is_float_account") for h in heads),
        "reconciliation": " ".join(f"{a['label']}: {a['report']['account'].get('reconciliation')}"
                                   for a in accounts),
        "rejected_row_count": sum(h.get("rejected_row_count") or 0 for h in heads),
        "pages": sum(h.get("pages") or 0 for h in heads),
        "closing_balance": _sum_opt([h.get("closing_balance") for h in heads]),
        "freshness": {
            **fresh[oldest],
            "period_start": p_start, "period_end": p_end, "period_days": period_days,
            "generated_age_days": max(ages) if ages else None,
            "generated_ok": (None if any(f.get("generated_ok") is None for f in fresh)
                             else all(f.get("generated_ok") for f in fresh)),
            "blocking": not revenue_assessed,
        },
    }

    summary = {
        "revenue_assessed": revenue_assessed,
        "not_assessed_reason": eligibility.get("reason") if not revenue_assessed else None,
        "rows": rows,
        "totals": totals,
        "full_month_run_rate": run_rate,
        "full_months": [r["month"] for r in full],
        "partial_months": [r["month"] for r in rows if not r["is_full_month"]],
        "loan_eligibility": eligibility,
    }

    return {
        "account": account,
        "policy": policy.as_dict(),
        "summary": summary,
        "reconciliation": analyzer._reconciliation(rows, totals, eligibility, disclosures,
                                                   revenue_assessed=revenue_assessed),
        "edge_cases": edge_cases,
        "details": details,
        "disclosures": disclosures,
        "monthly_flow": _combine_flow(included or accounts),
        "red_flags": red_flags,
        "rejected_rows": [r for a in accounts for r in a["report"].get("rejected_rows", [])][:50],
        "assumptions": list(dict.fromkeys(
            [s for a in accounts for s in a["report"].get("assumptions", [])]
            + ["Several statements were combined. Each account is analysed under its own "
               "format's rules; monthly figures are then added. A month covered by two "
               "accounts for different lengths is scaled on the longer coverage. Inflows "
               "matching an outflow of the same amount from another uploaded account within "
               "one day are treated as the applicant's own money and deducted."])),
        "combined": True,
        "accounts": [
            {
                "label": a["label"],
                "files": a["files"],
                "provider": a["statement"].header.provider,
                "account_id": a["statement"].header.msisdn,
                "customer": a["statement"].header.customer,
                "period_start": a["report"]["account"].get("period_start"),
                "period_end": a["report"]["account"].get("period_end"),
                "included_in_combined": a in included,
                "not_included_reason": (None if a in included else
                                        a["report"]["summary"].get("not_assessed_reason")),
                "duplicates_dropped": a.get("duplicates_dropped", 0),
                "gaps": a.get("gaps", []),
                "inter_account_transfers_deducted": sum(
                    t["amount"] for t in a.get("inter_account_transfers", [])),
                "totals": a["report"]["summary"]["totals"],
                "rows": a["report"]["summary"]["rows"],
                "meta": a.get("meta"),
            }
            for a in accounts
        ],
    }
