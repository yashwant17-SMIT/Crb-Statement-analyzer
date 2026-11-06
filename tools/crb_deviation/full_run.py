"""
One-off: every intake row through the whole application, as Ledger's
"Assess selected rows" would decide it, with the CRB deviation points D1-D5
on every band.

For each row it downloads the CRB report and the MoMo/BoK statements from
Drive, runs the live tool's own two steps (app.main.assess_bureau, then
assess_statements when the gate is open) and takes the ops-sheet decision
from app.intake.decision. The intake sheet and the live results workbook are
not touched. Output:

  - a new Google Sheet with each customer's Ledger report, written by the
    live tool's own report writer (sheets.plan), one tab per customer
  - a local .xlsx for ops: one line per customer, the decision, why in a
    few words, and a link to their report

    python full_run.py --sign-in                # once: allows creating the report sheet
    python full_run.py --sheet "<intake sheet URL>" --tab "<tab name>"
    python full_run.py --from-results           # rebuild the outputs without re-assessing
    python full_run.py --retry                  # re-assess only the rows that couldn't be

A row with no usable requested amount is assessed anyway (live intake stops
it): the statement then shows the full eligible amount.

Uses the read-only sign-in (token.json) and download cache (cache/) of run.py.
Results are kept in cache/full_run_results.json so --from-results needs no
downloads or parsing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import checks  # noqa: F401  (puts backend/ on the path)
from google_io import CannotAssess, download, drive_id, find_columns, key, read_tab, session, signed_in_as, spreadsheet_id
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

from report import AMBER, GREEN, RED, _fill, _table
from app import deviation, intake, intake_fields, sheets  # noqa: E402
from app.config import Policy  # noqa: E402
from app.crb_analyzer import _report_datetime  # noqa: E402
from app.main import MAX_STATEMENT_FILES, assess_bureau, assess_statements  # noqa: E402

# D5's "cleared at least 6 months ago" counts back from today in live Ledger.
# These reports were pulled when the customer applied, months ago, so here it
# counts back from each report's own date instead.
_d5_live = deviation.d5_closed_npa


def _d5_as_of_report(report, cutoff, policy, today=None):
    return _d5_live(report, cutoff, policy, today or _report_datetime(report.report_date))


deviation.d5_closed_npa = _d5_as_of_report

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "cache" / "full_run_results.json"
WRITE_TOKEN = HERE / "token_write.json"
# Only files this tool creates: it can't open anything else in Drive.
WRITE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

DECISIONS = {"approved": "Approved", "on_hold": "On hold", "rejected": "Rejected", None: "Can't assess"}
ORDER = ["Approved", "On hold", "Rejected", "Can't assess"]
FILL = {"Approved": GREEN, "On hold": AMBER, "Rejected": RED, "Can't assess": "D9D9D9"}
TODO = {"Approved": "Can be approved.",
        "On hold": "A credit officer decides, from the CRB and Statement columns and the report.",
        "Rejected": "Decline.",
        "Can't assess": "Fix what the CRB or Statement column says, then assess again."}


# ------------------------------------------------------------- assessing --
def columns(header: List[dict], cols: Dict[str, int]) -> Dict[str, object]:
    """The intake fields beyond run.py's, found by header as Code.gs finds them."""
    keys = [key(c["text"]) for c in header]

    def first(test) -> Optional[int]:
        return next((i for i, k in enumerate(keys) if k and i not in cols.values() and test(k)), None)

    out = dict(cols)
    out["statement"] = [i for i, k in enumerate(keys) if "statement" in k and not re.search("type|provider", k)]
    out["customer_type"] = first(lambda k: k in {"customertype", "type"}
                                 or ("type" in k and re.search("customer|client|borrower|applicant", k)))
    out["requested_amount"] = first(lambda k: "amount" in k and re.search("request|loan|applied", k)
                                    and not re.search("sanction|approv|current", k))
    out["provider"] = first(lambda k: k in {"provider", "statementprovider"})
    return out


def text(row: List[dict], i: Optional[int]) -> str:
    return row[i]["text"] if i is not None and i < len(row) else ""


def links(row: List[dict], i: Optional[int]) -> List[str]:
    return row[i]["links"] if i is not None and i < len(row) else []


