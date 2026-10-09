"""
Credit bureau gate tests.

The gate rule under test: the bureau check runs first, and the MoMo statement
is parsed only when the band is at or above the configured minimum (DD by
default, so AA/BB/CC/DD proceed and EE/FF/GG/HH/II/JJ do not).

    MOMO_CRB_FIXTURE=/path/to/bureau.pdf pytest -q
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.config import Policy
from app.crb_analyzer import assess_credit_report
from app.crb_parser import (
    BAND_LADDER, CreditAccount, CreditProfile, ParsedCreditReport,
    normalise_band, parse_credit_report,
)


def _report_date(days_old: int = 0) -> str:
    """A 'Generated' timestamp `days_old` days before now, in the bureau's own format."""
    return (datetime.now() - timedelta(days=days_old)).strftime("%d %b %Y %I:%M %p")


def make_report(band="AA", score=675, accounts=None, summary=None,
               report_date=None) -> ParsedCreditReport:
    # Fresh by default so band-only tests are not coupled to the freshness
    # gate; tests that care about report age pass report_date explicitly.
    return ParsedCreditReport(
        bureau="TransUnion Rwanda", subscriber="TEST LTD",
        report_date=report_date if report_date is not None else _report_date(0),
        pdf_id="x", profile=CreditProfile(names="MR TEST PERSON", national_id="1"),
        score=score, band=band, probability_of_default=1.16, adverse_action_reasons=[],
        summary=summary or {}, accounts=accounts or [], collaterals=[],
        collateral_duplicates_removed=0, insurance_policies=[], enquiries=[], pages=12,
    )


def account(**kw) -> CreditAccount:
    base = dict(index=1, account_type="Instalment account", classification="NORMAL RISK",
                account_status="CURRENT", principal_amount=1_000_000, balance_amount=500_000,
                arrears_amount=0.0, days_in_arrears=0, is_open=True)
    base.update(kw)
    return CreditAccount(**base)


# ------------------------------------------------------------- the gate ----
@pytest.mark.parametrize("band,allowed", [
    ("AA", True), ("BB", True), ("CC", True), ("DD", True),
    ("EE", False), ("FF", False), ("GG", False),
    ("HH", False), ("II", False), ("JJ", False),
])
def test_gate_admits_dd_and_above_only(band, allowed):
    r = assess_credit_report(make_report(band=band), Policy())
    assert r["gate"]["statement_parsing_allowed"] is allowed
    assert r["gate"]["decision"] == ("proceed" if allowed else "decline")


def test_gate_minimum_is_configurable():
    strict = Policy(); strict.min_band = "BB"
    assert assess_credit_report(make_report(band="BB"), strict)["gate"]["statement_parsing_allowed"]
    assert not assess_credit_report(make_report(band="CC"), strict)["gate"]["statement_parsing_allowed"]


def test_unreadable_band_refers_rather_than_guessing():
    r = assess_credit_report(make_report(band=None, score=None), Policy())
    assert r["gate"]["statement_parsing_allowed"] is False
    assert r["gate"]["decision"] == "refer"


def test_accepted_band_list_is_reported():
    g = assess_credit_report(make_report(), Policy())["gate"]
    assert g["bands_accepted"] == ["AA", "BB", "CC", "DD"]
    assert g["band_ladder"] == BAND_LADDER


# ------------------------------------------------- band normalisation ------
@pytest.mark.parametrize("raw,expected", [
    ("AA", "AA"), ("aa", "AA"), ("A", "AA"), ("675 - AA", "AA"),
    (" dd ", "DD"), ("D", "DD"), ("zz", None), ("", None), (None, None),
])
def test_band_normalisation_accepts_common_forms(raw, expected):
    assert normalise_band(raw, None) == expected


@pytest.mark.parametrize("score,band", [
    (900, "AA"), (675, "AA"), (668, "AA"), (667, "BB"), (660, "BB"),
    (654, "CC"), (649, "DD"), (648, "EE"), (617, "JJ"), (100, "JJ"),
])
def test_band_falls_back_to_score_when_letters_missing(score, band):
    assert normalise_band(None, score) == band


