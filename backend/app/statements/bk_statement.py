"""
Bank of Kigali account statement (JasperReports export).

  Book Date | Value Date | Reference | Narration | Debit | Credit | Balance

Structurally the opposite of a MoMo statement. Direction is explicit - a row
is a debit or a credit, never ambiguous - but there is no counterparty
identifier at all. The other party, where it exists, is embedded in a
free-text narration:

  'MTN Push to Bank | MTNPUSH25562246402250788972318'
  'Cash deposit-BK YACU : UWASE VANESSA | SCHOOL FEES\\n0788802755UWASE VANESS'
  'EKASH incoming transfer : Monique NDACYAYI | Monique NDACYAYISENGA250795862821'

So counterparty keys here are derived, not given, and the engine is told so
via Capabilities.cp_id_basis. Where a phone number is recoverable from the
narration we use it; otherwise the key is a slug of the narration's name part,
which collides across people with the same display name.

In exchange the format offers a stronger completeness check than a row count:
opening + credits - debits must equal the printed closing balance.
"""
from __future__ import annotations

import html
import re
from typing import List, Optional

from .base import (
    Capabilities,
    ParsedStatement,
    RejectedRow,
    StatementHeader,
    StatementParseError,
    Txn,
    clean,
    dedupe_name,
    find_msisdn,
    slugify,
    to_int,
    to_iso,
)

NAME = "bk_statement_v1"
PROVIDER = "bank_of_kigali"
LABEL = "Bank of Kigali - account statement"

_COLS = 7
_BOOK, _VALUE, _REF, _NARRATION, _DEBIT, _CREDIT, _BALANCE = range(7)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The header is a three-column block whose inter-column gap collapses to a
# single space in layout extraction, so it cannot be split on whitespace. Each
# value is instead taken as the text between its own label and the next label.
_LABELS = (
    "Customer Name", "Account No", "Issued on", "Phone Number", "Account Name",
    "Total Debits", "Address", "Account Type", "Total Credits", "Email",
    "IBAN", "Open Balance", "Period", "Currency", "Closing Balance",
)
_LABEL_RE = re.compile(r"\b(" + "|".join(re.escape(l) for l in _LABELS) + r")\s*:\s*")
_PERIOD_RE = re.compile(r"(\S+)\s+To\s+(\S+)")


def _header_fields(text: str) -> dict:
    """Slice the header block into {label: value} between successive labels."""
    flat = html.unescape(re.sub(r"[ \t]+", " ", text or ""))
    marks = list(_LABEL_RE.finditer(flat))
    out = {}
    for i, m in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(flat)
        # A value never runs past its own line - the last label on the page
        # would otherwise swallow the whole transaction table.
        value = flat[m.end():stop].split("\n")[0].strip().strip("|").strip()
        if value and value.lower() not in ("null", "none"):
            out.setdefault(m.group(1), value)
    return out


# Narration prefixes, normalised into a pseudo transaction type so downstream
# rules have something categorical to work with. Order matters - first hit wins.
_TYPE_MAP = [
    ("mtn push to bank", "MTN_PUSH_IN"),
    ("mtn pull from bank", "MTN_PULL_OUT"),
    ("new app momo outward", "MOMO_OUT"),
    ("bk to bk", "BK_INTERNAL"),
    ("new app bk-bk", "BK_INTERNAL"),
    ("cash deposit", "CASH_IN"),
    ("cash withdraw", "CASH_OUT"),
    ("ekash incoming", "EKASH_IN"),
    ("ekash p2p", "EKASH_OUT"),
    ("ekash charges", "CHARGE"),
    ("bill pyt charge", "CHARGE"),
    ("charge", "CHARGE"),
    ("bill payment", "BILL_PAYMENT"),
    ("bill pyt", "BILL_PAYMENT"),
    ("transfer in", "TRANSFER_IN"),
    ("agent commission", "AGENT_COMMISSION"),
]


def sniff(first_page_text: str) -> float:
    # Layout-mode extraction pads inside words ('Bank  of Kigali'), so collapse
    # runs of whitespace before matching.
    t = re.sub(r"\s+", " ", (first_page_text or "").lower())
    if "bank of kigali account statement" in t:
        return 0.99
    if "bank of kigali" in t and "narration" in t:
        return 0.8
    return 0.0