def as_statement(e: CannotAssess) -> CannotAssess:
    """download() words its errors for the CRB file; say statement instead."""
    swap = lambda s: s.replace("CRB report PDF", "statement PDF").replace("CRB", "statement")
    return CannotAssess(swap(e.reason).capitalize(), swap(e.detail), swap(e.action))


def assess_row(sess, row: List[dict], cols: dict, n: int, account: str, policy: Policy) -> dict:
    rec = {"row": n, "app_id": text(row, cols.get("application_id")), "customer": text(row, cols.get("customer")),
           "today": text(row, cols.get("sheet_status")), "out": None, "decision": None, "error": "", "files": {},
           "no_amount": ""}
    cache = HERE / "cache"
    try:
        crb = [i for i in (drive_id(x) for x in links(row, cols["crb"])) if i]
        stmts = [i for c in cols["statement"] for i in (drive_id(x) for x in links(row, c)) if i]
        if not crb:
            raise CannotAssess("No CRB report", f"the CRB cell says \"{text(row, cols['crb']) or 'nothing'}\"", "")
        if not stmts:
            raise CannotAssess("No statement link", "the statement cell has no Drive link", "")
        if len(stmts) > MAX_STATEMENT_FILES:
            raise CannotAssess("Too many statements", f"{len(stmts)} links, at most {MAX_STATEMENT_FILES}", "")
        try:  # read strictly, as the intake route does, before anything is downloaded
            ctype = intake_fields.customer_type(text(row, cols["customer_type"]))
            prov = intake_fields.provider(text(row, cols["provider"]))
        except intake_fields.IntakeFieldError as e:
            raise CannotAssess("Sheet cell to fix", str(e).rstrip("."), "")
        try:  # the sheet shows "RF1,000,000"; Code.gs sends the cell's number
            amount = intake_fields.requested_amount(
                re.sub(r"(?i)^\s*(rwf|frw|rf)\s*", "", text(row, cols["requested_amount"])))
        except intake_fields.IntakeFieldError as e:
            # Live intake stops here. The back-test assesses anyway, without a
            # request, so the statement shows the full eligible amount.
            amount, rec["no_amount"] = None, str(e).rstrip(".")

        data, crb_name = download(sess, crb[0], cache, account)
        out, bureau = assess_bureau(crb_name, data, policy)
        rec["out"] = out  # kept if the statement then can't be used
        files = []
        if out["credit_bureau"]["gate"]["statement_parsing_allowed"]:
            for i in stmts:
                try:
                    files.append(download(sess, i, cache, account)[::-1])
                except CannotAssess as e:
                    raise as_statement(e)
            out = assess_statements(out, bureau, files, [prov or ""], policy,
                                    customer_type=ctype, requested_amount=amount)
        rec["files"] = {"crb": crb_name, "statement": ", ".join(name for name, _ in files)}
        rec["out"], rec["decision"] = out, intake.decision(out)
    except CannotAssess as e:
        rec["error"] = f"{e.reason}: {e.detail}"
    except Exception as e:  # a parse failure or anything else: record it and go on
        if type(e).__name__ == "StatementParseError":
            rec["error"] = "Statement can't be read: not a MoMo or BoK statement Ledger knows, or a scan"
        else:
            detail = getattr(e, "detail", None)
            rec["error"] = f"{type(e).__name__}: {detail.get('message') if isinstance(detail, dict) else e}"
    return rec


