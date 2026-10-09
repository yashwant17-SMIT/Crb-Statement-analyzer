"""
Tests for the multi-format statement package.

The pure-logic tests run everywhere. The parser tests need real PDFs, because
the whole point of an adapter is what it does to a real file:

    MOMO_FIXTURE=/path/to/momo-app-statement.pdf \
    MOMO_EXCEL_FIXTURE=/path/to/momo-excel-export.pdf \
    MOMO_PORTAL_FIXTURE=/path/to/momo-portal-export.pdf \
    BK_FIXTURE=/path/to/bank-of-kigali-statement.pdf \
    pytest -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.analyzer import analyze
from app.config import Policy
from app.statements import (
    Capabilities,
    ParsedStatement,
    StatementHeader,
    StatementParseError,
    Txn,
    parse_statement,
)
from app.statements.registry import PROVIDERS, provider_options

MOMO = os.getenv("MOMO_FIXTURE", "")
EXCEL = os.getenv("MOMO_EXCEL_FIXTURE", "")
PORTAL = os.getenv("MOMO_PORTAL_FIXTURE", "")
BK = os.getenv("BK_FIXTURE", "")

needs_momo = pytest.mark.skipif(not MOMO or not Path(MOMO).exists(), reason="set MOMO_FIXTURE")
needs_excel = pytest.mark.skipif(not EXCEL or not Path(EXCEL).exists(), reason="set MOMO_EXCEL_FIXTURE")
needs_portal = pytest.mark.skipif(not PORTAL or not Path(PORTAL).exists(), reason="set MOMO_PORTAL_FIXTURE")
needs_bk = pytest.mark.skipif(not BK or not Path(BK).exists(), reason="set BK_FIXTURE")

ACC = "250700000001"


def txn(i, ts, frm, frm_name, to, to_name, amount, direction=None, ttype="TRANSFER"):
    return Txn(str(100000 + i), "SUCCESSFUL", ttype, ts, frm, frm_name, to, to_name,
               amount, 0, None, 1, direction)


def stmt(rows, caps=None, header=None):
    return ParsedStatement(
        header=header or StatementHeader(customer="Jane DOE", msisdn=ACC),
        transactions=rows,
        pages=1,
        source_format="test",
        capabilities=caps or Capabilities(),
    )


# ------------------------------------------------------------- the registry --
def test_every_provider_lists_at_least_one_format():
    for key, spec in PROVIDERS.items():
        assert spec["formats"], f"provider {key} has no adapters"


def test_provider_options_is_serialisable_for_the_console():
    opts = provider_options()
    assert {o["value"] for o in opts} == set(PROVIDERS)
    for o in opts:
        assert o["label"] and all(f["name"] and f["label"] for f in o["formats"])


def test_momo_app_sniff_is_whitespace_tolerant():
    from app.statements import mtn_momo
    text = "Momo   Statement\nTotal Transactions: 10"
    assert mtn_momo.sniff(text) == 0.99


def test_momo_portal_sniff_and_party_ids():
    from app.statements import mtn_momo, mtn_momo_portal
    head = "Id External id Date Status Type From From name To To name From / Fee Amount Balance Currency FRI:"
    assert mtn_momo_portal.sniff(head) > 0.9
    assert mtn_momo.sniff(head) == 0.0
    assert mtn_momo_portal._party_id("FRI:250788751206/MSISDN") == "250788751206"
    assert mtn_momo_portal._party_id("FRI:23671830/MM") == "23671830"
    # A service-provider leg must not collapse onto the holder's own number.
    assert mtn_momo_portal._party_id("FRI:250788751206@airtime.mtn/SP") == "250788751206@airtime.mtn"


def test_unknown_provider_is_rejected_by_name():
    with pytest.raises(StatementParseError) as e:
        parse_statement(b"%PDF-1.4 not really", provider="barclays")
    assert "barclays" in str(e.value)


# --------------------------------------------------- direction and dropped rows
def test_stated_direction_wins_over_identifier_comparison():
    """A bank statement has no counterparty id, so the adapter states direction."""
    rows = [
        txn(1, "2026-01-05T00:00:00", "name:acme", "ACME", "ACC-NO", "Jane", 5000, "in"),
        txn(2, "2026-01-06T00:00:00", "ACC-NO", "Jane", "name:wasac", "WASAC", 2000, "out"),
    ]
    header = StatementHeader(customer="Jane DOE", msisdn="ACC-NO")
    report = analyze(stmt(rows, Capabilities(cp_id_basis="derived_name"), header), Policy())
    assert report["disclosures"]["inflow_count"] == 1
    assert report["disclosures"]["outflow_count"] == 1


def test_unattributable_rows_are_disclosed_not_dropped():
    """A row belonging to neither side used to vanish from every figure."""
    rows = [
        txn(1, "2026-01-05T09:00:00", "250788000001", "Cust", ACC, "Jane", 10_000),
        txn(2, "2026-01-06T09:00:00", "250788000002", "A", "250788000003", "B", 900_000),
    ]
    report = analyze(stmt(rows), Policy())
    assert report["disclosures"]["unattributed_count"] == 1
    assert report["disclosures"]["unattributed_total"] == 900_000


# ----------------------------------------------------- capability-aware rules --
def test_self_transfer_by_identity_when_no_names_are_available():
    """The Excel export carries no names; the sender IS the account instead."""
    rows = [
        txn(1, "2026-01-05T09:00:00", ACC, "", ACC, "", 500_000),
        txn(2, "2026-01-06T09:00:00", "250788000002", "", ACC, "", 40_000),
    ]
    caps = Capabilities(counterparty_name=False, balance=False)
    report = analyze(stmt(rows, caps), Policy())
    assert report["summary"]["totals"]["self_transfers"] == 500_000
    assert report["summary"]["totals"]["net_revenue"] == 40_000


def test_missing_names_are_declared_not_silently_passed():
    caps = Capabilities(counterparty_name=False, balance=False)
    rows = [txn(1, "2026-01-05T09:00:00", "250788000002", "", ACC, "", 40_000)]
    d = analyze(stmt(rows, caps), Policy())["disclosures"]
    checks = {n["check"]: n["status"] for n in d["capability_notes"]}
    assert checks["distinct_individual_senders"] == "not_assessed"
    assert checks["low_liquidity"] == "not_assessed"
    # A count that cannot be computed is null, never a misleading zero.
    assert d["distinct_individual_senders"] is None


def test_loan_detected_from_transaction_type_without_a_lender_name():
    rows = [txn(1, "2026-01-05T09:00:00", "250796760009", "", ACC, "", 20_000,
                ttype="CUSTOM_LOAN_PAYOUT")]
    caps = Capabilities(counterparty_name=False, balance=False)
    report = analyze(stmt(rows, caps), Policy())
    assert report["summary"]["totals"]["loan_inflows"] == 20_000
    assert report["summary"]["totals"]["net_revenue"] == 0


def test_round_trip_window_is_not_assessed_at_day_precision():
    rows = [
        txn(1, "2026-01-05T00:00:00", "name:x", "X", "ACC-NO", "Jane", 800_000, "in"),
        txn(2, "2026-01-05T00:00:00", "ACC-NO", "Jane", "name:y", "Y", 800_000, "out"),
    ]
    header = StatementHeader(customer="Jane DOE", msisdn="ACC-NO")
    caps = Capabilities(cp_id_basis="derived_name", timestamp_precision="day", txn_type=False)
    report = analyze(stmt(rows, caps, header), Policy())
    checks = {n["check"]: n["status"] for n in report["disclosures"]["capability_notes"]}
    assert checks["rapid_round_trips"] == "not_assessed"


# -------------------------------------------------------------- float account --
def test_float_account_gets_no_revenue_figure():
    header = StatementHeader(customer="Jane DOE", msisdn="ACC-NO",
                             account_type="BK Agent Float & Commission")
    rows = [txn(1, "2026-01-05T00:00:00", "name:x", "X", "ACC-NO", "Jane", 900_000, "in")]
    report = analyze(stmt(rows, Capabilities(cp_id_basis="derived_name"), header), Policy())
    assert report["summary"]["revenue_assessed"] is False
    assert report["summary"]["totals"]["net_revenue"] is None
    assert report["red_flags"][0]["code"] == "FLOAT_ACCOUNT"
    # Gross is still stated - the file is not unreadable, it is the wrong document.
    assert report["summary"]["totals"]["gross_inflows"] == 900_000


def test_float_block_can_be_turned_off_but_the_flag_stays():
    header = StatementHeader(customer="Jane DOE", msisdn="ACC-NO",
                             account_type="BK Agent Float & Commission")
    rows = [txn(1, "2026-01-05T00:00:00", "name:x", "X", "ACC-NO", "Jane", 900_000, "in")]
    policy = Policy()
    policy.block_revenue_on_float_accounts = False
    report = analyze(stmt(rows, Capabilities(cp_id_basis="derived_name"), header), policy)
    assert report["summary"]["revenue_assessed"] is True
    assert report["summary"]["totals"]["net_revenue"] == 900_000
    assert any(f["code"] == "FLOAT_ACCOUNT" for f in report["red_flags"])


# ------------------------------------------------------------------- policy ---
def test_min_band_is_normalised_and_bad_values_fail_loudly():
    assert Policy(min_band="bb").min_band == "BB"
    with pytest.raises(ValueError):
        Policy(min_band="B")


# ---------------------------------------------------------------- real files --
@needs_momo
def test_momo_app_statement_dispatches_and_reconciles():
    st = parse_statement(Path(MOMO).read_bytes(), provider="mtn_momo")
    assert st.source_format == "mtn_momo_v1"
    assert st.parse_complete
    assert st.capabilities.counterparty_name and st.capabilities.balance


@needs_excel
def test_momo_excel_export_dispatches_and_infers_the_account():
    st = parse_statement(Path(EXCEL).read_bytes(), provider="mtn_momo")
    assert st.source_format == "mtn_momo_excel_v1"
    assert st.header.msisdn
    assert not st.capabilities.counterparty_name
    assert st.rejected_rows == []
    assert all(t.direction in ("in", "out") for t in st.transactions)


@needs_portal
def test_momo_portal_export_survives_overflowing_names():
    st = parse_statement(Path(PORTAL).read_bytes(), provider="mtn_momo")
    assert st.source_format == "mtn_momo_portal_v1"
    assert st.rejected_rows == []
    acct = st.header.msisdn
    assert all(acct in (t.from_msisdn, t.to_msisdn) for t in st.transactions)
    # Newest first: every row's balance follows from the one before it, which
    # only holds if no overflowing name leaked into a number column.
    rows = st.transactions
    for newer, older in zip(rows, rows[1:]):
        if newer.balance_after is None or older.balance_after is None:
            continue
        sign = 1 if newer.direction == "in" else -1
        assert older.balance_after + sign * newer.amount - (newer.fee or 0) == newer.balance_after


@needs_bk
def test_bk_statement_balance_identity_closes():
    st = parse_statement(Path(BK).read_bytes(), provider="bank_of_kigali")
    assert st.source_format == "bk_statement_v1"
    assert st.balance_reconciles is True
    assert st.rejected_rows == []


@needs_bk
def test_wrong_provider_is_refused_rather_than_misparsed():
    with pytest.raises(StatementParseError) as e:
        parse_statement(Path(BK).read_bytes(), provider="mtn_momo")
    assert "does not look like" in str(e.value)


# ---- MTN app statement: timestamps wrapped inside a narrow Date column -------
class _FakePage:
    def __init__(self, rows, text=""):
        self._rows, self._text = rows, text

    def extract_tables(self):
        return [self._rows]

    def extract_text(self):
        return self._text


class _FakePdf:
    def __init__(self, *pages):
        self.pages = list(pages)


_MOMO_HEAD = ["Transaction\nId", "Status", "Transaction Type", "Date", "From", "To",
              "Amount\n(RWF)", "Fee\n(RWF)", "Balance\nAfter\n(RWF)"]
_MOMO_TEXT = ("Momo Statement\nCustomer: Eric MUGISHA\nNumber: 250787222222\n"
              "Total Transactions: 4\n")


def _momo_row(txid, date, frm="250795470715\nAlice UWASE", to="250787222222\nIsaac Isaac",
              amount="1000", bal="5000"):
    return [txid, "SUCCESSFUL", "TRANSFER", date, frm, to, amount, "N/A", bal]


def test_momo_app_timestamp_wrapped_across_three_lines():
    from app.statements import mtn_momo
    page = _FakePage([_MOMO_HEAD, _momo_row("30945592683", "2026-\n10-01\n23:22:50")], _MOMO_TEXT)
    st = mtn_momo.parse(_FakePdf(page))
    assert [t.timestamp for t in st.transactions] == ["2026-10-01T23:22:50"]
    assert not st.rejected_rows


def test_momo_app_timestamp_finished_on_the_next_page():
    """At a page break the date can start on one row and end on the continuation."""
    from app.statements import mtn_momo
    p1 = _FakePage([_MOMO_HEAD, _momo_row("30587934046", "2026-",
                                          frm="250780426400\nINTAGANZWA SACCO")], _MOMO_TEXT)
    p2 = _FakePage([_MOMO_HEAD,
                    ["", "", "", "09-25\n17:21:25", "KIREHE", "", "", "", ""],
                    _momo_row("30587934047", "2026-09-25 17:30:00")])
    st = mtn_momo.parse(_FakePdf(p1, p2))
    assert [t.timestamp for t in st.transactions] == ["2026-09-25T17:21:25", "2026-09-25T17:30:00"]
    assert st.transactions[0].from_name == "INTAGANZWA SACCO KIREHE"
    assert not st.rejected_rows


def test_momo_app_original_two_line_timestamp_still_parses():
    from app.statements import mtn_momo
    page = _FakePage([_MOMO_HEAD, _momo_row("28160779051", "2026-05-27\n07:52:10")], _MOMO_TEXT)
    assert mtn_momo.parse(_FakePdf(page)).transactions[0].timestamp == "2026-05-27T07:52:10"


def test_momo_app_unparseable_timestamp_is_rejected_not_dropped():
    from app.statements import mtn_momo
    page = _FakePage([_MOMO_HEAD, _momo_row("1234567", "garbage"),
                      _momo_row("1234568", "2026-10-01 10:00:00")], _MOMO_TEXT)
    st = mtn_momo.parse(_FakePdf(page))
    assert len(st.transactions) == 1 and len(st.rejected_rows) == 1
    assert "garbage" in st.rejected_rows[0].reason


# ------------------------------------------------ BK branch-printed layout --
BK_BRANCH_PAGE1 = """4 August 2026
14:11:22
Account Statement for the period 20260501 To 20260801
Account Number : 100100100100 Address :
Account Name : Jane DOE
Currency : RWF Phone : +250780000000
Printed By : A TELLER Email : jane@example.com
Branch : KIMIRONKO BRANCH Account Type: Individual Current account
Date : 04 AUG 2026 IBAN : RW28040100100100100646
Book Date Reference Description Narration Value Date Debit Credit Closing Balance
Balance at Perio 828
d Start
01 MAY 26 FTCM26120WOJ BK to BK NEW M 3Jane DOE 01 MAY 26 510,000 510,828
"""


def test_bk_branch_layout_is_recognised_and_its_header_read():
    from app.statements import bk_branch, bk_statement
    assert bk_branch.sniff(BK_BRANCH_PAGE1) >= 0.9
    # The internet-banking adapter must not claim it, and vice versa.
    assert bk_statement.sniff(BK_BRANCH_PAGE1) == 0.0
    h = bk_branch._parse_header(BK_BRANCH_PAGE1)
    assert (h.msisdn, h.customer, h.contact_phone) == ("100100100100", "Jane DOE", "+250780000000")
    assert h.account_type == "Individual Current account"
    assert h.period_start.startswith("2026-05-01") and h.period_end.startswith("2026-08-01")
    assert h.generated_at == "2026-08-04T14:11:22"


def test_bk_branch_wrapped_cells_rejoin_mid_word_only():
    from app.statements.bk_branch import _join
    edge = 230
    assert _join([("MTN Push to Ba", 222), ("nk", 177)], edge) == "MTN Push to Bank"
    assert _join([("BK to BK NEW M", 225), ("OBISERVE", 206), ("JEANNE UWAYO", 227)], edge) == \
        "BK to BK NEW MOBISERVE JEANNE UWAYO"
