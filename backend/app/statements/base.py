"""
Shared types for every statement adapter.

An adapter's only job is to turn one provider's PDF into a ParsedStatement.
Nothing here interprets a transaction - all judgement lives in analyzer.py,
which never learns which format produced the rows it is given.
"""
from __future__ import annotations

import io
import itertools
import logging
import multiprocessing
import os
import re
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any, Dict, Iterator, List, Literal, Optional, Tuple

log = logging.getLogger(__name__)

Direction = Literal["in", "out"]


class StatementParseError(ValueError):
    """Raised when a file is not a readable statement of the expected format."""


@dataclass
class Txn:
    txn_id: str
    status: str                      # normalised to SUCCESSFUL / FAILED
    txn_type: str
    timestamp: str                   # ISO-8601
    from_msisdn: str
    from_name: str
    to_msisdn: str
    to_name: str
    amount: int
    fee: Optional[int]
    balance_after: Optional[int]
    page: int
    # Set by adapters that know direction outright (debit/credit columns).
    # Adapters that don't leave it None and _frame falls back to comparing
    # the party identifiers against the account.
    direction: Optional[Direction] = None

    def dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RejectedRow:
    """A row the adapter saw, could not use, and refuses to drop silently."""
    page: int
    reason: str
    raw: str

    def dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Capabilities:
    """What this format can actually supply.

    The engine consults this to say "not assessed, because this format carries
    no balance column" instead of emitting a clean-looking report with a red
    flag that could never have fired.
    """
    counterparty_id: bool = True
    counterparty_name: bool = True
    txn_type: bool = True
    balance: bool = True
    status: bool = True
    # How a counterparty key is derived: strongest first.
    cp_id_basis: Literal["msisdn", "account_no", "derived_name", "none"] = "msisdn"
    # What the file lets us reconcile against.
    reconciliation: Optional[Literal["row_count", "balance_identity"]] = None
    # Bank statements are booked by date, not by clock time. Rules with a
    # sub-day window (rapid round-trips) cannot run at day precision.
    timestamp_precision: Literal["second", "day"] = "second"

    def dict(self) -> Dict[str, Any]:
        return asdict(self)

    def unavailable(self) -> List[str]:
        out = []
        if not self.counterparty_name:
            out.append("counterparty_name")
        if not self.txn_type:
            out.append("transaction_type")
        if not self.balance:
            out.append("running_balance")
        if not self.status:
            out.append("transaction_status")
        if self.timestamp_precision == "day":
            out.append("intraday_timing")
        return out


@dataclass
class StatementHeader:
    customer: Optional[str] = None
    # The identifier transactions are keyed on: an MSISDN for MoMo, an account
    # number for a bank. analyzer._frame compares party fields against this.
    msisdn: Optional[str] = None
    # The holder's contact number where the statement prints one. Kept apart
    # from `msisdn` because bureau identity linkage needs a phone, and on a
    # bank statement `msisdn` is an account number.
    contact_phone: Optional[str] = None
    email: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    generated_at: Optional[str] = None
    declared_txn_count: Optional[int] = None
    # Bank statements print these; they give a stronger reconciliation than a
    # row count because they check money rather than cardinality.
    opening_balance: Optional[int] = None
    closing_balance: Optional[int] = None
    total_credits: Optional[int] = None
    total_debits: Optional[int] = None
    account_type: Optional[str] = None
    provider: Optional[str] = None

    def dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParsedStatement:
    header: StatementHeader
    transactions: List[Txn]
    pages: int
    source_format: str = "mtn_momo_v1"
    capabilities: Capabilities = field(default_factory=Capabilities)
    rejected_rows: List[RejectedRow] = field(default_factory=list)

    @property
    def parse_complete(self) -> bool:
        """True when we recovered exactly the row count the statement claims."""
        return (
            self.header.declared_txn_count is None
            or self.header.declared_txn_count == len(self.transactions)
        )

    @property
    def balance_reconciles(self) -> Optional[bool]:
        """opening + credits - debits == closing, where the file states them.

        None when the statement does not print the totals. A False here is a
        hard stop: it means at least one amount was misread.
        """
        h = self.header
        if None in (h.opening_balance, h.closing_balance, h.total_credits, h.total_debits):
            return None
        return h.opening_balance + h.total_credits - h.total_debits == h.closing_balance

    def reconciliation_note(self) -> str:
        if self.capabilities.reconciliation == "balance_identity":
            ok = self.balance_reconciles
            if ok is None:
                return "Statement prints no period totals; balance identity not checked."
            return (
                "Opening + credits - debits equals the printed closing balance."
                if ok
                else "Opening + credits - debits does NOT equal the printed closing balance."
            )
        if self.header.declared_txn_count is None:
            return "Statement declares no transaction count; row completeness not checked."
        return (
            f"Recovered {len(self.transactions)} of "
            f"{self.header.declared_txn_count} declared transactions."
        )


