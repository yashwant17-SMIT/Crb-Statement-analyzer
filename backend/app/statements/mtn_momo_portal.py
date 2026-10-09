"""
MTN Rwanda MoMo statement, portal/merchant export printed to PDF from Excel.

  Id | External id | Date | Status | Type | From | From name | To | To name |
  From / Fee | Amount | Balance | Currency

Differs from both other MoMo formats:

  * Party cells are wallet URIs - 'FRI:250788751206/MSISDN', 'FRI:23671830/MM'
    (merchant code), 'FRI:250788751206@airtime.mtn/SP' (service provider).
  * Amount is signed from the account holder's side, so direction is stated.
  * Dates are US month-first ('9/22/2026 14:05'), with no seconds.
  * There is no header block - no customer, period or declared count. The
    account is inferred as the party present on (almost) every row.

And one quirk that defeats pdfplumber's own table extraction: Excel prints a
long name's clipped overflow into the next cell, so the glyphs of
'NYIRAMATABARO PERUTH' and 'FRI:250788751206/MSISDN' overlap in the To column
and extract_tables yields 'BFARRI:O25 P0E7R8U8T7H51206/MSISDN'. Each cell is
still emitted as one continuous run of glyphs in the content stream, so cells
are rebuilt from runs, each attributed to the column its first glyph sits in.
"""
from __future__ import annotations

import bisect
import collections
import re
from datetime import datetime
from typing import List, Optional

from .base import (
    Capabilities, ParsedStatement, RejectedRow, StatementHeader,
    StatementParseError, Txn, clean, dedupe_name, to_int,
)

NAME = "mtn_momo_portal_v1"
PROVIDER = "mtn_momo"
LABEL = "MTN MoMo - portal/merchant export"

_COLS = 13
(_ID, _EXT_ID, _DATE, _STATUS, _TYPE, _FROM, _FROM_NAME, _TO, _TO_NAME,
 _FEE, _AMT, _BALANCE, _CURRENCY) = range(_COLS)

_TXID_RE = re.compile(r"^\d{6,}$")
_PARTY_RE = re.compile(r"^FRI:(.+?)(?:/[A-Z]+)?$")
_OK_STATUSES = {"SUCCESSFUL", "SUCCESS", "COMPLETED", "COMMITTED"}

# Glyphs closer than this (pt) along a line are the same run of text.
_RUN_GAP = 0.6


def sniff(first_page_text: str) -> float:
    t = re.sub(r"\s+", " ", (first_page_text or "").lower())
    if "external id" in t and "from name" in t and "to name" in t:
        return 0.97 if "fri:" in t else 0.8
    return 0.0


def _party_id(cell: str) -> str:
    """'FRI:250788751206/MSISDN' -> '250788751206'; unknown shapes kept whole."""
    m = _PARTY_RE.match(cell.replace(" ", ""))
    return m.group(1) if m else cell


def _to_iso(value: str) -> str:
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).isoformat()
        except ValueError:
            continue
    raise StatementParseError(f"Unrecognised date format: {value!r}")


def _page_rows(page) -> List[List[str]]:
    """Rebuild every table row on the page from glyph runs (see module doc)."""
    out: List[List[str]] = []
    for table in page.find_tables():
        full = next((row.cells for row in table.rows
                     if len(row.cells) == _COLS and all(row.cells)), None)
        if full is None:
            continue
        edges = [c[0] for c in full] + [full[-1][2]]
        for row in table.rows:
            top, bottom = row.bbox[1], row.bbox[3]
            runs: List[list] = []
            for ch in page.chars:  # content-stream order
                if not top <= (ch["top"] + ch["bottom"]) / 2 < bottom:
                    continue
                prev = runs[-1][-1] if runs else None
                if (prev and abs(ch["x0"] - prev["x1"]) < _RUN_GAP
                        and abs(ch["top"] - prev["top"]) < 1):
                    runs[-1].append(ch)
                else:
                    runs.append([ch])
            cells = [""] * _COLS
            for run in runs:
                col = bisect.bisect_right(edges, run[0]["x0"] + 0.5) - 1
                col = max(0, min(_COLS - 1, col))
                cells[col] = f"{cells[col]} {''.join(c['text'] for c in run)}".strip()
            out.append(cells)
    return out