# ------------------------------------------------------------ findings ----
def test_band_alone_does_not_approve_a_delinquent_file():
    """The whole point of this module: an AA file can still be unlendable."""
    accts = [account(balance_amount=5_370_499, arrears_amount=3_037_167, days_in_arrears=65)]
    r = assess_credit_report(make_report(band="AA", accounts=accts), Policy())
    assert r["gate"]["statement_parsing_allowed"] is True      # band opens the gate
    assert r["gate"]["recommendation"] == "refer"              # but do not approve
    codes = {f["code"] for f in r["findings"] if f["severity"] == "blocking"}
    assert {"CAP_BREACH", "ARREARS", "DELINQUENT", "SCORE_DIVERGENCE"} <= codes


def test_clean_file_produces_no_blocking_findings():
    accts = [account(balance_amount=250_000, arrears_amount=0, days_in_arrears=0)]
    r = assess_credit_report(make_report(band="BB", accounts=accts), Policy())
    assert [f for f in r["findings"] if f["severity"] == "blocking"] == []
    assert r["gate"]["recommendation"] == "proceed"


def test_closed_accounts_are_not_counted_as_exposure():
    accts = [account(is_open=False, account_status="ACCOUNT CLOSED", balance_amount=9_000_000)]
    e = assess_credit_report(make_report(accounts=accts), Policy())["exposure"]
    assert e["total_open_balance"] == 0
    assert e["closed_account_count"] == 1


def test_current_status_does_not_mask_delinquency():
    accts = [account(account_status="CURRENT", days_in_arrears=65, arrears_amount=100_000)]
    codes = {f["code"] for f in assess_credit_report(make_report(accounts=accts), Policy())["findings"]}
    assert "DELINQUENT" in codes



def test_exposure_reports_ongoing_loans_and_total_monthly_emi():
    accts = [
        account(index=1, balance_amount=900_000, scheduled_payment=120_000,
                scheduled_payment_available=True),
        account(index=2, balance_amount=600_000, scheduled_payment=75_000,
                scheduled_payment_available=True),
        account(index=3, is_open=False, account_status="ACCOUNT CLOSED",
                balance_amount=2_000_000, scheduled_payment=90_000,
                scheduled_payment_available=True),
    ]
    e = assess_credit_report(make_report(accounts=accts), Policy())["exposure"]
    assert e["ongoing_loan_count"] == 2
    assert e["open_account_count"] == 2
    assert e["closed_account_count"] == 1
    assert e["scheduled_monthly_payments"] == 195_000
    assert e["scheduled_monthly_payments_status"] == "complete"
    assert e["emi_accounts_with_reported_payment"] == 2



def test_written_off_open_accounts_do_not_contribute_to_eligibility_emi():
    accts = [
        account(index=1, account_status="CURRENT", classification="NORMAL RISK",
                scheduled_payment=100_000, scheduled_payment_available=True),
        account(index=2, account_status="WRITTEN OFF", classification="WRITE OFF",
                scheduled_payment=200_000, scheduled_payment_available=True),
        account(index=3, account_status="CURRENT", classification="DOUBTFUL",
                scheduled_payment=0, scheduled_payment_available=False),
    ]
    e = assess_credit_report(make_report(accounts=accts), Policy())["exposure"]
    assert e["scheduled_monthly_payments"] == 100_000
    # The written-off account and the DOUBTFUL one are both held out: one is not
    # a live facility, the other is not being repaid.
    assert e["ongoing_emi_account_count"] == 1
    assert e["emi_accounts_with_reported_payment"] == 1
    assert e["emi_accounts_missing_payment"] == 0
    assert e["emi_accounts_excluded_from_eligibility"] == 2
    assert e["emi_scope"] == "open_performing_accounts_only"
    assert e["scheduled_monthly_payments_status"] == "complete"


def test_non_performing_accounts_are_not_deducted_but_are_surfaced():
    """A DOUBTFUL facility months past term is not an ongoing monthly payment.

    Excluding it raises apparent affordability, so it must arrive as a blocking
    finding rather than quietly vanish from the report.
    """
    accts = [
        account(index=1, classification="NORMAL RISK",
                scheduled_payment=40_000, scheduled_payment_available=True),
        account(index=2, classification="DOUBTFUL", is_non_performing=True,
                balance_amount=530_304, arrears_amount=603_817,
                repayment_terms="12 MONTHLY", principal_amount=752_000),
    ]
    r = assess_credit_report(make_report(accounts=accts), Policy())
    e = r["exposure"]
    assert e["scheduled_monthly_payments"] == 40_000          # no synthetic 62,667
    assert e["ongoing_loan_count"] == 1                        # active loans, not open accounts
    assert e["unserviced_obligations"]["count"] == 1
    assert e["unserviced_obligations"]["balance"] == 530_304
    codes = {f["code"] for f in r["findings"] if f["severity"] == "blocking"}
    assert "UNSERVICED_OBLIGATION" in codes


