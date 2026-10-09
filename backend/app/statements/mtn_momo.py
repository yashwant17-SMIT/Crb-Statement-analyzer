"""
MTN Rwanda MoMo statement, app-generated PDF (the original format).

  Transaction Id | Status | Transaction Type | Date | From | To |
  Amount (RWF) | Fee (RWF) | Balance After (RWF)

Quirks handled below:
  * a transaction can wrap across two table rows, the continuation carrying
    the tail of the From/To names with an empty id;
  * the column header repeats on every page;
  * the Date column is narrow in some issues of the format, so the timestamp
    wraps inside its cell ("2026-\n10-01\n23:22:50"), and at a page break part
    of it can land on the continuation row ("2026-" then "10-01 23:22:50").
"""
from __future__ import annotations

import re
from typing import List

from .base import (
    Capabilities, ParsedStatement, RejectedRow, StatementHeader,
    StatementParseError, Txn, clean, dedupe_name, split_party, to_int, to_iso,
)

NAME = "mtn_momo_v1"
PROVIDER = "mtn_momo"
LABEL = "MTN MoMo - app statement"

TXID_RE = re.compile(r"^\d{6,}$")
# A timestamp with every line break and space removed: "2026-10-0123:22:50".
_SQUASHED_TS = re.compile(r"(\d{4}-\d{2}-\d{2})(\d{2}:\d{2}(?::\d{2})?)")
HEADER_FIRST_CELL = "transaction\nid"

_HEADER_FIELDS = {
    "customer": re.compile(r"Customer:\s*(.+)"),
    "msisdn": re.compile(r"Number:\s*(\S+)"),
    "email": re.compile(r"Email:\s*(\S+)"),
    "period": re.compile(r"Period:\s*(.+?)\s+to\s+(.+)"),
    "generated": re.compile(r"Generated:\s*(.+)"),
    "declared_count": re.compile(r"Total Transactions:\s*([\d,]+)"),
}


def sniff(first_page_text: str) -> float:
    # PDF text extraction can preserve visual spacing, e.g. "Momo   Statement".
    # Normalize whitespace before matching so the adapter fingerprints the
    # document semantics rather than one renderer's spacing.
    t = re.sub(r"\s+", " ", (first_page_text or "").lower()).strip()
    if "momo statement" in t and "total transactions:" in t:
        return 0.99
    if "momo statement" in t:
        return 0.7
    return 0.0


def _parse_header(text: str) -> StatementHeader:
    h = StatementHeader(provider="MTN Rwanda MoMo")
    if m := _HEADER_FIELDS["customer"].search(text):
        h.customer = m.group(1).strip()
    if m := _HEADER_FIELDS["msisdn"].search(text):
        h.msisdn = m.group(1).strip()
        h.contact_phone = h.msisdn
    if m := _HEADER_FIELDS["email"].search(text):
        h.email = m.group(1).strip()
    if m := _HEADER_FIELDS["period"].search(text):
        h.period_start, h.period_end = m.group(1).strip(), m.group(2).strip()
    if m := _HEADER_FIELDS["generated"].search(text):
        h.generated_at = m.group(1).strip()
    if m := _HEADER_FIELDS["declared_count"].search(text):
        h.declared_txn_count = int(m.group(1).replace(",", ""))
    return h


def _timestamp(text: str) -> str:
    """Rebuild a timestamp the PDF has broken across lines, then parse it."""
    squashed = re.sub(r"\s+", "", text or "")
    if m := _SQUASHED_TS.fullmatch(squashed):
        return to_iso(f"{m.group(1)} {m.group(2)}")
    return to_iso(" ".join((text or "").split()))


def parse(pdf) -> ParsedStatement:
    rows: List[Txn] = []
    rejected: List[RejectedRow] = []
    header = StatementHeader(provider="MTN Rwanda MoMo")
    pages = len(pdf.pages)

    # Rows are collected first and turned into transactions at the end,
    # because a continuation row can still be adding to the previous row's
    # names and timestamp - across a page break included.
    pending: List[dict] = []

    for page_no, page in enumerate(pdf.pages, start=1):
        if page_no == 1:
            header = _parse_header(page.extract_text() or "")

        for table in page.extract_tables():
            for raw in table:
                if len(raw) < 9:
                    continue
                first = clean(raw[0])
                if first.lower() == HEADER_FIRST_CELL:
                    continue

                if TXID_RE.match(first):
                    pending.append({"raw": raw, "date": raw[3] or "", "page": page_no,
                                    "from": clean(raw[4]), "to": clean(raw[5])})
                elif pending:
                    last = pending[-1]
                    if clean(raw[3]):
                        last["date"] += "\n" + clean(raw[3])
                    tail_from, tail_to = clean(raw[4]), clean(raw[5])
                    if tail_from:
                        last["from_tail"] = f"{last.get('from_tail', '')} {tail_from}".strip()
                    if tail_to:
                        last["to_tail"] = f"{last.get('to_tail', '')} {tail_to}".strip()

    for rec in pending:
        raw, page_no = rec["raw"], rec["page"]
        try:
            ts = _timestamp(rec["date"])
        except StatementParseError:
            rejected.append(RejectedRow(
                page_no, f"unparseable timestamp {clean(rec['date'])!r}",
                " | ".join(clean(c) for c in raw)))
            continue
        amount = to_int(clean(raw[6]))
        if amount is None:
            rejected.append(RejectedRow(
                page_no, f"unparseable amount {clean(raw[6])!r}",
                " | ".join(clean(c) for c in raw)))
            continue
        f_id, f_name = split_party(rec["from"])
        t_id, t_name = split_party(rec["to"])
        if rec.get("from_tail"):
            f_name = dedupe_name(f"{f_name} {rec['from_tail']}".strip())
        if rec.get("to_tail"):
            t_name = dedupe_name(f"{t_name} {rec['to_tail']}".strip())
        rows.append(Txn(
            txn_id=clean(raw[0]),
            status=clean(raw[1]).upper(),
            txn_type=clean(raw[2]).upper(),
            timestamp=ts,
            from_msisdn=f_id, from_name=f_name,
            to_msisdn=t_id, to_name=t_name,
            amount=amount,
            fee=to_int(clean(raw[7])),
            balance_after=to_int(clean(raw[8])),
            page=page_no,
            direction=None,   # derived by comparing parties to the account
        ))

    if not rows:
        raise StatementParseError(
            "No transaction rows found. The file may be a scanned image, "
            "password-protected, or not an MTN MoMo app statement.")

    return ParsedStatement(
        header=header, transactions=rows, pages=pages, source_format=NAME,
        capabilities=Capabilities(
            counterparty_id=True, counterparty_name=True, txn_type=True,
            balance=True, status=True, cp_id_basis="msisdn",
            reconciliation="row_count"),
        rejected_rows=rejected,
    )