def _infer_account(rows: List[List[str]]) -> str:
    counts: collections.Counter = collections.Counter()
    for r in rows:
        counts[_party_id(r[_FROM])] += 1
        counts[_party_id(r[_TO])] += 1
    counts.pop("", None)
    if not counts:
        raise StatementParseError("No transaction parties found in the statement.")
    account, hits = counts.most_common(1)[0]
    if hits < len(rows) * 0.8:
        raise StatementParseError(
            "Could not identify the account this statement belongs to: no single "
            f"number appears on most rows (best candidate {account} on {hits} of "
            f"{len(rows)})."
        )
    return account


def parse(pdf) -> ParsedStatement:
    raw_rows: List[tuple[int, List[str]]] = []
    rejected: List[RejectedRow] = []

    for page_no, page in enumerate(pdf.pages, start=1):
        for r in _page_rows(page):
            first = clean(r[_ID])
            if not first or first.lower() == "id":
                continue
            if not _TXID_RE.match(first):
                if sum(1 for c in r if c) > 1:
                    rejected.append(RejectedRow(page_no, "no transaction id in first column",
                                                " | ".join(r)))
                continue
            raw_rows.append((page_no, r))

    if not raw_rows:
        raise StatementParseError(
            "No transaction rows found. The file may be a scanned image, "
            "password-protected, or not an MTN MoMo portal export.")

    account = _infer_account([r for _, r in raw_rows])
    holder_names: collections.Counter = collections.Counter()

    txns: List[Txn] = []
    for page_no, r in raw_rows:
        try:
            ts = _to_iso(r[_DATE])
        except StatementParseError:
            rejected.append(RejectedRow(page_no, f"unparseable timestamp {r[_DATE]!r}",
                                        " | ".join(r)))
            continue
        signed = to_int(r[_AMT])
        if signed is None:
            rejected.append(RejectedRow(page_no, f"unparseable amount {r[_AMT]!r}",
                                        " | ".join(r)))
            continue

        frm, to = _party_id(r[_FROM]), _party_id(r[_TO])
        from_name, to_name = dedupe_name(r[_FROM_NAME]), dedupe_name(r[_TO_NAME])
        if account not in (frm, to):
            rejected.append(RejectedRow(
                page_no, "neither party is the account holder - transaction not attributable",
                " | ".join(r)))
        direction = "out" if signed < 0 else "in"
        holder_names[from_name if direction == "out" else to_name] += 1

        status = r[_STATUS].upper()
        txns.append(Txn(
            txn_id=r[_ID],
            status="SUCCESSFUL" if status in _OK_STATUSES else "FAILED",
            txn_type=re.sub(r"\s+", "_", r[_TYPE].strip().upper()),
            timestamp=ts,
            from_msisdn=frm, from_name=from_name,
            to_msisdn=to, to_name=to_name,
            amount=abs(signed),
            fee=to_int(r[_FEE]),
            balance_after=to_int(r[_BALANCE]),
            page=page_no,
            direction=direction,
        ))

    stamps = sorted(t.timestamp for t in txns)
    holder_names.pop("", None)
    header = StatementHeader(
        customer=holder_names.most_common(1)[0][0] if holder_names else None,
        msisdn=account,
        contact_phone=account,
        period_start=stamps[0] if stamps else None,
        period_end=stamps[-1] if stamps else None,
        provider="MTN Rwanda MoMo",
    )

    return ParsedStatement(
        header=header, transactions=txns, pages=len(pdf.pages), source_format=NAME,
        capabilities=Capabilities(
            counterparty_id=True, counterparty_name=True, txn_type=True,
            balance=True, status=True, cp_id_basis="msisdn",
            reconciliation=None),
        rejected_rows=rejected,
    )
