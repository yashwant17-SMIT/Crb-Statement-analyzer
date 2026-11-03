"""
One-off back-test: score every customer on the intake sheet against the CRB
deviation points D1-D5 (the live tool's rules), and write an Excel file for the ops team that separates

  - customers rejected on band (EE-JJ) who would pass every deviation point
  - customers approved on band (AA-DD) who would fail one

Separate from the live tool: it reads the sheet and the bureau PDFs, and
writes nothing anywhere but the output file. See README.md.

    python run.py --sheet "<intake sheet URL>" [--tab "Applications"]
    python run.py --pdf crb1.pdf crb2.pdf      # quick test on local files
    python run.py --pdf "CRB folder"           # a folder of PDFs, to a workbook
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import checks
import report
from google_io import (CannotAssess, download, drive_id, find_columns, read_tab, session,
                       signed_in_as, spreadsheet_id)
from app.config import Policy  # noqa: E402  (checks puts backend/ on the path)
from app.crb_parser import ParsedCreditReport, parse_credit_report  # noqa: E402

HERE = Path(__file__).resolve().parent

# What a CRB cell says instead of a link when the bureau had nothing for the customer.
NO_RECORD = re.compile(r"no\s*(record|crb|result|data|hit)|not\s*found|invalid\s*crn|does\s*not\s*exist", re.I)


def empty_record(n: int, app_id: str = "", customer: str = "", cell_text: str = "") -> dict:
    return {"row": n, "app_id": app_id, "customer": customer, "cell_text": cell_text, "link": "",
            "file_name": "", "crb_name": "", "national_id": "", "report_date": "", "band": "", "today": "",
            "group": checks.NOT_ASSESSED, "checks": None, "report": None,
            "reason": "", "detail": "", "action": "", "note": ""}


def cannot(rec: dict, e: CannotAssess) -> dict:
    rec.update(group=checks.NOT_ASSESSED, checks=None, reason=e.reason, detail=e.detail, action=e.action)
    return rec


def score(rec: dict, rep: ParsedCreditReport, policy: Policy) -> dict:
    """Fill a record from a parsed report."""
    rec.update(report=rep, crb_name=rep.profile.names or "", national_id=rep.profile.national_id or "",
               report_date=rep.report_date or "", band=rep.band or "")
    ev = checks.evaluate(rep, policy)
    rec.update(today=ev["band_rule"], group=ev["group"], checks=ev["checks"], note=ev["note"])
    return rec


def parse(data: bytes, name: str) -> ParsedCreditReport:
    try:
        return parse_credit_report(data)
    except Exception as e:
        raise CannotAssess("CRB report could not be read", f"'{name or 'The file'}': {e}",
                           "Open the PDF. If it is a scan, a photo or not a TransUnion Rwanda report, "
                           "assess this customer by hand.")


def assess_row(sess, row: List[dict], cols: Dict[str, int], n: int, cache: Path, account: str,
               policy: Policy) -> dict:
    def cell(field: str) -> dict:
        i = cols.get(field)
        return row[i] if i is not None and i < len(row) else {"text": "", "links": []}

    crb = cell("crb")
    rec = empty_record(n, cell("application_id")["text"], cell("customer")["text"], crb["text"])
    try:
        if not crb["text"] and not crb["links"]:
            raise CannotAssess("CRB cell is empty", "There is no CRB link in this row.",
                               "Paste the Drive link to the customer's CRB PDF into the CRB column.")
        ids = [i for i in (drive_id(link) for link in crb["links"]) if i]
        if not ids and crb["links"]:
            raise CannotAssess("CRB link is not a Drive file", f"The CRB cell links to {crb['links'][0]}.",
                               "Replace it with the Google Drive link to the CRB PDF itself (not a folder).")
        if not ids and NO_RECORD.search(crb["text"]):
            raise CannotAssess("No CRB report for this customer", f"The CRB cell says \"{crb['text']}\".",
                               "The bureau seems to have had no record. The deviation checks need a CRB "
                               "report, so decide this customer by hand.")
        if not ids:
            raise CannotAssess("No link in the CRB cell", f"The CRB cell has text but no link: \"{crb['text']}\".",
                               "Paste the Drive link to the customer's CRB PDF into the CRB column.")
        rec["link"] = f"https://drive.google.com/file/d/{ids[0]}/view"
        data, rec["file_name"] = download(sess, ids[0], cache, account)
        return score(rec, parse(data, rec["file_name"]), policy)
    except CannotAssess as e:
        return cannot(rec, e)
    except Exception as e:  # anything unexpected: say so, keep going
        return cannot(rec, CannotAssess("Unexpected error", f"{type(e).__name__}: {e}",
                                        "Run the report again. If it repeats, send this message to the developer."))


def pdf_paths(paths: List[Path]) -> List[Path]:
    """The PDFs given, with each folder expanded to the PDFs inside it."""
    out: List[Path] = []
    for path in paths:
        out += sorted(f for f in path.rglob("*") if f.suffix.lower() == ".pdf") if path.is_dir() else [path]
    return out


def score_files(paths: List[Path], policy: Policy, out: Path = None) -> None:
    """Score local CRB PDFs (files or folders): print each one, and with several write a workbook."""
    files = pdf_paths(paths)
    records = []
    for n, path in enumerate(files, 1):
        rec = empty_record(n)
        rec["file_name"] = path.name
        try:
            data = path.read_bytes()
            rec = score(rec, parse(data, path.name), policy)
        except CannotAssess as e:
            rec = cannot(rec, e)
        rec["customer"] = rec["crb_name"] or path.stem
        records.append(rec)
        print(f"\n[{n}/{len(files)}] {path.name}")
        if rec["checks"] is None:
            print(f"  Could not assess: {rec['reason']}. {rec['detail']}\n  What to do: {rec['action']}")
        else:
            print("  " + report.console(rec).replace("\n", "\n  "))
    if out is None and len(files) <= 3:
        return
    folders = sorted({str(p) for p in paths if p.is_dir()})
    name = Path(folders[0]).name.replace(" ", "_") if len(folders) == 1 else "pdfs"
    out = out or HERE / f"crb_deviation_{name}_{datetime.now():%Y-%m-%d}.xlsx"
    report.write(records, out, {"source": ", ".join(folders) or f"{len(files)} PDF files",
                                "source_label": "CRB reports from", "row_label": "#"}, policy)
    counts = Counter(report.OUTCOMES[r["group"]][0] + (f" ({r['today']} today)" if r["today"] else "")
                     for r in records)
    for label, k in counts.most_common():
        print(f"  {k:4}  {label}", file=sys.stderr)
    print(f"Wrote {out}", file=sys.stderr)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet", help="intake spreadsheet URL or ID")
    src.add_argument("--pdf", nargs="+", type=Path, help="score these local CRB PDFs or folders of them instead (no Google)")
    p.add_argument("--tab", help="tab with the applications (default: the first tab)")
    p.add_argument("--crb-column", help="header of the CRB link column, if not found automatically")
    p.add_argument("--out", type=Path, help="output .xlsx (default: crb_deviation_<date>.xlsx here)")
    p.add_argument("--client-secret", type=Path, default=HERE / "client_secret.json")
    args = p.parse_args(argv)
    policy = Policy()
    if args.pdf:
        return score_files(args.pdf, policy, args.out)

    sess = session(args.client_secret, HERE / "token.json")
    account = signed_in_as(sess)
    sheet_id = spreadsheet_id(args.sheet)
    tab, rows = read_tab(sess, sheet_id, args.tab)
    header, cols = find_columns(rows, args.crb_column)
    print(f'Signed in as {account}. Tab "{tab}", header on row {header + 1}: ' +
          ", ".join(f"{f}={rows[header][i]['text']!r}" for f, i in cols.items()), file=sys.stderr)

    # An application has a CRB cell or a customer name; a pre-numbered empty row has neither.
    def filled(r: List[dict], field: str) -> bool:
        i = cols.get(field)
        return i is not None and i < len(r) and bool(r[i]["text"] or r[i]["links"])
    apps = [(n, r) for n, r in enumerate(rows[header + 1:], start=header + 2)
            if filled(r, "crb") or filled(r, "customer")]

    records = []
    for k, (n, row) in enumerate(apps, 1):
        rec = assess_row(sess, row, cols, n, HERE / "cache", account, policy)
        records.append(rec)
        label = report.OUTCOMES[rec["group"]][0]
        print(f"[{k}/{len(apps)}] row {n} {rec['customer'] or rec['crb_name']}: {label}"
              f"{' - ' + rec['reason'] if rec['reason'] else ''}", file=sys.stderr)

    out = args.out or HERE / f"crb_deviation_{datetime.now():%Y-%m-%d}.xlsx"
    report.write(records, out, {"source": f"{args.sheet}  (tab: {tab})", "account": account}, policy)
    counts = Counter(report.OUTCOMES[r["group"]][0] + (f" ({r['today']} today)" if r["today"] else "")
                     for r in records)
    for label, n in counts.most_common():
        print(f"  {n:4}  {label}", file=sys.stderr)
    print(f"Wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