def assess_all(args, wanted: Optional[set] = None) -> tuple[List[dict], dict]:
    sess = session(HERE / "client_secret.json", HERE / "token.json")
    account = signed_in_as(sess)
    tab, rows = read_tab(sess, spreadsheet_id(args.sheet), args.tab)
    save = wanted is None  # retry() merges its rows into the saved results itself
    header, base = find_columns(rows)
    cols = columns(rows[header], base)
    for f in ("customer_type", "requested_amount"):
        if cols[f] is None:
            raise SystemExit(f"No {f.replace('_', ' ')} column in the header row.")
    if not cols["statement"]:
        raise SystemExit("No statement link column in the header row.")
    print(f'Signed in as {account}. Tab "{tab}", header on row {header + 1}.', file=sys.stderr)

    if args.rows:
        wanted = set()
        for part in args.rows.split(","):
            a, _, b = part.partition("-")
            wanted.update(range(int(a), int(b or a) + 1))
    apps = [(n, r) for n, r in enumerate(rows[header + 1:], start=header + 2)
            if (links(r, base["crb"]) or text(r, base["crb"]) or text(r, base.get("customer")))
            and (wanted is None or n in wanted)]

    # Every CRB and statement was pulled for its application, months ago; judged
    # against today the 15-day age limits would stop them all. The rest is as live.
    policy = Policy(crb_max_age_days=100_000, statement_max_age_days=100_000)
    records = []
    for k, (n, row) in enumerate(apps, 1):
        rec = assess_row(sess, row, cols, n, account, policy)
        records.append(rec)
        print(f"[{k}/{len(apps)}] row {n} {rec['customer']}: {ledger(rec)}"
              f"{' - ' + rec['error'] if rec['error'] else ''}", file=sys.stderr)
    info = {"source": args.sheet, "tab": tab, "run_on": f"{datetime.now():%d %b %Y}"}
    if save:
        RESULTS.write_text(json.dumps({"info": info, "records": records}, default=str))
    return records, info


def retry(args) -> tuple[List[dict], dict]:
    """Re-assess the saved rows that couldn't be assessed, and put them back in the saved results."""
    saved = json.loads(RESULTS.read_text())
    records, info = saved["records"], saved["info"]
    failed = {r["row"] for r in records if r["error"]}
    print(f"Re-assessing {len(failed)} rows that couldn't be assessed.", file=sys.stderr)
    args.sheet, args.tab, args.rows = info["source"], info["tab"], None
    redone = {r["row"]: r for r in assess_all(args, failed)[0]}
    records = [redone.get(r["row"], r) for r in records]
    RESULTS.write_text(json.dumps({"info": info, "records": records}, default=str))
    return records, info


REPORT_ONLY = {"PASS_THROUGH", "ROUND_NUMBERS", "AGENT_FLOAT", "UNDISCLOSED_BORROWING"}  # as app/analyzer.py now raises them


def report_only(records: List[dict]) -> int:
    """Bring results saved before the analyzer change in line with it.

    Round-tripping, round-number inflows, agent float and existing borrowing
    are shown but never severe. A flag's severity feeds no figure (only
    sorting and the decision), so lowering it and re-deciding gives what a
    fresh run would. Returns the
    number of rows whose decision changed.
    """
    order = {"severe": 0, "elevated": 1, "informational": 2}

    def walk(x):
        if isinstance(x, dict):
            if x.get("code") in REPORT_ONLY and x.get("severity") == "severe":
                x["severity"] = "elevated"
            for k, v in x.items():
                walk(v)
                if k == "red_flags" and isinstance(v, list):
                    v.sort(key=lambda f: order.get(f.get("severity"), 3))
        elif isinstance(x, list):
            for v in x:
                walk(v)

    changed = 0
    for rec in records:
        walk(rec["out"])
        if rec["decision"]:
            new = intake.decision(rec["out"])
            changed += new["decision"] != rec["decision"]["decision"]
            rec["decision"] = new
    return changed


# --------------------------------------------------------- in plain words --
def ledger(rec: dict) -> str:
    return DECISIONS[(rec["decision"] or {}).get("decision")]


def today(rec: dict) -> str:
    s = rec["today"].strip()
    for label, rx in (("Approved", "approv|accept|pass|sanction"), ("Rejected", "reject|declin|fail"),
                      ("On hold", "hold|pend|refer|review")):
        if re.search(rx, s, re.I):
            return label
    return "Not decided"


def _points(gate: dict, results: set) -> str:
    dev = gate.get("deviation") or {}
    return ", ".join(f"{c['code']} {c['title']}" for c in dev.get("checks") or [] if c["result"] in results)


GREY = "D9D9D9"
STEP_FILL = {"Pass": GREEN, "No credit data": GREEN, "Clean": GREEN, "Refer": AMBER, "Check": AMBER, "Flagged": AMBER,
             "Reject": RED, "Not eligible": RED, "Can't read": GREY, "Not checked": "FFFFFF"}
