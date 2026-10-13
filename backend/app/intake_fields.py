"""
Intake sheet cells -> the values the engine takes.

The intake sheet is typed by people, so the same customer type arrives as
"New", "new customer" or "NEW " and an amount as "1,000,000", "RWF 1 000 000"
or 1000000. Anything that cannot be read with certainty is refused with a
message naming the cell, rather than guessed: a misread amount changes the
sanction.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_TYPES = {
    "new": "new", "new customer": "new", "first time": "new", "first-time": "new",
    "existing": "existing", "existing customer": "existing", "repeat": "existing",
    "returning": "existing", "old": "existing",
}


class IntakeFieldError(ValueError):
    """A cell the underwriter has to fix in the sheet."""


def customer_type(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    if not text:
        raise IntakeFieldError("Customer type is empty. Enter New or Existing.")
    if text not in _TYPES:
        raise IntakeFieldError(f"Customer type {value!r} is not recognised. Enter New or Existing.")
    return _TYPES[text]


def requested_amount(value: Any) -> int:
    if isinstance(value, bool):
        raise IntakeFieldError(f"Requested amount {value!r} is not a number.")
    if isinstance(value, (int, float)):
        amount = value
    else:
        text = re.sub(r"(?i)\b(rwf|frw|rf)\b|[\s,']", "", str(value or ""))
        if not text:
            raise IntakeFieldError("Requested amount is empty.")
        if not re.fullmatch(r"\d+(\.0+)?", text):
            raise IntakeFieldError(f"Requested amount {value!r} is not a whole number of RWF.")
        amount = float(text)
    if amount != int(amount) or amount <= 0:
        raise IntakeFieldError(f"Requested amount {value!r} must be a positive whole number of RWF.")
    return int(amount)


_PROVIDER_ALIASES = {"mtn": "mtn_momo", "momo": "mtn_momo", "bk": "bank_of_kigali"}


def provider(value: Any) -> Optional[str]:
    """A provider key, or None to let the parser detect it."""
    from .statements import provider_options

    key = re.sub(r"[^a-z0-9]", "", str(value or "").casefold())
    if not key:
        return None
    for p in provider_options():
        if key in (re.sub(r"[^a-z0-9]", "", p["value"]), re.sub(r"[^a-z0-9]", "", p["label"].casefold())):
            return p["value"]
    if key in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[key]
    names = ", ".join(p["label"] for p in provider_options())
    raise IntakeFieldError(f"Provider {value!r} is not recognised. Use one of: {names}, or leave it empty.")