def test_non_performing_emi_can_be_deducted_when_policy_says_so():
    accts = [
        account(index=2, classification="DOUBTFUL", is_non_performing=True,
                balance_amount=530_304, repayment_terms="12 MONTHLY",
                principal_amount=752_000),
    ]
    e = assess_credit_report(
        make_report(accounts=accts),
        Policy(deduct_emi_on_non_performing=True),
    )["exposure"]
    assert e["scheduled_monthly_payments"] == 62_667
    assert e["unserviced_obligations"]["count"] == 0


def test_no_ongoing_emi_is_zero_not_missing_when_only_written_off_accounts_have_emi():
    accts = [
        account(index=1, account_status="WRITTEN OFF", classification="WRITE OFF",
                scheduled_payment=200_000, scheduled_payment_available=True),
    ]
    r = assess_credit_report(make_report(accounts=accts), Policy())
    e = r["exposure"]
    assert e["scheduled_monthly_payments"] == 0
    assert e["scheduled_monthly_payments_status"] == "no_ongoing_emi"
    assert e["emi_accounts_excluded_from_eligibility"] == 1
    assert not any(f["code"] == "EMI_DATA_INCOMPLETE" for f in r["findings"])

def test_missing_emi_is_not_guessed_or_presented_as_complete():
    accts = [
        account(index=1, scheduled_payment=100_000, scheduled_payment_available=True),
        account(index=2, scheduled_payment=0, scheduled_payment_available=False),
    ]
    r = assess_credit_report(make_report(accounts=accts), Policy())
    e = r["exposure"]
    assert e["scheduled_monthly_payments"] == 100_000
    assert e["scheduled_monthly_payments_status"] == "partial"
    assert e["emi_accounts_with_reported_payment"] == 1
    assert e["emi_accounts_missing_payment"] == 1
    finding = next(f for f in r["findings"] if f["code"] == "EMI_DATA_INCOMPLETE")
    assert finding["severity"] == "warning"


def test_no_open_loans_has_zero_emi_without_warning():
    accts = [
        account(is_open=False, account_status="ACCOUNT CLOSED",
                scheduled_payment=100_000, scheduled_payment_available=True)
    ]
    r = assess_credit_report(make_report(accounts=accts), Policy())
    e = r["exposure"]
    assert e["ongoing_loan_count"] == 0
    assert e["scheduled_monthly_payments"] == 0
    assert e["scheduled_monthly_payments_status"] == "no_open_loans"
    assert not any(f["code"] == "EMI_DATA_INCOMPLETE" for f in r["findings"])

# ------------------------------------------------------- golden fixture ----
FIXTURE = os.getenv("MOMO_CRB_FIXTURE", "")
needs = pytest.mark.skipif(not FIXTURE or not Path(FIXTURE).exists(),
                           reason="set MOMO_CRB_FIXTURE to a real bureau report PDF")


@pytest.fixture(scope="module")
def parsed():
    return parse_credit_report(Path(FIXTURE).read_bytes())


@needs
def test_golden_profile_and_score(parsed):
    assert parsed.band == "AA" and parsed.score == 675
    assert parsed.profile.national_id == "1199280136620091"
    assert len(parsed.profile.phones) == 14


@needs
def test_golden_account_counts(parsed):
    assert len(parsed.accounts) == 27
    assert sum(a.is_open for a in parsed.accounts) == 3


@needs
def test_golden_exposure_reconciles_to_summary(parsed):
    e = assess_credit_report(parsed, Policy())["exposure"]
    assert e["total_open_balance"] == 10_134_341
    assert e["total_arrears"] == 3_962_660
    assert e["worst_days_in_arrears"] == 65


@needs
def test_golden_collateral_duplicates_are_removed(parsed):
    """23 identical entries; summing them would overstate security 23-fold."""
    assert len(parsed.collaterals) == 1
    assert parsed.collateral_duplicates_removed == 22


@needs
def test_golden_collateral_blocks_are_not_mistaken_for_accounts(parsed):
    """Collateral entries share the 'N) Trade Sector:' prefix with accounts."""
    assert len(parsed.accounts) == 27, "collateral blocks leaked into the account list"