STEP_HELP = [
    ("CRB", "", "Pass: every deviation point D1–D5 is Allowed. Refer: within a Refer limit, the approving "
                "authority decides. Check: the report lacks a date or figure a point needs (e.g. when a "
                "closed NPA was cleared), or the points pass but another CRB finding (e.g. exposure above "
                "the credit cap) needs a credit officer. Reject: beyond a limit, so the statement is not checked."),
    ("Statement", "", "Clean: eligible with nothing to check. Flagged: eligible, but a severe flag (or a "
                      "missing figure) needs a credit officer. Not eligible: the statement doesn't support "
                      "a loan. Round-tripping, round-number inflows, agent float and existing borrowing are shown "
                      "in the report only; they don't hold a customer."),
    ("Ledger says", "", "Approved needs a CRB Pass and a Clean statement. Any Refer, Check or Flagged is On hold. "
                        "A CRB Reject or Not eligible is Rejected."),
]
STATEMENT_ERRORS = ("Statement", "No statement", "Too many statements")


def crb_result(rec: dict) -> tuple[str, str]:
    """(category, a line) for the CRB step."""
    if not rec["out"]:
        if rec["error"].startswith(STATEMENT_ERRORS):
            return "Not checked", "Not checked: the statement couldn't be used"
        if rec["error"].startswith("Sheet cell"):
            return "Not checked", "Not checked: fix the sheet first"
        return "Can't read", rec["error"] + "."
    crb = rec["out"]["credit_bureau"]
    gate = crb["gate"]
    other = [f["title"] for f in crb.get("findings") or []
             if f.get("severity") == "blocking" and f.get("code") != "DEVIATION_REFER"]
    hold = f". Also to check: {', '.join(other)}" if other else ""
    if not gate["statement_parsing_allowed"]:
        failed = _points(gate, {"reject"})
        return "Reject", (f"Reject: {failed}" if failed else f"Reject: {gate.get('reason')}")
    if gate.get("note"):
        return "No credit data", "Pass: no credit data on the report" + hold
    # Live Ledger holds both for the approving authority; a Review is not a
    # Refer limit, though, but a date or figure missing from the report.
    refer, review = _points(gate, {"refer"}), _points(gate, {"review"})
    if refer:
        also = f". Also can't tell from the report: {review}" if review else ""
        return "Refer", f"Refer to approving authority: {refer}" + also + hold
    if review:
        return "Check", f"Check by hand, the report lacks a date or figure: {review}" + hold
    if other:
        return "Check", "Points all Allowed, but check: " + ", ".join(other)
    return "Pass", "Pass: every deviation point Allowed"


def statement_result(rec: dict) -> tuple[str, str]:
    """(category, a line) for the statement step."""
    if not rec["out"]:
        if rec["error"].startswith(STATEMENT_ERRORS) or rec["error"].startswith("Sheet cell"):
            return "Can't read", rec["error"] + "."
        return "Not checked", "Not checked"
    if not rec["out"]["credit_bureau"]["gate"]["statement_parsing_allowed"]:
        return "Not checked", "Not checked: stopped at the CRB"
    if rec["error"]:
        return "Can't read", rec["error"] + "."
    rep = rec["out"].get("statement") or {}
    el = (rep.get("summary") or {}).get("loan_eligibility") or {}
    if el.get("status") == "not_eligible":
        return "Not eligible", "Not eligible: " + (el.get("reason") or "").rstrip(".")
    issues = list(dict.fromkeys(re.sub(r" — .*", "", f["title"]) for f in rep.get("red_flags") or []
                                if f.get("severity") == "severe"))
    if (rep.get("identity_check") or {}).get("status") == "mismatch":
        issues.append("not in the applicant's name")
    if (rep.get("meta") or {}).get("parse_complete") is False:
        issues.append("not read in full")
    if el.get("status") != "eligible":
        return "Flagged", "Loan amount not worked out: " + (el.get("reason") or "see the report").rstrip(".")
    amount = f"Eligible for {el['sanctioned_amount']:,} RWF" if el.get("sanctioned_amount") else "Eligible"
    if rec.get("no_amount"):
        amount += " (no requested amount on the sheet, so the full eligible amount)"
    return ("Flagged", f"{amount}, but flagged: {', '.join(issues)}") if issues else ("Clean", amount)


