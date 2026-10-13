"""
Intake sheet, pushed by Apps Script.

Ops queue applications as rows in a Google Sheet. A script bound to that
sheet (apps-script/Code.gs) runs as the sheet's owner, fetches each row's
PDFs from Drive and posts them to /api/v1/intake/assess. The service holds no
Google credentials for this: what the script can open is what its owner can
open in Drive.

The route is closed unless INTAKE_TOKEN is set, and every call must carry it
in the X-Intake-Token header. The response is shaped for the script, which
writes it straight into the sheet: the row's status and one-line result, a
Register row, and the customer's report as plain cell values.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime
from typing import Any, Optional

from . import sheets

TOKEN = os.getenv("INTAKE_TOKEN", "").strip()


def configured() -> bool:
    return bool(TOKEN)


def token_ok(header: Optional[str]) -> bool:
    return bool(TOKEN) and secrets.compare_digest((header or "").encode(), TOKEN.encode())


def outcome(out: dict) -> tuple[str, str]:
    """(Ledger status, Ledger result) for an assessment response."""
    gate = (out.get("credit_bureau") or {}).get("gate") or {}
    band = gate.get("band") or "?"
    if not gate.get("statement_parsing_allowed"):
        return "Blocked at CRB", f"Band {band}: {gate.get('reason') or 'blocked by credit policy'}"
    if out.get("decision") == "awaiting_statement":
        return "Awaiting statement", f"Band {band} clears. No statement link to assess."
    rep = out.get("statement") or {}
    el = (rep.get("summary") or {}).get("loan_eligibility") or {}
    flags = rep.get("red_flags") or []
    severe = sum(1 for f in flags if f.get("severity") == "severe")
    parts = [f"Band {band}", sheets._verdict(rep),
             f"Eligibility {sheets._human(el.get('status')) or '—'}"]
    if el.get("sanctioned_amount") is not None:
        parts.append(f"Sanctioned {el['sanctioned_amount']:,} RWF")
    if severe:
        parts.append(f"{severe} severe finding{'s' if severe != 1 else ''}")
    return "Done", " · ".join(parts)


def _plain(v: Any) -> tuple[Any, Optional[str]]:
    """A cell value Apps Script can write as-is, and its number format."""
    if isinstance(v, sheets.Pct):
        return float(v), "0.0%"
    if isinstance(v, sheets.Stamp):
        return float(v), "yyyy-mm-dd hh:mm"
    if v is None:
        return "", None
    return v, None


def _table(rows: list[list[Any]]) -> dict:
    """Rows of plain values, plus [row, col, format] for the cells that need one."""
    values, formats = [], []
    for r, row in enumerate(rows):
        out = []
        for c, v in enumerate(row):
            plain, fmt = _plain(v)
            out.append(plain)
            if fmt:
                formats.append([r, c, fmt])
        values.append(out)
    return {"values": values, "formats": formats}


def for_sheet(out: dict, files: dict) -> dict:
    """Everything the script writes back: status, Register row, report tab."""
    crb, report = out.get("credit_bureau"), out.get("statement")
    at = datetime.now().replace(microsecond=0)
    aid = sheets.new_assessment_id(at)
    title = sheets.tab_title(crb, report)
    status, result = outcome(out)

    # The Register as the export writes it, less the columns underwriters own
    # and the customer link, which the script points at its own tab.
    cols = [c for c in sheets.REGISTER_COLS if not c.human and c.header not in ("Customer", "Current")]
    full = dict(zip((c.header for c in sheets.REGISTER_COLS),
                    sheets.register_row(aid, at, title, 0, crb, report)))
    register = _table([[full[c.header] for c in cols]])

    lay = sheets.build_layout(crb, report, files, aid, at)
    return {
        "assessment_id": aid,
        "status": status,
        "result": result,
        "register": {"headers": [c.header for c in cols], **register},
        "report": {"title": title, "kinds": lay.kind, **_table(lay.rows)},
    }