@needs
def test_golden_payment_history_is_24_months(parsed):
    assert all(len(a.payment_history) == 24 for a in parsed.accounts if a.payment_history)


@needs
def test_statement_pdf_rejected_in_the_bureau_slot():
    stmt = os.getenv("MOMO_FIXTURE", "")
    if not stmt or not Path(stmt).exists():
        pytest.skip("set MOMO_FIXTURE too")
    from app.crb_parser import CreditReportParseError
    with pytest.raises(CreditReportParseError):
        parse_credit_report(Path(stmt).read_bytes())


def test_missing_reported_emi_can_be_calculated_from_principal_and_term():
    accts = [
        account(index=1, principal_amount=600_000, scheduled_payment=0,
                scheduled_payment_available=False, repayment_terms="6 Months"),
    ]
    r = assess_credit_report(make_report(accounts=accts), Policy())
    e = r["exposure"]
    assert e["ongoing_loan_count"] == 1
    assert e["scheduled_monthly_payments"] == 100_000
    assert e["scheduled_monthly_payments_status"] == "calculated"
    assert e["emi_accounts_with_calculated_payment"] == 1
    assert e["emi_accounts_missing_payment"] == 0
    row = e["accounts"][0]
    assert row["emi_source"] == "calculated_principal_only"
    assert row["calculated_emi"] == 100_000
    assert row["repayment_term_months"] == 6
    assert not any(f["code"] == "EMI_DATA_INCOMPLETE" for f in r["findings"])


def test_mixed_reported_and_calculated_emi_is_partial_but_usable_for_eligibility():
    accts = [
        account(index=1, scheduled_payment=120_000, scheduled_payment_available=True),
        account(index=2, principal_amount=600_000, scheduled_payment=0,
                scheduled_payment_available=False, repayment_terms="6 Months"),
    ]
    r = assess_credit_report(make_report(accounts=accts), Policy())
    e = r["exposure"]
    assert e["scheduled_monthly_payments"] == 220_000
    assert e["scheduled_monthly_payments_status"] == "calculated" or e["scheduled_monthly_payments_status"] == "partial"
    assert e["emi_accounts_with_reported_payment"] == 1
    assert e["emi_accounts_with_calculated_payment"] == 1


def test_performing_accounts_heading_is_not_matched_inside_npa_heading():
    """The SUMMARY and NPA heading both contain 'Performing Accounts'."""
    from app.crb_parser import _parse_accounts

    text = """SUMMARY\nNon-Performing Accounts 0 4\nOpen NPA 0 3\nPerforming Accounts 0 24\n\nNON-PERFORMING ACCOUNTS\n1) Trade Sector: Bank Sector Bureau\nAccount Type: MOBILE LOAN\nClassification: DOUBTFUL\nPrincipal Amount: RWF 43,000.00\nBalance Amount: RWF 47,890.00\nAccount Status: CURRENT\nAccount Ref: NPA-1\n\nPERFORMING ACCOUNTS\n1) Trade Sector: MicroFinance Sector\nAccount Type: Instalment account\nClassification: NORMAL RISK\nPrincipal Amount: RWF 350,000.00\nBalance Amount: RWF 160,955.00\nScheduled Payments: RWF 24,063.00\nAccount Status: CURRENT\nAccount Ref: PA-1\n"""
    accounts, _skipped = _parse_accounts(text)
    assert len(accounts) == 2
    assert [(a.account_ref, a.is_non_performing) for a in accounts] == [
        ("NPA-1", True), ("PA-1", False)
    ]


