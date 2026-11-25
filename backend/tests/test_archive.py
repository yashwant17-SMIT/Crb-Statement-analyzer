"""
The JSON archive: folder naming, the file it writes, and the two routes that
write it (the console's save and the intake row).

    pytest -q tests/test_archive.py
"""
from __future__ import annotations

import json
import os
import re

import pytest

from app import archive
from tests.test_intake import CLIENT, CRB, engine, post  # noqa: F401  (engine is a fixture)

REPORT = {"account": {"customer": "MUKAMANA Alice", "msisdn": "250788000111"},
          "summary": {"net_revenue": 450000}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "DIR", str(tmp_path))
    return tmp_path


def files_in(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# --------------------------------------------------------------- the module ---
def test_folder_is_the_name_without_its_title_then_the_national_id():
    crb = {"applicant": {"names": "MR MUGISHA ERIC", "national_id": "1199080000000001"}}
    assert archive.customer_folder(crb, REPORT) == "mugisha-eric_1199080000000001"
    assert archive.customer_folder(CRB, REPORT) == archive.customer_folder(CRB, None), \
        "a re-run must land in the same folder"


def test_statement_only_runs_key_on_the_msisdn():
    assert archive.customer_folder(None, REPORT) == "mukamana-alice_250788000111"


def test_folder_names_cannot_leave_the_archive():
    crb = {"applicant": {"names": "../../etc/passwd", "national_id": "..\\..\\x"}}
    folder = archive.customer_folder(crb, None)
    assert "/" not in folder and "\\" not in folder and ".." not in folder
    assert archive.customer_folder({"applicant": {}}, None) == "unknown-applicant"


def test_save_writes_one_complete_file(store):
    out = archive.save(CRB, REPORT, {"crb": "crb.pdf", "statement": "jan.pdf"}, source="console",
                       inputs={"customer_type": "new", "requested_amount": 500000})
    assert files_in(store) == [out["path"]]
    assert re.fullmatch(r"[a-z0-9-]+_\d+/\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.json", out["path"])
    doc = json.loads((store / out["path"]).read_text())
    assert doc["assessment_id"] == out["assessment_id"]
    assert doc["source"] == "console"
    assert doc["credit_bureau"] == CRB and doc["statement"] == REPORT
    assert doc["files"]["crb"] == "crb.pdf"
    assert doc["inputs"]["requested_amount"] == 500000
    assert doc["customer"]["national_id"] == CRB["applicant"]["national_id"]
    assert oct(os.stat(store / out["path"]).st_mode & 0o777) == "0o600"


def test_a_rerun_adds_a_file_and_never_overwrites(store, monkeypatch):
    class Frozen(archive.datetime):  # three saves inside the same second
        @classmethod
        def now(cls, tz=None):
            return archive.datetime(2026, 11, 25, 16, 47, 3)

    monkeypatch.setattr(archive, "datetime", Frozen)
    saved = [archive.save(CRB, None, {}, source="console")["path"] for _ in range(3)]
    folder = archive.customer_folder(CRB, None)
    assert saved == [f"{folder}/2026-11-25_16-47-03.json", f"{folder}/2026-11-25_16-47-03_2.json",
                     f"{folder}/2026-11-25_16-47-03_3.json"]
    assert files_in(store) == sorted(saved), "no temp file is left behind"
    ids = {json.loads((store / p).read_text())["assessment_id"] for p in saved}
    assert len(ids) == 3


@pytest.mark.parametrize("aid", ["../../evil", "A2026", "a20261125-101500-ab12", "A20261125-101500-AB12/x"])
def test_only_real_assessment_ids_are_accepted(store, aid):
    with pytest.raises(archive.ArchiveError, match="Not an assessment ID"):
        archive.save(CRB, None, {}, source="console", assessment_id=aid)
    assert files_in(store) == []


def test_nothing_to_save_and_not_configured(store, monkeypatch):
    with pytest.raises(archive.ArchiveError, match="Nothing to save"):
        archive.save(None, None, {}, source="console")
    monkeypatch.setattr(archive, "DIR", "")
    with pytest.raises(archive.ArchiveError, match="not configured"):
        archive.save(CRB, None, {}, source="console")


def test_an_unwritable_folder_is_a_clear_error_and_leaves_no_temp_file(store):
    store.chmod(0o500)
    try:
        with pytest.raises(archive.ArchiveError, match="can write to it"):
            archive.save(CRB, None, {}, source="console")
    finally:
        store.chmod(0o700)
    assert files_in(store) == []


# ------------------------------------------------------------- console route ---
def test_console_route_status_and_save(store):
    assert CLIENT.get("/api/v1/archive/status").json() == {"configured": True}
    res = CLIENT.post("/api/v1/archive/save", json={
        "credit_bureau": CRB, "statement": REPORT, "files": {"crb": "crb.pdf"},
        "inputs": {"customer_type": "existing"}, "assessment_id": "A20261125-101500-AB12"})
    assert res.status_code == 200, res.text
    assert res.json()["assessment_id"] == "A20261125-101500-AB12"
    doc = json.loads((store / res.json()["path"]).read_text())
    assert doc["source"] == "console" and doc["assessment_id"] == "A20261125-101500-AB12"


def test_console_route_refuses_when_off_or_malformed(store, monkeypatch):
    bad = CLIENT.post("/api/v1/archive/save", json={"credit_bureau": "text", "statement": None})
    assert bad.status_code == 400
    failed = CLIENT.post("/api/v1/archive/save", json={"credit_bureau": None, "statement": None})
    assert failed.status_code == 502 and failed.json()["detail"]["error"] == "archive_failed"
    monkeypatch.setattr(archive, "DIR", "")
    assert CLIENT.get("/api/v1/archive/status").json() == {"configured": False}
    off = CLIENT.post("/api/v1/archive/save", json={"credit_bureau": CRB})
    assert off.status_code == 503


# -------------------------------------------------------------- intake route ---
def test_an_intake_row_is_archived_under_its_sheet_assessment_id(engine, store):
    engine["gate"](True)
    res = post(sno="41")
    assert res.status_code == 200, res.text
    body = res.json()
    aid = body["results"]["assessment_id"]
    assert body["archive"]["assessment_id"] == aid
    doc = json.loads((store / body["archive"]["path"]).read_text())
    assert doc["source"] == "intake"
    assert doc["inputs"]["sno"] == "41" and doc["inputs"]["customer_type"] == "existing"
    assert doc["files"]["statement"] == "jan.pdf, feb.pdf"
    assert doc["statement"] is not None


def test_a_failed_archive_never_fails_the_intake_row(engine, store, monkeypatch):
    engine["gate"](False)

    def broken(*a, **k):
        raise archive.ArchiveError("disk full")

    monkeypatch.setattr(archive, "save", broken)
    res = post()
    assert res.status_code == 200
    assert res.json()["archive"] == {"error": "disk full"}
    assert res.json()["status"]


def test_intake_without_an_archive_is_unchanged(engine, monkeypatch):
    monkeypatch.setattr(archive, "DIR", "")
    engine["gate"](False)
    assert "archive" not in post().json()
