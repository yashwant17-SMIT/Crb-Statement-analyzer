"""
Bank of Kigali account statement, branch-printed (core-banking / Apache FOP).

  Book Date | Reference | Description | Narration | Value Date | Debit | Credit | Closing Balance

The statement a branch prints for a customer, as opposed to the internet
banking export handled by bk_statement.py. Same bank, same ledger, different
document:

  * dates print as '01 MAY 26', not ISO;
  * the transaction kind ('MTN Push to Bank', 'BK to BK NEW MOBISERVE') is
    its own Description column, and the Narration column carries the other
    party ('3Jane DOE', 'MTNPUSH1234567890125078800000');
  * every cell is hard-wrapped at a fixed width, mid-word ('MTN Push to Ba' /
    'nk'), so wrapped lines are joined with no space;
  * the bank's name is only in the logo, so the format is recognised by its
    layout and its Rwandan IBAN with BK's bank code (040).

The opening balance is the 'Balance at Period Start' row, the closing balance
the 'Balance at Period End' row, and the period totals are printed on the last
page - so the balance identity can be checked exactly as for the other export.
"""
from __future__ import annotations

import re
from typing import List, Optional

from .base import (
    Capabilities,
    ParsedStatement,
    RejectedRow,
    StatementHeader,
    StatementParseError,
    Txn,
    to_int,
    to_iso,
)
from .bk_statement import _counterparty, _txn_type

NAME = "bk_branch_statement_v1"
PROVIDER = "bank_of_kigali"
LABEL = "Bank of Kigali - branch-printed statement"

_COLS = 8
_BOOK, _REF, _DESC, _NARRATION, _VALUE, _DEBIT, _CREDIT, _BALANCE = range(8)

_DATE_RE = re.compile(r"^\d{2} [A-Z]{3} \d{2}$")
_PERIOD_RE = re.compile(r"for the period\s+(\d{8})\s+To\s+(\d{8})", re.I)
_PRINTED_RE = re.compile(r"^\s*(\d{1,2} [A-Za-z]+ \d{4})\s*\n\s*(\d{2}:\d{2}:\d{2})", re.M)


def _unwrap(cell: Optional[str]) -> str:
    """Join a hard-wrapped cell. The wrap falls mid-word, so no space is added."""
    return "".join((cell or "").split("\n")).strip()


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def sniff(first_page_text: str) -> float:
    t = _flat(first_page_text)
    layout = ("account statement for the period" in t and "book date" in t
              and "narration" in t and "closing balance" in t)
    if not layout:
        return 0.0
    # RW + 2 check digits + 040 is Bank of Kigali's bank code.
    if re.search(r"iban\s*:\s*rw\d{2}040", t) or "bk to bk" in t or "bk yacu" in t:
        return 0.95
    return 0.6


def _field(text: str, label: str, stop: str = r"\s{2,}|\n| [A-Z][a-z]+ ?:") -> Optional[str]:
    m = re.search(rf"{label}\s*:\s*(.*?)(?:{stop}|$)", text)
    value = m.group(1).strip() if m else ""
    return value or None


def _parse_header(text: str) -> StatementHeader:
    h = StatementHeader(provider="Bank of Kigali")
    h.msisdn = _field(text, "Account Number")
    h.customer = _field(text, "Account Name")
    h.contact_phone = _field(text, "Phone")
    h.email = _field(text, "Email")
    h.account_type = _field(text, "Account Type", stop=r"\n")
    if m := _PERIOD_RE.search(text):
        h.period_start = to_iso(f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}")
        h.period_end = to_iso(f"{m.group(2)[:4]}-{m.group(2)[4:6]}-{m.group(2)[6:]}")
    if m := _PRINTED_RE.search(text):
        try:
            h.generated_at = to_iso(f"{m.group(1)}")[:10] + "T" + m.group(2)
        except StatementParseError:
            h.generated_at = None
    return h