# ------------------------------------------------------- report sheet ----
def write_session():
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = Credentials.from_authorized_user_file(str(WRITE_TOKEN), WRITE_SCOPES) if WRITE_TOKEN.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        creds = InstalledAppFlow.from_client_secrets_file(
            str(HERE / "client_secret.json"), WRITE_SCOPES).run_local_server(port=0)
        WRITE_TOKEN.write_text(creds.to_json())
    return AuthorizedSession(creds)


def _ok(res, what: str):
    if res.status_code >= 300:
        raise SystemExit(f"{what} failed: HTTP {res.status_code} {res.text[:300]}")
    return res.json()


def write_reports(records: List[dict], info: dict) -> str:
    """One new spreadsheet, each customer's report in it; sets rec['link']. Returns its URL."""
    sess = write_session()
    title = f"Ledger back-test reports – {info['tab']} – {info['run_on']}"
    book = _ok(sess.post(SHEETS_API, json={"properties": {"title": title}}), "Creating the report sheet")
    sid, first_gid = book["spreadsheetId"], book["sheets"][0]["properties"]["sheetId"]
    base = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    tabs: Dict[str, int] = {}
    register: List[tuple] = []
    done = [r for r in records if r["out"]]
    for k, rec in enumerate(done, 1):
        out = rec["out"]
        plan = sheets.plan(out["credit_bureau"], out.get("statement"), rec["files"], tabs, register)
        for attempt in range(5):  # Sheets allows 60 writes a minute
            res = sess.post(f"{SHEETS_API}/{sid}:batchUpdate", json={"requests": plan["ops"]})
            if res.status_code != 429:
                break
            time.sleep(20)
        _ok(res, f"Writing the report for row {rec['row']}")
        for name, gid in sheets.TAB_IDS.items():
            tabs.setdefault(name, gid)
        tabs[plan["tab"]] = plan["gid"]
        register = [(t, "no" if t.casefold() == plan["tab"].casefold() else c) for t, c in register]
        register.append((plan["tab"], "yes"))
        rec["link"] = f"{base}#gid={plan['gid']}"
        print(f"  report {k}/{len(done)}: row {rec['row']}", file=sys.stderr)
        time.sleep(1.1)
    _ok(sess.post(f"{SHEETS_API}/{sid}:batchUpdate",
                  json={"requests": [{"deleteSheet": {"sheetId": first_gid}}]}), "Tidying the report sheet")
    return base