def test_bounced_cheque_block_is_not_read_as_an_open_credit_account():
    """`N) Trade Sector:` is shared by accounts, collaterals AND bounced cheques.

    The Bounced Cheques heading was missing from the section bounds, so the
    PERFORMING ACCOUNTS section ran on into it and produced a zero-value
    account with no type, class or status - counted as open, inflating the
    open-account count and diluting the EMI completeness ratio.
    """
    from app.crb_parser import _parse_accounts, _parse_bounced_cheques

    text = (
        "PERFORMING ACCOUNTS\n"
        "1) Trade Sector: MicroFinance Sector\n"
        "Account Type: Instalment account\n"
        "Classification: NORMAL RISK\n"
        "Principal Amount: RWF 300,000.00\n"
        "Balance Amount: RWF 300,000.00\n"
        "Account Status: CURRENT\n"
        "Account Ref: PA-1\n"
        "\n"
        "Payment History\n"
        "= Facility not yet assigned\n"
        "\n"
        "Bounced Cheques\n"
        "1) Trade Sector: Bank Sector Bureau\n"
        "Cheque Date: 26-Jun-2025\n"
        "Cheque No.: 00000006\n"
        "Bounce Reason: NSF(No Sufficient Funds)\n"
        "Cheque Amount: RWF 1,250,000.00\n"
        "Date Bounced: 08-Aug-2025\n"
    )
    accounts, _skipped = _parse_accounts(text)
    assert [a.account_ref for a in accounts] == ["PA-1"]

    cheques = _parse_bounced_cheques(text)
    assert len(cheques) == 1
    assert cheques[0]["amount"] == 1_250_000
    assert cheques[0]["trade_sector"] == "Bank Sector Bureau"
    assert cheques[0]["date_bounced"] == "08-Aug-2025"


def test_blocks_without_account_fields_are_skipped_not_counted():
    """Second line of defence if a future format moves a heading."""
    from app.crb_parser import _parse_accounts

    text = (
        "PERFORMING ACCOUNTS\n"
        "1) Trade Sector: MicroFinance Sector\n"
        "Account Type: Instalment account\n"
        "Principal Amount: RWF 300,000.00\n"
        "Account Status: CURRENT\n"
        "Account Ref: PA-1\n"
        "\n"
        "2) Trade Sector: Bank Sector Bureau\n"
        "Cheque Date: 26-Jun-2025\n"
        "Cheque Amount: RWF 1,250,000.00\n"
    )
    accounts, skipped = _parse_accounts(text)
    assert [a.account_ref for a in accounts] == ["PA-1"]
    assert len(skipped) == 1
    assert skipped[0]["index"] == "2"


def test_pa_open_total_value_is_not_read_from_the_npa_row():
    """'PA Open Total Value' is a substring of 'NPA Open Total Value'.

    An unanchored search reads the applicant's non-performing exposure into the
    performing figure, which is the more flattering of the two.
    """
    from app.crb_parser import _parse_summary

    text = (
        "SUMMARY\n"
        "Open NPA 1 1\n"
        "NPA Open Total Value (RWF) 350,680.00 530304.00\n"
        "Open PA 1 2\n"
        "PA Open Total Value(RWF) 480,655.00 313080.00\n"
    )
    s = _parse_summary(text)
    assert s["npa_open_total_value"] == {"your_sector": 350_680.0, "other_sectors": 530_304.0}
    assert s["pa_open_total_value"] == {"your_sector": 480_655.0, "other_sectors": 313_080.0}
    assert s["open_pa"]["other_sectors"] == 2.0


def test_zero_scheduled_payment_against_a_live_balance_is_not_a_reported_emi():
    """The bureau prints 0.00 as a placeholder on accounts that plainly have a
    schedule. Accepting it as reported both understates the obligation and
    suppresses the principal-only fallback."""
    from app.crb_parser import _parse_accounts

    # Two-space runs are the column breaks pdfplumber emits on this layout;
    # the field regexes terminate on them.
    text = (
        "PERFORMING ACCOUNTS\n"
        "1) Trade Sector: Non Deposit Taking  Principal Amount: RWF 400,000.00\n"
        "Account Type: Instalment account  Balance Amount: RWF 480,655.00\n"
        "Repayment Terms: 180 DAILY  Scheduled Payments: RWF 0.00\n"
        "Classification: NORMAL RISK  Account Status: CURRENT\n"
        "Account Ref: PA-1\n"
    )
    (a,), _ = _parse_accounts(text)
    assert a.scheduled_payment_printed_zero is True
    assert a.scheduled_payment_available is False
    # 180 daily instalments is about 5.9 months, so a principal-only estimate
    # is available where previously the account contributed a flat zero.
    assert a.calculated_emi_available is True
    assert 65_000 < a.calculated_emi < 70_000
    assert a.term_basis == "converted_from_daily"


def test_sub_month_repayment_term_is_refused_rather_than_converted():
    """'3 WEEKLY' is ambiguous between three instalments and a three-week cycle.
    Either way it yields no usable monthly term, so none is invented."""
    from app.crb_parser import _term_months

    inst, freq, months, basis, unusable = _term_months("3 WEEKLY")
    assert (inst, freq) == (3, "WEEKLY")
    assert months is None and basis is None
    assert "under one month" in unusable

    assert _term_months("12 MONTHLY")[2] == 12.0
    assert 3.9 < _term_months("120 DAILY")[2] < 4.0


