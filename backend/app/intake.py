"""
Intake sheet, pushed by Apps Script.

Ops queue applications as rows in a Google Sheet. A script bound to that
sheet (apps-script/Code.gs) runs as the sheet's owner, fetches each row's
PDFs from Drive and posts them to /api/v1/intake/assess. The service holds no
Google credentials for this: what the script can open is what its owner can
open in Drive.

The route is closed unless INTAKE_TOKEN is set, and every call must carry it
in the X-Intake-Token header.

Output lands in the results workbook exactly as the Sheets export writes it
(Register, detail tabs, the customer's formatted report), but the service
does not write it. The script sends the workbook's current tabs and Register
column, the service answers with the one batchUpdate that records the
assessment (sheets.plan), and the script applies it as the sheet owner. The
intake row then gets the status and a link to the customer's tab.
"""
from __future__ import annotations

import json
import os
import secrets
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


class StateError(ValueError):
    """The results-workbook state the script sent cannot be used."""


def parse_state(raw: Any) -> tuple[dict[str, int], list[tuple[str, str]]]:
    """(tab title -> sheet ID, Register (Customer, Current) pairs) from the script."""
    try:
        state = json.loads(raw) if isinstance(raw, str) else None
        tabs = {str(t): int(g) for t, g in state["tabs"].items()}
        register = [(str(c or ""), str(x or "")) for c, x in state.get("register") or []]
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise StateError("results_state is missing or malformed. Update Code.gs to the "
                         "version that ships with this server.") from exc
    return tabs, register


def for_sheet(out: dict, files: dict, tabs: dict[str, int],
              register: list[tuple[str, str]]) -> dict:
    """The row's status and result, and the results-workbook write."""
    status, result = outcome(out)
    return {"status": status, "result": result,
            "results": sheets.plan(out["credit_bureau"], out.get("statement"), files, tabs, register)}
