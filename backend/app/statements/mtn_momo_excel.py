"""
MTN Rwanda MoMo statement, Excel export.

A different report from the app-generated statement in mtn_momo.py:

  TransactionID | DATE | FromMSISDN | ToMSISDN | FromUsername | ToUsername |
  TransactionType | Amount | From Fee | To Fee | TransactionStatus

Three consequences the engine has to be told about:

  * There is no header block. No customer name, no declared transaction count,
    no stated period. The account is inferred as the identifier present on
    (almost) every row, and the period from the rows themselves.
  * Display names are effectively absent - on the sample, 18 of 2,741 rows.
    Self-transfer therefore cannot be name-matched. It does not need to be:
    both legs carry an MSISDN, so `from == to == account` is an exact test,
    which is stronger than the name match it replaces.
  * There is no running balance, so liquidity checks cannot run.
"""
from __future__ import annotations

import collections
import re
from typing import Any, Dict, List, Optional

from .base import (
    Capabilities,
    ParsedStatement,
    RejectedRow,
    StatementHeader,
    StatementParseError,
    Txn,
    clean,
    to_int,
    to_iso,
)

NAME = "mtn_momo_excel_v1"
PROVIDER = "mtn_momo"
LABEL = "MTN MoMo - spreadsheet export"

_TXID_RE = re.compile(r"^\d{6,}$")
_HEADER_CELL = "transactionid"
_COLS = 11

# Column indices.
_ID, _DATE, _FROM, _TO, _FROM_NAME, _TO_NAME, _TYPE, _AMT, _FROM_FEE, _TO_FEE, _STATUS = range(11)

# The export's own vocabulary for a completed transaction.
_OK_STATUSES = {"COMMITTED", "COMPLETED", "SUCCESS", "SUCCESSFUL"}


def sniff(first_page_text: str) -> float:
    t = (first_page_text or "").lower()
    if "frommsisdn" in t and "tomsisdn" in t:
        return 0.98 if "momo" in t else 0.85
    return 0.0


def _infer_account(rows: List[List[str]]) -> str:
    """The identifier on nearly every row is the account the statement is for."""
    counts: collections.Counter = collections.Counter()
    for r in rows:
        counts[clean(r[_FROM])] += 1
        counts[clean(r[_TO])] += 1
    counts.pop("", None)
    if not counts:
        raise StatementParseError("No transaction identifiers found in the statement.")
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
    pages = len(pdf.pages)

    for page_no, page in enumerate(pdf.pages, start=1):
        for table in page.extract_tables():
            for raw in table:
                if not raw or len(raw) < _COLS:
                    continue
                first = clean(raw[_ID])
                if not first or first.lower().replace(" ", "") == _HEADER_CELL:
                    continue
                if not _TXID_RE.match(first):
                    # Title banners ('MTN RWANDA MOMO STATEMENT') have a single
                    # populated cell; anything else with content is a real loss.
                    if sum(1 for c in raw if clean(c)) > 1:
                        rejected.append(
                            RejectedRow(page_no, "no transaction id in first column",
                                        " | ".join(clean(c) for c in raw))
                        )
                    continue
                raw_rows.append((page_no, [clean(c) for c in raw]))

    if not raw_rows:
        raise StatementParseError(
            "No transaction rows found. The file may be a scanned image, "
            "password-protected, or not an MTN MoMo spreadsheet export."
        )

    account = _infer_account([r for _, r in raw_rows])

    txns: List[Txn] = []
    for page_no, r in raw_rows:
        try:
            ts = to_iso(r[_DATE])
        except StatementParseError:
            rejected.append(RejectedRow(page_no, f"unparseable timestamp {r[_DATE]!r}",
                                        " | ".join(r)))
            continue
        amount = to_int(r[_AMT])
        if amount is None:
            rejected.append(RejectedRow(page_no, f"unparseable amount {r[_AMT]!r}",
                                        " | ".join(r)))
            continue

        frm, to = r[_FROM], r[_TO]
        # A row where the account is on both legs is a wallet self-movement. It
        # is reported as an inflow so it lands in gross and is then deducted as
        # a self-transfer, matching how the app-generated format behaves.
        if to == account:
            direction = "in"
        elif frm == account:
            direction = "out"
        else:
            direction = None
            rejected.append(RejectedRow(
                page_no,
                "neither party is the account holder - transaction not attributable",
                " | ".join(r),
            ))

        status = r[_STATUS].upper()
        fee = to_int(r[_TO_FEE] if direction == "in" else r[_FROM_FEE])

        txns.append(
            Txn(
                txn_id=r[_ID],
                status="SUCCESSFUL" if status in _OK_STATUSES else "FAILED",
                txn_type=r[_TYPE].upper(),
                timestamp=ts,
                from_msisdn=frm,
                from_name=r[_FROM_NAME],
                to_msisdn=to,
                to_name=r[_TO_NAME],
                amount=amount,
                fee=fee,
                balance_after=None,
                page=page_no,
                direction=direction,
            )
        )

    stamps = sorted(t.timestamp for t in txns)
    header = StatementHeader(
        customer=None,
        msisdn=account,
        contact_phone=account,
        period_start=stamps[0] if stamps else None,
        period_end=stamps[-1] if stamps else None,
        declared_txn_count=None,
        provider="MTN Rwanda MoMo",
    )

    return ParsedStatement(
        header=header,
        transactions=txns,
        pages=pages,
        source_format=NAME,
        capabilities=Capabilities(
            counterparty_id=True,
            counterparty_name=False,
            txn_type=True,
            balance=False,
            status=True,
            cp_id_basis="msisdn",
            reconciliation=None,
        ),
        rejected_rows=rejected,
    )