def test_unreported_days_in_arrears_is_not_reported_as_zero_days_late():
    accts = [
        account(index=1, days_in_arrears=0, arrears_amount=603_817,
                is_non_performing=True, classification="DOUBTFUL"),
    ]
    accts[0].days_in_arrears_reported = False
    e = assess_credit_report(make_report(accounts=accts), Policy())["exposure"]
    assert e["worst_days_in_arrears"] is None
    assert e["worst_days_in_arrears_reported"] is False
    assert e["days_in_arrears_missing_count"] == 1


def test_portfolio_reconciles_parsed_accounts_against_the_bureau_summary():
    summary = {
        "open_pa": {"your_sector": 1.0, "other_sectors": 2.0},
        "open_npa": {"your_sector": 1.0, "other_sectors": 1.0},
        "pa_open_total_value": {"your_sector": 480_655.0, "other_sectors": 313_080.0},
        "npa_open_total_value": {"your_sector": 350_680.0, "other_sectors": 530_304.0},
        "closed_pa": {"your_sector": 0.0, "other_sectors": 31.0},
        "closed_npa": {"your_sector": 0.0, "other_sectors": 0.0},
    }
    # Only two of the three open performing accounts recovered.
    accts = [
        account(index=1, balance_amount=480_655),
        account(index=2, balance_amount=313_080),
        account(index=3, classification="LOSS", is_non_performing=True, balance_amount=350_680),
        account(index=4, classification="DOUBTFUL", is_non_performing=True, balance_amount=530_304),
    ]
    r = assess_credit_report(make_report(accounts=accts, summary=summary), Policy())
    p = r["exposure"]["portfolio"]
    assert p["performing"]["open_count"] == 2
    assert p["performing"]["bureau_open_count"] == 3
    assert p["reconciles"] is False
    assert "SUMMARY_MISMATCH" in {f["code"] for f in r["findings"]}


def test_supplied_transunion_report_has_four_open_accounts_and_no_duplicate_npas():
    """Regression test for the uploaded report that previously displayed 7 open rows."""
    fixture = Path("/mnt/data/1198670180735194.pdf")
    if not fixture.exists():
        pytest.skip("uploaded CRB fixture is not mounted")
    parsed = parse_credit_report(fixture.read_bytes())
    open_accounts = [a for a in parsed.accounts if a.is_open]
    assert len(open_accounts) == 4
    assert len({a.account_ref for a in open_accounts}) == 4
    assert sum(a.balance_amount for a in open_accounts) == pytest.approx(1_972_237.94)
    assert sum(a.scheduled_payment for a in open_accounts if a.scheduled_payment_available) == pytest.approx(24_063)
    assert {a.account_ref for a in open_accounts} == {
        "2054000165314422", "102249727", "2W655N", "LD2611417833"
    }


def test_non_performing_emi_is_excluded_from_the_headline_and_from_the_row_total():
    """Regression: the exposure table must foot to the figure above it.

    Two ways this went wrong. The NPA rows carry a `scheduled_payment` derived
    from their term, so summing that column double-counted debt that is not
    being serviced. And the headline was rounded independently of the rows, so
    the displayed numbers were a rupee off the displayed total.
    """
    accts = [
        account(index=1, classification="LOSS", is_non_performing=True,
                repayment_terms="120 DAILY", principal_amount=400_000,
                balance_amount=350_680, arrears_amount=350_680),
        account(index=2, classification="DOUBTFUL", is_non_performing=True,
                repayment_terms="12 MONTHLY", principal_amount=752_000,
                balance_amount=530_304, arrears_amount=603_817),
        account(index=3, classification="NORMAL RISK", repayment_terms="180 DAILY",
                principal_amount=400_000, balance_amount=480_655),
        account(index=4, classification="NORMAL RISK", scheduled_payment=3_429,
                scheduled_payment_available=True, balance_amount=300_000),
    ]
    e = assess_credit_report(make_report(accounts=accts), Policy())["exposure"]
    rows = e["accounts"]

    npa_rows = [a for a in rows if a["non_performing"]]
    assert len(npa_rows) == 2
    assert all(a["counted_in_emi"] is False for a in npa_rows)
    assert all(a["emi_contribution"] == 0 for a in npa_rows)
    # The informational figure is still there; it is simply not the one to sum.
    assert all(a["scheduled_payment"] for a in npa_rows)

    assert sum(a["emi_contribution"] for a in rows) == e["scheduled_monthly_payments"]
    assert e["scheduled_monthly_payments"] == 71_067


