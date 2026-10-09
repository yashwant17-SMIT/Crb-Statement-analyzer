"""
Statement parsing, one adapter per provider format.

Every adapter returns the same ParsedStatement, so analyzer.py never learns
which format produced the rows it is given.
"""
from __future__ import annotations

import io

import pdfplumber

from .base import (  # noqa: F401 - re-exported as the package's public types
    Capabilities,
    ParsedStatement,
    RejectedRow,
    StatementHeader,
    StatementParseError,
    Txn,
)
from .registry import ADAPTERS, PROVIDERS, provider_options

SNIFF_THRESHOLD = 0.5


def parse_statement(data: bytes, provider: str | None = None) -> ParsedStatement:
    """Parse raw PDF bytes into a ParsedStatement.

    `provider` is what the operator selected in the console. When given, only
    that provider's formats are considered, and a file that clearly belongs to
    a different provider is rejected rather than silently misparsed. When
    omitted, every adapter is scored - kept for the API and for tests.
    """
    if provider is not None and provider not in PROVIDERS:
        raise StatementParseError(
            f"Unknown provider {provider!r}. Supported: "
            + ", ".join(sorted(PROVIDERS))
        )

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - surface any pdf backend failure
        raise StatementParseError(f"File could not be opened as a PDF: {exc}") from exc

    with pdf:
        first_page = pdf.pages[0].extract_text(layout=True) if pdf.pages else ""
        scored = sorted(
            ((a.sniff(first_page or ""), a) for a in ADAPTERS),
            key=lambda pair: pair[0],
            reverse=True,
        )
        candidates = PROVIDERS[provider]["formats"] if provider else ADAPTERS
        best_score, best = max(
            ((a.sniff(first_page or ""), a) for a in candidates),
            key=lambda pair: pair[0],
        )

        if best_score < SNIFF_THRESHOLD:
            top_score, top = scored[0]
            if provider and top_score >= SNIFF_THRESHOLD:
                raise StatementParseError(
                    f"This file does not look like a {PROVIDERS[provider]['label']} "
                    f"statement - it matches {top.LABEL}. Re-upload it with the "
                    "correct provider selected."
                )
            raise StatementParseError(
                "Unrecognised statement format. Supported formats: "
                + "; ".join(a.LABEL for a in ADAPTERS)
                + ". A scanned or photographed statement will not parse."
            )

        return best.parse(pdf)