# ------------------------------------------------------------- pages -------
# pdfplumber keeps every page it has read, with all its characters and lines,
# until the PDF is closed: a 1,079-page MoMo statement held 7.7 GB. Adapters
# walk pages through these helpers, which let each page go once it is read.

# Reading a page's tables is pdfminer working through its drawing
# instructions, about 0.2 s a page, so a long statement is shared out across
# processes. Below this many pages the start-up isn't worth it.
PARALLEL_FROM_PAGES = 40
_CHUNK_PAGES = 25
_MAX_WORKERS = 8


def iter_pages(pdf) -> Iterator[Tuple[int, Any]]:
    """(page number from 1, page) for every page, each closed once read."""
    for page_no, page in enumerate(pdf.pages, start=1):
        try:
            yield page_no, page
        finally:
            page.close()


def page_text(pdf, index: int, **kwargs) -> str:
    """One page's text, the page closed afterwards; "" for an empty PDF."""
    if not pdf.pages:
        return ""
    page = pdf.pages[index]
    try:
        return page.extract_text(**kwargs) or ""
    finally:
        page.close()


def page_tables(pdf) -> List[Tuple[int, List[List[List[Optional[str]]]]]]:
    """(page number from 1, page.extract_tables()) for every page, in order."""
    n = len(pdf.pages)
    workers = min(os.cpu_count() or 1, _MAX_WORKERS, -(-n // _CHUNK_PAGES))
    if n >= PARALLEL_FROM_PAGES and workers > 1:
        try:
            return _tables_in_parallel(pdf.stream, n, workers)
        except (OSError, BrokenProcessPool) as exc:  # no processes here: read in this one
            log.warning("Reading %d pages in one process: %s", n, exc)
    return [(page_no, page.extract_tables()) for page_no, page in iter_pages(pdf)]


def _tables_in_parallel(stream, n: int, workers: int):
    stream.seek(0)
    data = stream.read()
    starts = range(0, n, _CHUNK_PAGES)
    # spawn, not fork: this runs inside the server's worker threads.
    with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"),
                             initializer=_open_in_worker, initargs=(data,)) as pool:
        chunks = pool.map(_read_tables, starts, [min(s + _CHUNK_PAGES, n) for s in starts])
        return [(page_no, tables) for page_no, tables in
                enumerate(itertools.chain.from_iterable(chunks), start=1)]


_worker_pdf = None


def _open_in_worker(data: bytes) -> None:
    global _worker_pdf
    import pdfplumber
    _worker_pdf = pdfplumber.open(io.BytesIO(data))


def _read_tables(start: int, stop: int):
    out = []
    for page in _worker_pdf.pages[start:stop]:
        try:
            out.append(page.extract_tables())
        finally:
            page.close()
    return out


# ------------------------------------------------------------- helpers -----
def clean(cell: Optional[str]) -> str:
    return (cell or "").strip()


def to_int(value: str) -> Optional[int]:
    value = (value or "").replace(",", "").replace("\u00a0", "").strip()
    if not value or value.upper() == "N/A":
        return None
    neg = value.startswith("(") and value.endswith(")")
    if neg:
        value = value[1:-1]
    try:
        n = int(round(float(value)))
    except ValueError:
        return None
    return -n if neg else n


_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%d %b %y",
    "%d %B %Y",
)


def to_iso(value: str) -> str:
    v = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).isoformat()
        except ValueError:
            continue
    raise StatementParseError(f"Unrecognised date format: {value!r}")


def dedupe_name(name: str) -> str:
    """Collapse 'X Y X Y' -> 'X Y' produced by the statement generator."""
    if not name:
        return name
    words = name.split()
    n = len(words)
    if n % 2 == 0 and words[: n // 2] == words[n // 2:]:
        return " ".join(words[: n // 2])
    return name


def split_party(cell: str) -> tuple[str, str]:
    """'250788564258\\nAudace TUYISENGE' -> ('250788564258', 'Audace TUYISENGE')."""
    lines = [p.strip() for p in (cell or "").split("\n") if p.strip()]
    if not lines:
        return "", ""
    return lines[0], dedupe_name(" ".join(lines[1:]).strip())


_MSISDN_RE = re.compile(r"\b(?:250)?(7[0-9]{8})\b")


def find_msisdn(text: str) -> Optional[str]:
    """Pull a Rwandan mobile number out of free text, normalised to 2507XXXXXXXX."""
    m = _MSISDN_RE.search((text or "").replace(" ", ""))
    return f"250{m.group(1)}" if m else None


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
