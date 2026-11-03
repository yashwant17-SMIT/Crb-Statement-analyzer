"""
The Excel workbook the ops team reads.

Tabs, in order:
  Summary               what the file is, counts per outcome, why rows could
                        not be assessed, and the checks in plain words
  Could be approved     one tab per outcome that needs action, same columns
  Refer to credit head    as All customers
  Would be rejected
  Manual check
  Could not assess      the exact reason and what to do, per row
  All customers         every intake row: outcome, next step, why (account by
                        account) and a Pass / Refer / Check / Fail per check
  Loan details          every loan on every assessed CRB, with the ones that
                        affected the result flagged
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import checks
from checks import ALLOWED, REFER, REJECT, REVIEW, TITLES, day

CODES = list(TITLES)
MARK = {ALLOWED: "✓ Pass", REFER: "⚠ Refer", REVIEW: "? Check", REJECT: "✗ Fail"}

GREEN, AMBER, RED, BLUE, GREY = "C6EFCE", "FFEB9C", "FFC7CE", "DDEBF7", "E7E6E6"
RESULT_FILL = {ALLOWED: GREEN, REFER: AMBER, REVIEW: BLUE, REJECT: RED}

# Outcome label, colour, tab, and what it means / what to do.
OUTCOMES = {
    checks.COULD_APPROVE: ("Could be approved", GREEN, "Could be approved",
                           "Rejected today on band, but passes every deviation check. "
                           "Candidate for approval under the EE–JJ deviation policy."),
    checks.REJECTED_REFER: ("Refer to credit head", AMBER, "Refer to credit head",
                            "Rejected today on band. No check fails, but an overdue loan is within the "
                            "Refer limits, so it needs the credit head's decision."),
    checks.REJECTED_REVIEW: ("Manual check needed", BLUE, "Manual check",
                             "Rejected today on band. The CRB report is missing a date or figure a check "
                             "needs. Open the report and look at the loans listed under Why."),
    checks.STAYS_REJECTED: ("Stays rejected", RED, None,
                            "Rejected today on band, and fails at least one deviation check (see Why)."),
    checks.WOULD_REJECT: ("Would be rejected", RED, "Would be rejected",
                          "Approved today on band, but fails at least one deviation check (see Why)."),
    checks.APPROVED_REFER: ("Refer to credit head", AMBER, "Refer to credit head",
                            "Approved today on band. Under the deviation checks an overdue loan would need "
                            "the credit head's sign-off."),
    checks.APPROVED_REVIEW: ("Manual check needed", BLUE, "Manual check",
                             "Approved today on band. The CRB report is missing a date or figure a check "
                             "needs. Open the report and look at the loans listed under Why."),
    checks.NO_CHANGE: ("Stays approved", GREEN, None,
                       "Approved today on band, and passes every deviation check."),
    checks.NOT_ASSESSED: ("Could not assess", GREY, "Could not assess",
                          "See Why for the exact reason and What to do."),
}
ACTION_TABS = ["Could be approved", "Refer to credit head", "Would be rejected", "Manual check", "Could not assess"]

MAIN_COLUMNS = [("Intake row", 9), ("Application ID", 14), ("Customer", 24), ("CRB band", 9), ("Today", 10),
                ("Outcome", 20), ("What it means / what to do", 45), ("Why", 90)] + \
               [(TITLES[c], 13) for c in CODES] + \
               [("Loans on CRB", 16), ("Name on CRB", 26), ("National ID", 18), ("CRB report date", 20),
                ("CRB file", 30)]


def _cutoff(policy) -> str:
    return day(datetime.strptime(policy.crb_history_cutoff, "%Y-%m-%d"))


def why(rec: dict) -> str:
    """The reason, account by account; for a pass, what each check found."""
    if rec["checks"] is None:
        return f"{rec['reason']}. {rec['detail']}"
    cs = rec["checks"]
    bad = sorted((c for c in cs if c.result != ALLOWED), key=lambda c: -checks._RANK[c.result])
    if not bad:
        return "\n".join(["Passes all 5 checks:"] + [f"✓ {c.title}: {c.summary}" for c in cs])
    lines = []
    for c in bad:
        lines.append(f"{MARK[c.result]} – {c.title}: {c.summary}")
        lines += [f"    • {f.text}" for f in c.findings if f.result != ALLOWED]
    passed = [c.title for c in cs if c.result == ALLOWED]
    if passed:
        lines.append("✓ Passed: " + ", ".join(passed))
    return "\n".join(lines)


def main_row(rec: dict) -> list:
    label, _, _, meaning = OUTCOMES[rec["group"]]
    if rec["checks"] is None:
        meaning = rec["action"]
    marks = {c.code: MARK[c.result] for c in rec["checks"] or []}
    rep = rec.get("report")
    loans = (f"{sum(a.is_open for a in rep.accounts)} open, {sum(not a.is_open for a in rep.accounts)} closed"
             if rep else "")
    return [rec["row"], rec["app_id"], rec["customer"], rec["band"], rec["today"], label, meaning, why(rec)] + \
           [marks.get(c, "") for c in CODES] + \
           [loans, rec["crb_name"], rec["national_id"], rec["report_date"], rec["file_name"] or rec["link"]]


def loan_rows(rec: dict, policy) -> List[list]:
    rep = rec.get("report")
    if rep is None:
        return []
    flags: Dict[int, list] = {}
    for c in rec["checks"] or []:   # none for a report read but with no band
        for f in c.findings:
            if f.account is not None:
                flags.setdefault(id(f.account), []).append((c, f))
    cutoff = datetime.strptime(policy.crb_history_cutoff, "%Y-%m-%d")
    out = []
    for a in rep.accounts:
        hits = flags.get(id(a), [])
        worst = max(hits, key=lambda h: checks._RANK[h[1].result], default=None)
        flag = f"{MARK[worst[1].result]} – {worst[0].title}" if worst and worst[1].result != ALLOWED else ""
        note = "\n".join(f.text.split(": ", 1)[-1] for _, f in hits)
        term = (f"{a.repayment_term_months:g} months" if a.repayment_term_months else a.repayment_terms or "")
        out.append([rec["row"], rec["customer"] or rec["crb_name"], OUTCOMES[rec["group"]][0], a.index,
                    a.account_type, a.trade_sector, "Open" if a.is_open else "Closed", a.account_status,
                    a.classification, day(a.listing_date), term, day(a.last_payment_date),
                    round(a.principal_amount), round(a.balance_amount), round(a.arrears_amount),
                    a.days_in_arrears if a.days_in_arrears_reported else "not printed",
                    "Yes" if checks._after_cutoff(a, cutoff) else "No (before cutoff)", flag, note])
    return out


LOAN_COLUMNS = [("Intake row", 9), ("Customer", 24), ("Outcome", 20), ("Account #", 9), ("Loan type", 22),
                ("Lender sector", 20), ("Open / closed", 11), ("Status", 16), ("Classification", 16),
                ("Listed", 12), ("Term", 14), ("Last payment", 13), ("Principal (RWF)", 15),
                ("Balance (RWF)", 14), ("Arrears (RWF)", 14), ("Days overdue", 12),
                ("Listed from cutoff", 16), ("Flag", 26), ("Why flagged", 70)]

HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
HEAD_FONT = Font(bold=True, color="FFFFFF")
WRAP = Alignment(wrap_text=True, vertical="top")


def _fill(color: str) -> PatternFill:
    return PatternFill("solid", fgColor=color)


def _table(ws, columns, rows, colour=None, start_row: int = 1) -> None:
    """A header and rows with wrapping, widths, filter and frozen header."""
    for j, (name, width) in enumerate(columns, 1):
        cell = ws.cell(start_row, j, name)
        cell.fill, cell.font, cell.alignment = HEAD_FILL, HEAD_FONT, WRAP
        ws.column_dimensions[get_column_letter(j)].width = width
    for i, values in enumerate(rows, start_row + 1):
        lines = 1
        for j, v in enumerate(values, 1):
            cell = ws.cell(i, j, v)
            cell.alignment = WRAP
            if isinstance(v, str):
                width = columns[j - 1][1]
                lines = max(lines, sum(1 + len(part) // max(width, 1) for part in v.split("\n")))
        ws.row_dimensions[i].height = min(15 * lines, 400)
        if colour:
            colour(ws, i, values)
    ws.freeze_panes = ws.cell(start_row + 1, 4)
    last = get_column_letter(len(columns))
    ws.auto_filter.ref = f"A{start_row}:{last}{max(start_row + len(rows), start_row + 1)}"


def _colour_main(ws, i, values) -> None:
    outcome_col = 6
    label = values[outcome_col - 1]
    fill = next((c for (l, c, _, _) in OUTCOMES.values() if l == label), None)
    if fill:
        ws.cell(i, outcome_col).fill = _fill(fill)
        ws.cell(i, outcome_col).font = Font(bold=True)
    first_check = 9
    for k in range(len(CODES)):
        v = values[first_check - 1 + k]
        res = next((r for r, m in MARK.items() if m == v), None)
        if res:
            ws.cell(i, first_check + k).fill = _fill(RESULT_FILL[res])


def _colour_loans(ws, i, values) -> None:
    flag = values[17]
    res = next((r for r, m in MARK.items() if flag.startswith(m)), None)
    if res:
        ws.cell(i, 18).fill = _fill(RESULT_FILL[res])


def _summary(ws, records: List[dict], info: dict, policy) -> None:
    cutoff = _cutoff(policy)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 80
    ws.column_dimensions["E"].width = 22
    r = 1

    def put(*values, bold=False, fill=None, wrap=True, size=None):
        nonlocal r
        for j, v in enumerate(values, 1):
            c = ws.cell(r, j, v)
            c.alignment = WRAP if wrap else Alignment(vertical="top")
            if bold or size:
                c.font = Font(bold=bold, size=size or 11)
            if fill and j == 1:
                c.fill = _fill(fill)
        r += 1

    def head(*names):
        nonlocal r
        for j, v in enumerate(names, 1):
            c = ws.cell(r, j, v)
            c.fill, c.font, c.alignment = HEAD_FILL, HEAD_FONT, WRAP
        r += 1

    put("CRB deviation check – results", size=16, bold=True)
    assessed = sum(rec["checks"] is not None for rec in records)
    for k, v in [(info.get("source_label", "Intake sheet"), info["source"]),
                 ("Run on", f"{datetime.now():%d %b %Y %H:%M}"),
                 ("Run as (Google account)", info.get("account")),
                 ("Customers", f"{len(records)} rows: {assessed} assessed, {len(records) - assessed} could not be assessed")]:
        if not v:
            continue
        put(k, v, bold=False)
        ws.merge_cells(start_row=r - 1, start_column=2, end_row=r - 1, end_column=4)
    r += 1
    put("What this is", bold=True)
    put("Customers with a CRB band of EE–JJ are rejected today. This file checks every customer against the "
        "proposed deviation points. A rejected customer who passes every point could be approved. An approved "
        "customer (band AA–DD) who fails a point would be rejected under the same points. "
        "Nothing in the intake sheet was changed.")
    ws.merge_cells(start_row=r - 1, start_column=1, end_row=r - 1, end_column=5)
    ws.row_dimensions[r - 1].height = 48
    r += 1

    put("Outcomes", bold=True)
    head("Outcome", "Today", "Customers", "What it means / what to do", "Tab")
    counts = Counter(rec["group"] for rec in records)
    for g in checks.GROUPS:
        label, fill, tab, meaning = OUTCOMES[g]
        today = ("Rejected" if g in (checks.COULD_APPROVE, checks.REJECTED_REFER, checks.REJECTED_REVIEW,
                                     checks.STAYS_REJECTED)
                 else "" if g == checks.NOT_ASSESSED else "Approved")
        put(label, today, counts.get(g, 0), meaning, tab or "All customers", fill=fill)
    r += 1

    problems = Counter(rec["reason"] for rec in records if rec["checks"] is None)
    if problems:
        put("Why customers could not be assessed", bold=True)
        head("Reason", "", "Customers", "What to do", "")
        first_action = {}
        for rec in records:
            if rec["checks"] is None:
                first_action.setdefault(rec["reason"], rec["action"])
        for reason, n in problems.most_common():
            put(reason, "", n, first_action[reason])
        put("The Could not assess tab has the exact reason for each row.")
        r += 1

    put("The checks", bold=True)
    head("Check", "", "", "Pass  ·  Refer (credit head)  ·  Fail", "")
    rules = {
        "D1": f"Pass: no fraud cases and no bounced cheques since {cutoff}.  Fail: any fraud case, or a bounced "
              f"cheque since {cutoff}.",
        "D2": f"Pass: no open non-performing loan with money owing (loans listed from {cutoff}).  Fail: any.",
        "D4": "Pass: no write-offs.  Fail: any write-off, paid or unpaid, any amount, any date.",
        "D5": f"Pass: every closed non-performing loan was cleared (last payment) before {cutoff} and its term "
              f"ended before then.  Refer: term ended or last payment on or after {cutoff}, loan of up to "
              f"RWF {checks.D5_REFER_MAX_PRINCIPAL:,}.  Fail: the same, on a larger loan.",
        "D6": f"Each open loan listed from {cutoff}.  Pass: 0–{checks.D6_ALLOWED_MAX_DPD} days overdue and arrears up "
              f"to RWF {checks.D6_ALLOWED_MAX_ARREARS:,}.  Refer: {checks.D6_ALLOWED_MAX_DPD + 1}–"
              f"{checks.D6_REFER_MAX_DPD} days overdue and arrears up to RWF {checks.D6_REFER_MAX_ARREARS:,}.  "
              "Fail: anything beyond Refer.",
    }
    for code in CODES:
        put(f"{code} {TITLES[code]}", "", "", rules[code])
    put("? Check", "", "", "The CRB report is missing a date or figure the check needs; look at it by hand.")
    put("Overall", "", "", "A customer's outcome follows the worst check: Fail, then Check, then Refer, then Pass.")
    r += 1

    put("How to read the other tabs", bold=True)
    for k, v in [("All customers", "Every intake row, with its outcome, what to do, the reason account by account "
                                   "(Why), and the result of each check. Use the filter arrows in the header."),
                 ("Outcome tabs", "The same columns, only the customers with that outcome."),
                 ("Could not assess", "The exact reason each row couldn't be assessed and what to do about it."),
                 ("Loan details", "Every loan on each CRB report. Flag shows the loans that affected the result.")]:
        put(k, "", "", v)


def write(records: List[dict], path: Path, info: dict, policy) -> None:
    """info: source, account; optionally source_label and row_label for a folder of PDFs."""
    row_label = info.get("row_label", "Intake row")
    main_cols = [(row_label, MAIN_COLUMNS[0][1])] + MAIN_COLUMNS[1:]
    loan_cols = [(row_label, LOAN_COLUMNS[0][1])] + LOAN_COLUMNS[1:]
    order = {g: i for i, g in enumerate(checks.GROUPS)}
    records = sorted(records, key=lambda rec: (order[rec["group"]], rec["row"]))
    wb = Workbook()
    _summary(wb.active, records, info, policy)
    wb.active.title = "Summary"

    for tab in ACTION_TABS:
        ws = wb.create_sheet(tab)
        recs = [rec for rec in records if OUTCOMES[rec["group"]][2] == tab]
        if tab == "Could not assess":
            cols = [(row_label, 9), ("Application ID", 14), ("Customer", 24), ("Reason", 26), ("Details", 60),
                    ("What to do", 60), ("CRB cell shows", 30), ("CRB link", 50)]
            _table(ws, cols, [[x["row"], x["app_id"], x["customer"], x["reason"], x["detail"], x["action"],
                               x["cell_text"], x["link"]] for x in recs])
        else:
            _table(ws, main_cols, [main_row(x) for x in recs], _colour_main)
        if not recs:
            ws.cell(2, 1, "None")

    _table(wb.create_sheet("All customers"), main_cols, [main_row(x) for x in records], _colour_main)
    _table(wb.create_sheet("Loan details"), loan_cols,
           [row for x in records for row in loan_rows(x, policy)], _colour_loans)
    wb.save(path)


def console(rec: dict) -> str:
    """The same reading for the terminal (--pdf mode)."""
    label, _, _, meaning = OUTCOMES[rec["group"]]
    head = f"{rec['crb_name'] or '?'} · band {rec['band'] or '?'} ({rec['today'] or 'no band'}) → {label}"
    return "\n".join([head, "  " + meaning, ""] + ["  " + line for line in why(rec).split("\n")])
