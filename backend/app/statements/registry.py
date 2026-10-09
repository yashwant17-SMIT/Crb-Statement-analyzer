"""Adapter registry, keyed by the provider the operator picks in the console."""
from __future__ import annotations

from typing import Dict, List

from . import bk_branch, bk_statement, mtn_momo, mtn_momo_excel, mtn_momo_portal

ADAPTERS = [mtn_momo, mtn_momo_excel, mtn_momo_portal, bk_statement, bk_branch]

# Provider -> the formats that provider issues. The operator picks the
# provider; the format within it is sniffed, because a user knows which bank
# the file came from and cannot reliably know which export version it is.
PROVIDERS: Dict[str, Dict] = {
    "mtn_momo": {
        "label": "MTN MoMo",
        "formats": [mtn_momo, mtn_momo_excel, mtn_momo_portal],
    },
    "bank_of_kigali": {
        "label": "Bank of Kigali",
        "formats": [bk_statement, bk_branch],
    },
}


def provider_options() -> List[Dict[str, str]]:
    """Payload for the console's provider select."""
    return [
        {
            "value": key,
            "label": spec["label"],
            "formats": [
                {"name": f.NAME, "label": f.LABEL} for f in spec["formats"]
            ],
        }
        for key, spec in PROVIDERS.items()
    ]
