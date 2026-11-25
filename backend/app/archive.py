"""
Assessment archive: each finished assessment as one JSON file on disk, in a
folder per customer.

    $ASSESSMENTS_DIR/<name>_<national id or MSISDN>/<YYYY-MM-DD_HH-MM-SS>.json

    e.g. mugisha-eric_1199080000000001/2026-11-25_16-47-03.json

The file holds what the console shows: the bureau result, the statement
report (either may be null, as with a bureau file that stopped at the gate),
the upload names, the inputs the assessment ran with, and the assessment ID
that matches its Sheets row. A re-run writes a new file next to the old ones;
nothing is overwritten. Off while ASSESSMENTS_DIR is unset.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .sheets import new_assessment_id

DIR = os.getenv("ASSESSMENTS_DIR", "").strip()

# new_assessment_id()'s shape; an ID from the caller is checked against it.
_AID = re.compile(r"A\d{8}-\d{6}-[0-9A-F]{4}")
_UNSAFE = re.compile(r"[^a-z0-9]+")
# The bureau prints "MR MUGISHA ERIC"; the title is noise in a folder name.
_TITLES = {"mr", "mrs", "ms", "miss", "dr"}


class ArchiveError(Exception):
    """A failure the console should show the underwriter verbatim."""


def configured() -> bool:
    return bool(DIR)


def _customer(crb: Optional[dict], report: Optional[dict]) -> dict:
    # The same identity as the Sheets tab: the bureau national ID when there
    # is a bureau report, the statement holder and MSISDN when there is not.
    applicant = (crb or {}).get("applicant") or {}
    account = (report or {}).get("account") or {}
    return {"name": applicant.get("names") or account.get("customer"),
            "national_id": applicant.get("national_id"),
            "msisdn": account.get("msisdn")}


def customer_folder(crb: Optional[dict], report: Optional[dict]) -> str:
    """A stable, filesystem-safe folder name, so a re-run lands next to the last one.

    The name is for people; the national ID (or MSISDN) after it keeps two
    customers with the same name apart.
    """
    c = _customer(crb, report)
    words = [w for w in str(c["name"] or "").lower().split() if w.strip(".") not in _TITLES]
    name = _UNSAFE.sub("-", " ".join(words)).strip("-")[:80].strip("-")
    ident = _UNSAFE.sub("", str(c["national_id"] or c["msisdn"] or "").lower())[:30]
    return "_".join(p for p in (name, ident) if p) or "unknown-applicant"


def save(crb: Optional[dict], report: Optional[dict], files: Optional[dict] = None, *,
         source: str, assessment_id: Optional[str] = None,
         inputs: Optional[dict] = None) -> dict:
    """Write one assessment. `assessment_id` ties the file to its Sheets row."""
    if not configured():
        raise ArchiveError("The JSON archive is not configured on this server.")
    if not crb and not report:
        raise ArchiveError("Nothing to save: no bureau result and no statement report.")
    at = datetime.now().replace(microsecond=0)
    if assessment_id is not None and not _AID.fullmatch(assessment_id):
        raise ArchiveError(f"Not an assessment ID: {assessment_id!r}.")
    aid = assessment_id or new_assessment_id(at)

    doc = {"assessment_id": aid, "saved_at": at.isoformat(), "source": source,
           "customer": _customer(crb, report), "files": files or {}, "inputs": inputs or {},
           "credit_bureau": crb, "statement": report}
    folder = Path(DIR) / customer_folder(crb, report)
    stamp = f"{at:%Y-%m-%d_%H-%M-%S}"
    tmp = folder / f".{aid}.json.tmp"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        # Written aside and linked into place, so a reader never sees half a
        # file. link() refuses an existing name, so two saves in the same
        # second get _2, _3 rather than one replacing the other.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2, default=str)
        n = 1
        while True:
            path = folder / (f"{stamp}.json" if n == 1 else f"{stamp}_{n}.json")
            try:
                os.link(tmp, path)
                break
            except FileExistsError:
                n += 1
        tmp.unlink()
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ArchiveError(f"Could not write to {folder}: {exc.strerror or exc}. "
                           "Check that the archive folder exists and the service can write to it.") from exc
    return {"assessment_id": aid, "path": str(path.relative_to(DIR))}
