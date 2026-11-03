"""
One-off back-test: score every customer on the intake sheet against the CRB
deviation points D1-D6, and write an Excel file that separates

  - customers rejected on band (EE-JJ) who would pass every deviation point
  - customers approved on band (AA-DD) who would fail one

Separate from the live tool: it reads the sheet and the bureau PDFs, and
writes nothing anywhere but the output file. See README.md.

    python run.py --sheet "<intake sheet URL>" [--tab "Applications"]
    python run.py --pdf crb1.pdf crb2.pdf      # quick test on local files
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pandas as pd

import checks
from google_io import download, drive_id, find_columns, read_tab, session, spreadsheet_id
from app.config import Policy  # noqa: E402  (checks puts backend/ on the path)
from app.crb_parser import parse_credit_report  # noqa: E402

HERE = Path(__file__).resolve().parent
CODES = ["D1", "D2", "D4", "D5", "D6"]
TITLES = {"D1": "Fraud / bounced cheque", "D2": "Open NPA", "D4": "Write-off",
          "D5": "Closed NPA", "D6": "Open accounts: DPD and arrears"}


def assess_row(sess, row: List[dict], cols: Dict[str, int], n: int, cache: Path) -> dict:
    def cell(field: str) -> dict:
        i = cols.get(field)
        return row[i] if i is not None and i < len(row) else {"text": "", "links": []}

    out = {"Sheet row": n, "Application ID": cell("application_id")["text"],
           "Customer (sheet)": cell("customer")["text"], "Customer (CRB)": "", "National ID": "",
           "Report date": "", "Sheet band": cell("sheet_band")["text"],
           "Sheet status": cell("sheet_status")["text"], "CRB band": "", "Band rule": "",
           **{c: "" for c in CODES}, "Overall": "", "Group": checks.NOT_ASSESSED,
           **{f"{c} detail": "" for c in CODES}, "Note": "", "CRB link": ""}
    crb = cell("crb")
    ids = [i for i in (drive_id(link) for link in crb["links"]) if i]
    if not ids:
        out["Note"] = (f'No Drive file link in the CRB cell (it shows "{crb["text"]}").'
                       if crb["text"] else "The CRB cell is empty.")
        return out
    out["CRB link"] = f"https://drive.google.com/file/d/{ids[0]}/view"
    try:
        report = parse_credit_report(download(sess, ids[0], cache))
    except Exception as e:  # one bad file must not stop the run
        out["Note"] = str(e)
        return out
    ev = checks.evaluate(report)
    out.update({"Customer (CRB)": report.profile.names or "", "National ID": report.profile.national_id or "",
                "Report date": report.report_date or "", "CRB band": ev["band"] or "",
                "Band rule": ev["band_rule"], "Overall": ev["overall"], "Group": ev["group"]})
    for c in ev["checks"]:
        out[c.code], out[f"{c.code} detail"] = c.result, c.detail
    if ev["band_rule"] == "No band":
        out["Note"] = "No band could be read from the report."
    return out


def summary(df: pd.DataFrame, source: str, policy: Policy) -> pd.DataFrame:
    counts = df["Group"].value_counts()
    rows = [(g, int(counts.get(g, 0))) for g in checks.GROUPS]
    rows += [("", ""), ("Customers", len(df))]
    rows += [("", ""), ("Rejects among band-rejected, by point", "")]
    rejected = df[df["Band rule"] == "Rejected"]
    rows += [(f"{c} {TITLES[c]}", int((rejected[c] == checks.REJECT).sum())) for c in CODES]
    rows += [("", ""), ("Settings", ""), ("Source", source), ("Run at", f"{datetime.now():%Y-%m-%d %H:%M}"),
             ("Minimum approved band", policy.min_band), ("Loans considered from", policy.crb_history_cutoff),
             ("D6 Allowed", f"<= {checks.D6_ALLOWED_MAX_DPD} days past due and arrears <= "
                            f"RWF {checks.D6_ALLOWED_MAX_ARREARS:,}"),
             ("D6 Refer", f"{checks.D6_ALLOWED_MAX_DPD + 1}-{checks.D6_REFER_MAX_DPD} days past due and "
                          f"arrears <= RWF {checks.D6_REFER_MAX_ARREARS:,}; anything beyond is Reject"),
             ("D5", "Cleared before the cutoff is Allowed even when the maturity is unknown"),
             ("Refer", "Goes to the credit head."),
             ("Review", "The report lacks a date or figure the rule needs.")]
    return pd.DataFrame(rows, columns=["", "Customers"])


def write(df: pd.DataFrame, summ: pd.DataFrame, path: Path) -> None:
    sheets = {"Summary": summ,
              "Could be approved": df[df["Group"] == checks.COULD_APPROVE],
              "Would be rejected": df[df["Group"] == checks.WOULD_REJECT],
              "Refer to credit head": df[df["Group"].isin([checks.REJECTED_REFER, checks.APPROVED_REFER])],
              "Needs a look": df[df["Group"].isin([checks.REJECTED_REVIEW, checks.APPROVED_REVIEW])],
              "All customers": df}
    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        for name, frame in sheets.items():
            frame.to_excel(xl, sheet_name=name, index=False)
            ws = xl.sheets[name]
            ws.freeze_panes = "A2"
            for col in ws.columns:
                width = max(len(str(c.value or "")) for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)


def score_files(paths: List[Path]) -> None:
    """Score local CRB PDFs and print each check: a quick test, no Google needed."""
    for path in paths:
        print(f"\n{path.name}")
        try:
            report = parse_credit_report(path.read_bytes())
        except Exception as e:
            print(f"  Could not read: {e}")
            continue
        ev = checks.evaluate(report)
        print(f"  {report.profile.names or '?'} · band {ev['band'] or '?'} ({ev['band_rule']}) "
              f"· overall {ev['overall']} · {ev['group']}")
        for c in ev["checks"]:
            print(f"  {c.code} {TITLES[c.code]:<32} {c.result:<8} {c.detail}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet", help="intake spreadsheet URL or ID")
    src.add_argument("--pdf", nargs="+", type=Path, help="score these local CRB PDFs instead (no Google)")
    p.add_argument("--tab", help="tab with the applications (default: the first tab)")
    p.add_argument("--crb-column", help="header of the CRB link column, if not found automatically")
    p.add_argument("--out", type=Path, help="output .xlsx (default: crb_deviation_<date>.xlsx here)")
    p.add_argument("--client-secret", type=Path, default=HERE / "client_secret.json")
    args = p.parse_args(argv)
    if args.pdf:
        return score_files(args.pdf)

    sess = session(args.client_secret, HERE / "token.json")
    sheet_id = spreadsheet_id(args.sheet)
    tab, rows = read_tab(sess, sheet_id, args.tab)
    header, cols = find_columns(rows, args.crb_column)
    print(f'Tab "{tab}", header on row {header + 1}: ' +
          ", ".join(f"{f}={rows[header][i]['text']!r}" for f, i in cols.items()), file=sys.stderr)

    # An application has a CRB cell or a customer name; a pre-numbered empty row has neither.
    def filled(r: List[dict], field: str) -> bool:
        i = cols.get(field)
        return i is not None and i < len(r) and bool(r[i]["text"] or r[i]["links"])
    apps = [(n, r) for n, r in enumerate(rows[header + 1:], start=header + 2)
            if filled(r, "crb") or filled(r, "customer")]
    results = []
    for k, (n, row) in enumerate(apps, 1):
        res = assess_row(sess, row, cols, n, HERE / "cache")
        results.append(res)
        print(f"[{k}/{len(apps)}] row {n} {res['Customer (sheet)'] or res['Customer (CRB)']}: "
              f"{res['Group']}{' - ' + res['Note'] if res['Note'] else ''}", file=sys.stderr)

    df = pd.DataFrame(results)
    out = args.out or HERE / f"crb_deviation_{datetime.now():%Y-%m-%d}.xlsx"
    summ = summary(df, f"{args.sheet} / {tab}", Policy())
    write(df, summ, out)
    print(summ.head(len(checks.GROUPS)).to_string(index=False, header=False), file=sys.stderr)
    print(f"Wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