# ------------------------------------------------------- report freshness --
def test_stale_report_closes_the_gate():
    r = assess_credit_report(make_report(band="AA", report_date=_report_date(16)), Policy())
    assert r["gate"]["statement_parsing_allowed"] is False
    assert r["gate"]["decision"] == "decline"
    assert r["gate"]["report_is_current"] is False
    assert r["gate"]["report_age_days"] == 16


def test_report_at_the_boundary_is_still_current():
    r = assess_credit_report(make_report(band="AA", report_date=_report_date(15)), Policy())
    assert r["gate"]["statement_parsing_allowed"] is True
    assert r["gate"]["report_is_current"] is True


def test_unreadable_report_date_refers_even_on_a_clearing_band():
    r = assess_credit_report(make_report(band="AA", report_date="not a date"), Policy())
    assert r["gate"]["statement_parsing_allowed"] is False
    assert r["gate"]["decision"] == "refer"
    assert r["gate"]["report_is_current"] is None


def test_stale_report_reason_is_appended_not_lost_when_band_already_declines():
    r = assess_credit_report(make_report(band="JJ", report_date=_report_date(30)), Policy())
    assert r["gate"]["statement_parsing_allowed"] is False
    assert r["gate"]["decision"] == "decline"
    assert "Additionally" in r["gate"]["reason"]


def test_freshness_limit_is_configurable():
    lenient = Policy(); lenient.crb_max_age_days = 60
    r = assess_credit_report(make_report(band="AA", report_date=_report_date(30)), lenient)
    assert r["gate"]["statement_parsing_allowed"] is True


# --------------------------------------------------------- history cutoff --
def test_npa_before_the_cutoff_does_not_block():
    old = account(index=1, classification="LOSS", is_non_performing=True,
                  listing_date="15-Jun-2022", balance_amount=500_000)
    r = assess_credit_report(make_report(band="AA", accounts=[old]), Policy())
    codes = {f["code"] for f in r["findings"]}
    assert "NPA" not in codes
    assert "NPA_PRE_CUTOFF" in codes
    # The money owed is still shown - only the finding is suppressed.
    assert r["exposure"]["total_open_balance"] == 500_000


def test_npa_on_or_after_the_cutoff_still_blocks():
    recent = account(index=1, classification="LOSS", is_non_performing=True,
                     listing_date="15-Jun-2024", balance_amount=500_000)
    r = assess_credit_report(make_report(band="AA", accounts=[recent]), Policy())
    codes = {f["code"] for f in r["findings"] if f["severity"] == "blocking"}
    assert "NPA" in codes


def test_npa_with_unreadable_listing_date_is_not_suppressed():
    unknown = account(index=1, classification="LOSS", is_non_performing=True,
                      listing_date=None, balance_amount=500_000)
    r = assess_credit_report(make_report(band="AA", accounts=[unknown]), Policy())
    codes = {f["code"] for f in r["findings"] if f["severity"] == "blocking"}
    assert "NPA" in codes


def test_arrears_before_the_cutoff_are_excluded_from_the_finding():
    old = account(index=1, listing_date="01-Jan-2023", arrears_amount=900_000)
    r = assess_credit_report(make_report(band="AA", accounts=[old]), Policy())
    codes = {f["code"] for f in r["findings"]}
    assert "ARREARS" not in codes
    assert "ARREARS_PRE_CUTOFF" in codes


def test_history_cutoff_is_configurable():
    strict = Policy(); strict.crb_history_cutoff = "2025-01-01"
    mid_2024 = account(index=1, classification="LOSS", is_non_performing=True,
                       listing_date="15-Jun-2024", balance_amount=500_000)
    r = assess_credit_report(make_report(band="AA", accounts=[mid_2024]), strict)
    codes = {f["code"] for f in r["findings"]}
    assert "NPA" not in codes
    assert "NPA_PRE_CUTOFF" in codes