_HEADINGS = ("Book", "Reference", "Description", "Narration", "Value", "Debit", "Credit", "Closing")
_TEXT_COLS = ("ref", "desc", "narr")


def _columns(page) -> Optional[dict]:
    """Column edges from the heading row, or None on a page that has none."""
    words = page.extract_words()
    for w in words:
        if w["text"] != "Book":
            continue
        row = {x["text"]: x for x in words if abs(x["top"] - w["top"]) < 2}
        if not all(h in row for h in _HEADINGS):
            continue
        x = {h: row[h]["x0"] for h in _HEADINGS}
        return {
            "top": w["top"],
            "ref": x["Reference"], "desc": x["Description"], "narr": x["Narration"],
            "value": x["Value"], "debit": x["Debit"], "credit": x["Credit"], "balance": x["Closing"],
        }
    return None


def _lines(page, top_after: float):
    """Words grouped into visual lines, top to bottom."""
    lines: List[List[dict]] = []
    for w in sorted(page.extract_words(), key=lambda w: (round(w["top"]), w["x0"])):
        if w["top"] <= top_after:
            continue
        if lines and abs(lines[-1][0]["top"] - w["top"]) < 2:
            lines[-1].append(w)
        else:
            lines.append([w])
    return lines


def _split(line: List[dict], col: dict) -> dict:
    """One visual line -> {column: [words]}. Amounts are right-aligned, so they
    are placed by their right edge; text columns by their left edge."""
    out = {k: [] for k in ("book", "ref", "desc", "narr", "value", "debit", "credit", "balance")}
    for w in line:
        if re.fullmatch(r"[\d,().-]+", w["text"]) and w["x0"] >= col["value"] + 40:
            edge = w["x1"]
            key = ("debit" if edge < col["credit"] - 4 else
                   "credit" if edge < col["balance"] - 2 else "balance")
        else:
            x = w["x0"]
            key = ("book" if x < col["ref"] - 2 else "ref" if x < col["desc"] - 2 else
                   "desc" if x < col["narr"] - 2 else "narr" if x < col["value"] - 2 else
                   "value" if x < col["debit"] - 2 else "debit" if x < col["credit"] - 2 else
                   "credit" if x < col["balance"] - 2 else "balance")
        out[key].append(w)
    return out


def _join(parts: List[tuple], right_edge: float) -> str:
    """Join a cell's wrapped lines. A line that ran to the column edge was cut
    mid-word, so the next line is glued on; a short line ended a word."""
    text = ""
    for i, (line, x1) in enumerate(parts):
        text += line
        if i + 1 < len(parts) and x1 < right_edge - 14:
            text += " "
    return text.strip()