# ------------------------------------------------------------- workbook --
def write_workbook(records: List[dict], info: dict, reports_url: str, path: Path) -> None:
    order = {d: i for i, d in enumerate(ORDER)}
    records = sorted(records, key=lambda r: (order[ledger(r)], r["row"]))
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    bold, wrap = Font(bold=True), Alignment(wrap_text=True, vertical="top")
    ws.append(["Ledger back-test"]); ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"Every customer on the intake sheet (tab \"{info['tab']}\"), run through Ledger as "
               f"'Assess selected rows' would, with the new CRB deviation points. Run on {info['run_on']}. "
               "Nothing on the intake sheet was changed."])
    ws.append(["Each customer's full Ledger report:", reports_url])
    if reports_url:
        ws["B3"].hyperlink, ws["B3"].style = reports_url, "Hyperlink"
    ws.append([])
    ws.append(["Ledger says", "Customers", "What to do"])
    for c in ws[5]:
        c.font = bold
    counts = Counter(ledger(r) for r in records)
    for d in ORDER:
        ws.append([d, counts[d], TODO[d]])
        ws.cell(ws.max_row, 1).fill = _fill(FILL[d])
    ws.append([])
    changed = sum(ledger(r) not in (today(r), "Can't assess") for r in records)
    ws.append(["Different from today's decision", changed, "Filter the Customers tab on 'Changed'."])
    ws.append([])
    # How each customer got there: the CRB step across, the statement step down.
    crb_cats = ["Pass", "No credit data", "Check", "Refer", "Reject", "Can't read", "Not checked"]
    st_cats = ["Clean", "Flagged", "Not eligible", "Can't read", "Not checked"]
    pairs = Counter((crb_result(r)[0], statement_result(r)[0]) for r in records)
    crb_cats = [c for c in crb_cats if any(pairs[(c, s)] for s in st_cats)]
    st_cats = [s for s in st_cats if any(pairs[(c, s)] for c in crb_cats)]
    ws.append(["Statement ↓   CRB →"] + crb_cats)
    for c in ws[ws.max_row]:
        c.font = bold
    for j, c in enumerate(crb_cats, 2):
        ws.cell(ws.max_row, j).fill = _fill(STEP_FILL[c])
    for st in st_cats:
        ws.append([st] + [pairs[(c, st)] or "" for c in crb_cats])
        ws.cell(ws.max_row, 1).fill = _fill(STEP_FILL[st])
    ws.append([])
    for line in STEP_HELP:
        ws.append(list(line))
    ws.append([])
    ws.append(["Note", "", "The CRB reports and statements were pulled months ago, so the 15-day age limit on "
                           "them was not applied, and D5's 6 months are counted back from each CRB report's date (when "
                           "the customer applied), not from today. Everything else is as live Ledger decides."])
    ws.merge_cells("A2:C2")
    ws.row_dimensions[2].height = 45
    for row in ws.iter_rows():
        for c in row:
            c.alignment = wrap
    ws.column_dimensions["A"].width = 32
    for col in "BDEFG":
        ws.column_dimensions[col].width = 14
    ws.column_dimensions["C"].width = 80

    cols = [("Row", 6), ("Customer", 26), ("Application ID", 14), ("Today", 12), ("Ledger says", 13),
            ("Changed", 9), ("CRB", 45), ("Statement", 55), ("Report", 12)]
    rows = [[r["row"], r["customer"], r["app_id"], today(r), ledger(r),
             "" if ledger(r) == "Can't assess" else ("Changed" if ledger(r) != today(r) else ""),
             crb_result(r)[1], statement_result(r)[1],
             "Open report" if r.get("link") else ""] for r in records]

    def colour(sheet, i, values):
        c = sheet.cell(i, 5)
        c.fill, c.font = _fill(FILL[values[4]]), bold
        rec = records[i - 2]
        sheet.cell(i, 7).fill = _fill(STEP_FILL[crb_result(rec)[0]])
        sheet.cell(i, 8).fill = _fill(STEP_FILL[statement_result(rec)[0]])
        if rec.get("link"):
            sheet.cell(i, 9).hyperlink, sheet.cell(i, 9).style = rec["link"], "Hyperlink"

    cust = wb.create_sheet("Customers")
    _table(cust, cols, rows, colour)
    cust.freeze_panes = "C2"
    wb.save(path)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet", help="intake spreadsheet URL or ID")
    src.add_argument("--from-results", action="store_true", help="reuse cache/full_run_results.json")
    src.add_argument("--retry", action="store_true",
                     help="re-assess the saved rows that couldn't be assessed, keep the rest")
    src.add_argument("--sign-in", action="store_true", help="sign in to allow creating the report sheet")
    p.add_argument("--tab", help="tab with the applications (default: the first tab)")
    p.add_argument("--rows", help="only these sheet rows, e.g. 3-10,15 (for a trial)")
    p.add_argument("--no-reports", action="store_true", help="skip the Google report sheet")
    p.add_argument("--out", type=Path, help="output .xlsx (default: ledger_backtest_<date>.xlsx here)")
    args = p.parse_args(argv)
    if args.sign_in:
        write_session()
        return print(f"Signed in. Saved {WRITE_TOKEN.name}.", file=sys.stderr)

    if args.from_results:
        saved = json.loads(RESULTS.read_text())
        records, info = saved["records"], saved["info"]
        moved = report_only(records)
        RESULTS.write_text(json.dumps({"info": info, "records": records}, default=str))
        print(f"{moved} decisions changed by the report-only flags.", file=sys.stderr)
    elif args.retry:
        records, info = retry(args)
    else:
        records, info = assess_all(args)
    url = "" if args.no_reports else write_reports(records, info)
    out = args.out or HERE / f"ledger_backtest_{datetime.now():%Y-%m-%d}.xlsx"
    write_workbook(records, info, url, out)
    for label, k in Counter(ledger(r) for r in records).most_common():
        print(f"  {k:4}  {label}", file=sys.stderr)
    if url:
        print(f"Reports: {url}", file=sys.stderr)
    print(f"Wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