# ------------------------------------------------- recent repayment history
def test_recent_repayment_behavior_counts_last_12_months():
    history = [{"month": f"m{i}", "status": "0"} for i in range(6)] + \
        [{"month": f"m{i}", "status": "15"} for i in range(6, 9)] + \
        [{"month": f"m{i}", "status": "0"} for i in range(9, 24)]
    a = account(index=1, payment_history=history)
    e = assess_credit_report(make_report(band="AA", accounts=[a]), Policy())["exposure"]
    behavior = e["accounts"][0]["recent_repayment_behavior"]
    assert behavior["months_recorded"] == 12
    assert behavior["months_delayed"] == 3
    assert behavior["months_on_time"] == 9
    assert behavior["worst_days_late"] == 15
    assert len(behavior["delayed_detail"]) == 3


# ------------------------------------------------------ adverse records ----
def _cheque(date_bounced="08-Aug-2025"):
    return {"trade_sector": "Bank Sector Bureau", "cheque_date": date_bounced,
            "cheque_no": "00000006", "amount": 1_250_000.0,
            "date_bounced": date_bounced, "reason": "NSF(No Sufficient Funds)"}


def test_bounced_cheque_declines_even_a_top_band():
    rep = make_report(band="AA")
    rep.bounced_cheques = [_cheque()]
    g = assess_credit_report(rep, Policy())["gate"]
    assert g["statement_parsing_allowed"] is False
    assert g["decision"] == "decline" and g["recommendation"] == "decline"
    assert g["adverse_records"]


def test_fraud_case_declines():
    rep = make_report(band="AA", summary={"fraudulent_cases": {"your_sector": 0, "other_sectors": 1}})
    g = assess_credit_report(rep, Policy())["gate"]
    assert g["statement_parsing_allowed"] is False and g["decision"] == "decline"


def test_bounced_cheque_before_history_cutoff_is_ignored():
    rep = make_report(band="AA", summary={"bounced_cheques": {"your_sector": 0, "other_sectors": 1}})
    rep.bounced_cheques = [_cheque("10-Mar-2023")]
    out = assess_credit_report(rep, Policy())
    assert out["gate"]["statement_parsing_allowed"] is True
    assert out["gate"]["adverse_records"] == []
    codes = {f["code"] for f in out["findings"]}
    assert "BOUNCED_CHEQUE" not in codes and "BOUNCED_CHEQUE_PRE_CUTOFF" in codes


def test_undated_bounced_cheque_count_still_declines():
    rep = make_report(band="AA", summary={"bounced_cheques": {"your_sector": 1, "other_sectors": 0}})
    assert assess_credit_report(rep, Policy())["gate"]["decision"] == "decline"


def test_pre_cutoff_non_performing_account_raises_no_blocking_finding():
    """A facility listed before the history cutoff is old history: it must not
    reach the NPA, unserviced-debt or score-divergence findings, though its
    balance stays in the exposure."""
    old_npa = account(classification="DOUBTFUL", is_non_performing=True, listing_date="06-Mar-2023",
                      balance_amount=530_000, arrears_amount=900_000,
                      days_in_arrears=120, days_in_arrears_reported=True)
    out = assess_credit_report(make_report(band="AA", accounts=[old_npa]), Policy())
    blocking = [f["code"] for f in out["findings"] if f["severity"] == "blocking"]
    assert blocking == []
    assert out["gate"]["recommendation"] == "proceed"
    codes = {f["code"] for f in out["findings"]}
    assert {"NPA_PRE_CUTOFF", "UNSERVICED_OBLIGATION_PRE_CUTOFF"} <= codes
    assert out["exposure"]["total_open_balance"] == 530_000


def test_post_cutoff_non_performing_account_still_blocks():
    npa = account(classification="DOUBTFUL", is_non_performing=True, listing_date="06-Mar-2025",
                  balance_amount=530_000, arrears_amount=900_000)
    out = assess_credit_report(make_report(band="AA", accounts=[npa]), Policy())
    codes = {f["code"] for f in out["findings"] if f["severity"] == "blocking"}
    assert {"NPA", "UNSERVICED_OBLIGATION", "SCORE_DIVERGENCE"} <= codes


def test_pre_cutoff_performing_account_classification_still_warns():
    """The history cutoff covers NPAs and arrears only."""
    watch = account(classification="WATCH", listing_date="15-Jun-2022")
    out = assess_credit_report(make_report(band="AA", accounts=[watch]), Policy())
    assert "ADVERSE_CLASSIFICATION" in {f["code"] for f in out["findings"]}