def parse(pdf) -> ParsedStatement:
    pages = len(pdf.pages)
    header = _parse_header(pdf.pages[0].extract_text() or "") if pages else StatementHeader()
    txns: List[Txn] = []
    rejected: List[RejectedRow] = []

    col = None
    marker_balance: Optional[int] = None
    cur: Optional[dict] = None      # the row being assembled; it may run onto the next page

    def flush():
        nonlocal cur
        if cur is None:
            return
        row, cur = cur, None
        edges = {"ref": col["desc"], "desc": col["narr"], "narr": col["value"]}
        ref, desc, narr = (_join(row[k], edges[k] - 6) for k in _TEXT_COLS)
        debit, credit = to_int(row["debit"]), to_int(row["credit"])
        raw = f"{row['book']} | {ref} | {desc} | {narr} | {row['debit']} | {row['credit']} | {row['balance']}"
        if debit is None and credit is None:
            rejected.append(RejectedRow(row["page"], "row has neither a debit nor a credit", raw))
            return
        if debit is not None and credit is not None:
            rejected.append(RejectedRow(row["page"], "row carries both a debit and a credit", raw))
            return
        direction = "in" if credit is not None else "out"
        # 'Description : Narration' is the shape bk_statement's counterparty
        # reader expects: kind before the colon, the other party after it.
        cp_id, cp_name = _counterparty(f"{desc} : {narr}")
        account, account_name = header.msisdn or "", header.customer or ""
        txns.append(Txn(
            txn_id=ref or f"p{row['page']}-{len(txns)}",
            status="SUCCESSFUL",     # the statement lists settled entries only
            txn_type=_txn_type(desc),
            timestamp=to_iso(row["book"].title()),
            from_msisdn=cp_id if direction == "in" else account,
            from_name=cp_name if direction == "in" else account_name,
            to_msisdn=account if direction == "in" else cp_id,
            to_name=account_name if direction == "in" else cp_name,
            amount=credit if credit is not None else debit,
            fee=None,                # charges are their own rows here
            balance_after=to_int(row["balance"]),
            page=row["page"],
            direction=direction,
        ))

    for page_no, page in enumerate(pdf.pages, start=1):
        found = _columns(page)
        if found:
            col = found
        if col is None:
            continue
        # Above the table: the print date and time in the top corner, and on
        # page 1 the account block and the column headings.
        top = found["top"] + 2 if found else 90
        for line in _lines(page, top):
            text = " ".join(w["text"] for w in line)
            if re.fullmatch(r"Page \d+ of \d+", text):
                continue
            c = _split(line, col)
            book = " ".join(w["text"] for w in c["book"])
            refs = " ".join(w["text"] for w in c["ref"])

            # 'Balance at Perio' / 'd Start' (or 'd End'): the opening or closing
            # balance, wrapped over two lines like every other cell.
            if refs.startswith("Balance at Perio"):
                flush()
                marker_balance = to_int(" ".join(w["text"] for w in c["balance"]))
                continue
            if refs in ("d Start", "d End"):
                if refs == "d Start":
                    header.opening_balance = marker_balance
                else:
                    header.closing_balance = marker_balance
                continue
            if text.startswith(("Total Credits", "Total Debits")):
                flush()
                continue

            if _DATE_RE.match(book):
                flush()
                cur = {"page": page_no, "book": book, "ref": [], "desc": [], "narr": [],
                       "debit": "", "credit": "", "balance": ""}
            elif cur is None:
                if any(c[k] for k in ("debit", "credit")):
                    rejected.append(RejectedRow(page_no, "amount outside any dated row", text))
                continue
            elif any(c[k] for k in ("debit", "credit")):
                # A second set of amounts under one date is a row we would
                # otherwise merge into its neighbour.
                rejected.append(RejectedRow(page_no, "amount on a continuation line", text))
                continue

            for k in _TEXT_COLS:
                if c[k]:
                    cur[k].append((" ".join(w["text"] for w in c[k]), max(w["x1"] for w in c[k])))
            for k in ("debit", "credit", "balance"):
                if c[k]:
                    cur[k] = " ".join(w["text"] for w in c[k])
    flush()

    last = (pdf.pages[-1].extract_text() or "") if pages else ""
    if m := re.search(r"Total Credits\s*:\s*([\d,]+)", last):
        header.total_credits = to_int(m.group(1))
    if m := re.search(r"Total Debits\s*:\s*([\d,]+)", last):
        header.total_debits = to_int(m.group(1))

    if not txns:
        raise StatementParseError(
            "No transaction rows found. The file may be a scanned image, "
            "password-protected, or not a Bank of Kigali statement."
        )
    if not header.msisdn:
        raise StatementParseError("Statement header carries no account number.")

    return ParsedStatement(
        header=header,
        transactions=txns,
        pages=pages,
        source_format=NAME,
        capabilities=Capabilities(
            counterparty_id=False,
            counterparty_name=True,
            txn_type=False,          # derived from the description text, not stated
            balance=True,
            status=False,
            cp_id_basis="derived_name",
            reconciliation="balance_identity",
            timestamp_precision="day",
        ),
        rejected_rows=rejected,
    )