def _txn_type(narration: str) -> str:
    low = (narration or "").lower()
    for needle, label in _TYPE_MAP:
        if needle in low:
            return label
    return "OTHER"


def _counterparty(narration: str) -> tuple[str, str]:
    """Best-effort (id, name) out of a narration string.

    The id is a phone number where the narration carries one, otherwise a slug
    of the name - weaker, and declared as such in Capabilities.
    """
    text = (narration or "").replace("\n", " ")
    head = text.split("|")[0]
    name = head.split(":", 1)[1].strip() if ":" in head else head.strip()
    name = dedupe_name(re.sub(r"\s+", " ", name))
    phone = find_msisdn(text)
    if phone:
        return phone, name
    return (f"name:{slugify(name)}" if name else ""), name


def _parse_header(text: str) -> StatementHeader:
    f = _header_fields(text)
    h = StatementHeader(provider="Bank of Kigali")
    h.customer = f.get("Customer Name")
    h.msisdn = f.get("Account No")
    h.contact_phone = f.get("Phone Number")
    h.email = f.get("Email")
    h.account_type = f.get("Account Type")
    h.generated_at = f.get("Issued on")
    if period := f.get("Period"):
        if m := _PERIOD_RE.search(period):
            h.period_start, h.period_end = m.group(1), m.group(2)
    h.total_debits = to_int(f.get("Total Debits", ""))
    h.total_credits = to_int(f.get("Total Credits", ""))
    h.opening_balance = to_int(f.get("Open Balance", ""))
    h.closing_balance = to_int(f.get("Closing Balance", ""))
    return h


def parse(pdf) -> ParsedStatement:
    header = StatementHeader(provider="Bank of Kigali")
    txns: List[Txn] = []
    rejected: List[RejectedRow] = []
    pages = len(pdf.pages)

    for page_no, page in enumerate(pdf.pages, start=1):
        if page_no == 1:
            header = _parse_header(page.extract_text(layout=True) or "")

        for table in page.extract_tables():
            for raw in table:
                if not raw or len(raw) < _COLS:
                    continue
                cells = [clean(c) for c in raw]
                book = cells[_BOOK].split("\n")[0].strip()

                if not _DATE_RE.match(book):
                    # Column headings and the opening-balance line carry no date
                    # and no amounts; anything else is a row we are losing.
                    if book.lower().startswith("book date"):
                        continue
                    if cells[_DEBIT] or cells[_CREDIT]:
                        rejected.append(RejectedRow(page_no, "row carries an amount but no book date",
                                                    " | ".join(cells)))
                    continue

                debit = to_int(cells[_DEBIT])
                credit = to_int(cells[_CREDIT])
                if debit is None and credit is None:
                    rejected.append(RejectedRow(page_no, "row has neither a debit nor a credit",
                                                " | ".join(cells)))
                    continue
                if debit is not None and credit is not None:
                    rejected.append(RejectedRow(page_no, "row carries both a debit and a credit",
                                                " | ".join(cells)))
                    continue

                direction = "in" if credit is not None else "out"
                amount = credit if credit is not None else debit
                narration = html.unescape(re.sub(r"\s+", " ", cells[_NARRATION].replace("\n", " ")).strip())
                cp_id, cp_name = _counterparty(narration)
                account = header.msisdn or ""
                account_name = header.customer or ""

                txns.append(
                    Txn(
                        txn_id=cells[_REF].replace("\n", "") or f"p{page_no}-{len(txns)}",
                        status="SUCCESSFUL",     # the statement lists settled entries only
                        txn_type=_txn_type(narration),
                        timestamp=to_iso(book),
                        from_msisdn=cp_id if direction == "in" else account,
                        from_name=cp_name if direction == "in" else account_name,
                        to_msisdn=account if direction == "in" else cp_id,
                        to_name=account_name if direction == "in" else cp_name,
                        amount=amount,
                        fee=None,                # charges are their own rows here
                        balance_after=to_int(cells[_BALANCE]),
                        page=page_no,
                        direction=direction,
                    )
                )

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
            txn_type=False,          # derived from narration text, not stated
            balance=True,
            status=False,
            cp_id_basis="derived_name",
            reconciliation="balance_identity",
            timestamp_precision="day",
        ),
        rejected_rows=rejected,
    )
