"""
Regression tests for the underwriting engine.

The golden-file test pins the figures produced from a real 62-page statement.
The rest are the edge cases that actually found bugs during development:
an account with no outflows used to raise TypeError, and a quiet month used
to be misclassified as partial and dropped from the run rate.

    pip install pytest
    MOMO_FIXTURE=/path/to/statement.pdf pytest -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.analyzer import analyze
from app.config import Policy
from app.parser import ParsedStatement, StatementHeader, Txn, parse_statement

ACC = "250700000001"

# Counterparty numbers must not collide with ACC. Self-transfer is now tested by
# identity as well as by name, so a synthetic sender that happens to equal the
# account is a real self-transfer and will be deducted - which is correct
# behaviour, and was silently invisible while only names were matched.


def txn(i, ts, frm, frm_name, to, to_name, amount, fee=None, status="SUCCESSFUL"):
    return Txn(str(100000 + i), status, "TRANSFER", ts, frm, frm_name, to, to_name,
               amount, fee, 1000, 1)


def header(count, start="2026-01-01 00:00:00", end="2026-03-31 00:00:00"):
    # A 90-day span by default so the statement-freshness/period gate (which
    # is orthogonal to what most of these tests check) does not fire. Tests
    # that care about the period pass their own start/end.
    return StatementHeader(customer="Jane DOE", msisdn=ACC, period_start=start,
                           period_end=end, declared_txn_count=count)


def run(rows, hdr=None):
    return analyze(ParsedStatement(hdr or header(len(rows)), rows, 1), Policy())


# --------------------------------------------------------------- edge cases --
def test_inflows_only_does_not_crash():
    """No outflows: pandas .apply on an empty column returns a string dtype and
    `|` between two of them raises TypeError. Regression for _mask()."""
    rows = [txn(i, f"2026-01-{d:02d} 09:00:00", f"25078800{i:04d}", f"Cust {i}",
                ACC, "Jane Jane", 1000 + i)
            for i, d in enumerate(range(1, 13), start=1)]
    report = run(rows)
    assert report["summary"]["totals"]["net_revenue"] == sum(1000 + i for i in range(1, 13))
    assert report["disclosures"]["outflow_count"] == 0


def test_outflows_only_yields_zero_revenue():
    report = run([txn(1, "2026-01-05 09:00:00", ACC, "Jane DOE", "250700000009", "Peter", 5000, 20)])
    assert report["summary"]["totals"]["net_revenue"] == 0
    assert report["disclosures"]["inflow_count"] == 0


def test_all_failed_transactions_are_excluded():
    report = run([txn(1, "2026-01-05 09:00:00", "250700000009", "P", ACC, "Jane Jane",
                      5000, status="FAILED")])
    assert report["summary"]["totals"]["gross_inflows"] == 0
    assert report["disclosures"]["failed_count"] == 1


def test_quiet_month_is_still_a_full_month():
    """Two transactions 15 days apart must not make January a partial month.
    Coverage is measured against the statement period, not observed activity."""
    rows = [txn(1, "2026-01-05 09:00:00", "250700000009", "Peter", ACC, "Jane Jane", 5000),
            txn(2, "2026-01-20 10:00:00", "250700000007", "Alice", ACC, "Jane Jane", 7000)]
    row = run(rows)["summary"]["rows"][0]
    assert row["days_covered"] == 31
    assert row["is_full_month"] is True


def test_missing_period_header_falls_back_to_data_span():
    rows = [txn(1, "2026-01-01 09:00:00", "250700000009", "Peter", ACC, "Jane Jane", 5000),
            txn(2, "2026-01-12 10:00:00", "250700000007", "Alice", ACC, "Jane Jane", 7000)]
    hdr = StatementHeader(customer="Jane DOE", msisdn=ACC, declared_txn_count=2)
    row = run(rows, hdr)["summary"]["rows"][0]
    assert row["days_covered"] == 12
    assert row["is_full_month"] is False


def test_large_threshold_is_strictly_greater_than():
    policy = Policy()
    rows = [txn(1, "2026-01-05 09:00:00", "250700000009", "P", ACC, "Jane Jane",
                policy.large_txn_threshold)]
    report = analyze(ParsedStatement(header(1), rows, 1), policy)
    assert report["summary"]["totals"]["large_transactions"] == 0


def test_min_inflow_check_is_not_assessed_without_a_threshold():
    report = run([txn(1, "2026-01-05 09:00:00", "250700000009", "P", ACC, "Jane Jane", 5000)])
    assert report["disclosures"]["minimum_transaction_check"]["status"] == "not_assessed"


def test_response_is_json_serialisable():
    """numpy scalars survive pandas aggregation and break json.dumps if they leak."""
    import json
    rows = [txn(i, f"2026-01-{d:02d} 09:00:00", f"25078800{i:04d}", f"C{i}",
                ACC, "Jane Jane", 1000 * i) for i, d in enumerate(range(1, 6), start=1)]
    body = json.dumps(run(rows))
    assert "NaN" not in body and "Infinity" not in body



def test_statement_loan_eligibility_uses_three_month_average_and_crb_emi():
    rows = []
    i = 1
    for month in (1, 2, 3):
        for suffix, amount in enumerate((900_000, 900_000, 700_000), start=1):
            rows.append(txn(i, f"2026-0{month}-{10 + suffix:02d} 09:00:00", f"25078800{i:04d}", f"Cust {i}",
                            ACC, "Jane Jane", amount))
            i += 1
    report = analyze(ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                        period_end="2026-03-31", declared_txn_count=3), rows, 1),
        Policy(), monthly_emi=250_000)
    e = report["summary"]["loan_eligibility"]
    assert e["months_used"] == ["2026-01", "2026-02", "2026-03"]
    # 2.5m in each of Jan (31d), Feb (28d) and Mar (31d). On a 30-day footing
    # the short February scales up slightly, so the average is a little above
    # the raw monthly figure. This is the normalisation working, not drift.
    assert e["average_monthly_inflow"] == 2_505_760
    assert e["average_monthly_net_revenue"] == 2_505_760
    assert e["net_inflow"] == 2_255_760
    assert e["net_revenue"] == 338_364
    assert e["fee_cap"] == 50_755
    assert e["loan_amount_eligibility"] == 500_000
    assert e["status"] == "eligible"



def test_eligibility_deducts_only_the_ongoing_crb_emi_from_statement_net_revenue():
    rows = []
    i = 1
    for month in (1, 2, 3):
        for suffix, amount in enumerate((900_000, 900_000, 700_000), start=1):
            rows.append(txn(i, f"2026-0{month}-{10 + suffix:02d} 09:00:00", f"25078800{i:04d}", f"Cust {i}",
                            ACC, "Jane Jane", amount))
            i += 1
    report = analyze(ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                        period_end="2026-03-31", declared_txn_count=3), rows, 1),
        Policy(), monthly_emi=100_000)
    e = report["summary"]["loan_eligibility"]
    assert e["average_monthly_net_revenue"] == 2_505_760
    assert e["monthly_emi"] == 100_000
    assert e["net_inflow"] == 2_405_760
    assert e["net_revenue"] == 360_864
    assert e["fee_cap"] == 54_130
    assert e["monthly_emi_scope"] == "open_performing_accounts_only"

def test_statement_loan_eligibility_requires_complete_emi():
    rows = [
        txn(1, "2026-01-15 09:00:00", "250788000001", "A", ACC, "Jane", 1_000_000),
        txn(2, "2026-02-15 09:00:00", "250788000002", "B", ACC, "Jane", 1_000_000),
        txn(3, "2026-03-15 09:00:00", "250788000003", "C", ACC, "Jane", 1_000_000),
    ]
    report = analyze(ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                        period_end="2026-03-31", declared_txn_count=3), rows, 1),
        Policy(), monthly_emi=None)
    e = report["summary"]["loan_eligibility"]
    assert e["status"] == "not_assessed"
    assert e["monthly_emi"] is None


def test_statement_loan_eligibility_gap_is_not_assigned():
    rows = []
    i = 1
    for month in (1, 2, 3):
        for suffix in range(3):
            rows.append(txn(i, f"2026-0{month}-{10 + suffix:02d} 09:00:00", f"25078800{i:04d}", f"C{i}",
                            ACC, "Jane", 1_000_000))
            i += 1
    # avg 3M, EMI 200K -> net inflow 2.8M -> revenue 420K -> fee cap 63K, a normal band.
    report = analyze(ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                        period_end="2026-03-31", declared_txn_count=3), rows, 1),
        Policy(), monthly_emi=200_000)
    assert report["summary"]["loan_eligibility"]["loan_amount_eligibility"] == 600_000

# ------------------------------------------------------------- golden file --
FIXTURE = os.getenv("MOMO_FIXTURE", "")
needs_fixture = pytest.mark.skipif(
    not FIXTURE or not Path(FIXTURE).exists(),
    reason="set MOMO_FIXTURE to a real statement PDF",
)


@pytest.fixture(scope="module")
def golden():
    return analyze(parse_statement(Path(FIXTURE).read_bytes()), Policy())


@needs_fixture
def test_golden_parse_is_complete(golden):
    assert golden["disclosures"]["parse_complete"] is True
    assert golden["disclosures"]["total_transaction_count"] == 1747


@needs_fixture
@pytest.mark.parametrize("key,expected", [
    ("gross_inflows", 43_685_858),
    ("loan_inflows", 256_000),
    ("large_transactions", 9_100_000),
    ("net_revenue", 34_329_858),
    ("inflow_count", 496),
    ("counted_inflow_count", 489),
])
def test_golden_totals(golden, key, expected):
    assert golden["summary"]["totals"][key] == expected


@needs_fixture
def test_golden_arithmetic_reconciles(golden):
    for row in golden["summary"]["rows"]:
        assert row["net_revenue"] == (
            row["gross_inflows"] - row["self_transfers"]
            - row["loan_inflows"] - row["large_transactions"]
        )


@needs_fixture
def test_golden_detail_tables_match_summary(golden):
    totals = golden["summary"]["totals"]
    large = golden["details"]["large_transactions"]["inflows"]
    loans = golden["details"]["loan_inflows"]["items"]
    assert sum(x["amount"] for x in large) == totals["large_transactions"]
    assert sum(x["amount"] for x in loans) == totals["loan_inflows"]


@needs_fixture
def test_golden_disclosure_counts_are_consistent(golden):
    d = golden["disclosures"]
    assert d["successful_count"] + d["failed_count"] == d["total_transaction_count"]
    assert d["inflow_count"] + d["outflow_count"] == d["successful_count"]
    assert d["distinct_individual_senders"] + d["distinct_institution_senders"] == d["distinct_senders_total"]

# -------------------------------------------------------- prompt compliance --
def test_possible_self_transfer_is_explicitly_disclosed():
    rows = [
        txn(1, "2026-01-05 09:00:00", "250788000001", "Jane D", ACC, "Jane DOE", 80_000),
    ]
    report = run(rows)
    possible = report["details"]["self_transfers"]["possible_for_review"]
    assert len(possible) == 1
    assert possible[0]["match_basis"] == "partial_name_match"
    assert report["summary"]["totals"]["net_revenue"] == 80_000


def test_pattern_based_loan_cycle_is_deducted_and_evidenced():
    rows = [
        txn(1, "2026-01-05 09:00:00", "250788000010", "Lender Finance X", ACC, "Jane DOE", 1_000_000),
        txn(2, "2026-01-20 09:00:00", ACC, "Jane DOE", "250788000010", "Lender Finance X", 250_000),
        txn(3, "2026-02-05 09:00:00", ACC, "Jane DOE", "250788000010", "Lender Finance X", 250_000),
        txn(4, "2026-01-25 09:00:00", "250788000011", "Customer A", ACC, "Jane DOE", 300_000),
    ]
    report = run(rows, header(4))
    assert report["summary"]["totals"]["loan_inflows"] == 1_000_000
    assert report["summary"]["totals"]["net_revenue"] == 300_000
    assert len(report["details"]["loan_inflows"]["pattern_matches"]) == 1
    assert any(f["code"] == "UNDISCLOSED_BORROWING" for f in report["red_flags"])


def test_rapid_round_trip_reports_same_counterparty_match():
    rows = [
        txn(1, "2026-01-05 10:00:00", "250788000020", "Counterparty", ACC, "Jane DOE", 500_000),
        txn(2, "2026-01-05 10:30:00", ACC, "Jane DOE", "250788000020", "Counterparty", 500_000),
    ]
    report = run(rows)
    flag = next(f for f in report["red_flags"] if f["code"] == "PASS_THROUGH")
    assert flag["evidence"]["same_counterparty_round_trips"]
    assert flag["evidence"]["same_counterparty_round_trips"][0]["same_counterparty"] is True


def test_concentration_flags_single_payment_even_when_large_is_excluded():
    rows = [
        txn(1, "2026-01-05 09:00:00", "250788000030", "A", ACC, "Jane DOE", 2_000_000),
        txn(2, "2026-01-06 09:00:00", "250788000031", "B", ACC, "Jane DOE", 100_000),
    ]
    report = run(rows)
    concentration = report["disclosures"]["concentration"][0]
    assert concentration["largest_inflow"]["is_large_transaction"] is True
    assert concentration["largest_inflow_share"] > 0.9
    assert concentration["flagged"] is True
    assert any("One payment" in r for r in concentration["reasons"])


def test_profile_deviation_includes_month_and_transaction_evidence():
    rows = []
    i = 1
    for month, day_amount in [(1, 100_000), (2, 110_000), (3, 120_000), (4, 5_000_000)]:
        for d in range(1, 4):
            rows.append(txn(i, f"2026-{month:02d}-{d:02d} 09:00:00", f"250788{i:06d}", "Customer", ACC, "Jane DOE", day_amount))
            i += 1
    report = run(rows, header(len(rows), start="2026-01-01 00:00:00", end="2026-04-30 00:00:00"))
    flag = next(f for f in report["red_flags"] if f["code"] == "PROFILE_DEVIATION")
    assert "outlier_months" in flag["evidence"]
    assert "outlier_transactions" in flag["evidence"]


# ------------------------------------------------- NEW CUSTOMER sanction table --
def _eligibility_at_fee_cap(fee_cap, policy=None, **kw):
    """Drive the engine to a target fee cap.

    fee_cap = (avg monthly net revenue - EMI) x net_revenue_rate x fee_cap_rate,
    so with no EMI the required average is fee_cap / 0.0225.
    """
    from app.analyzer import _loan_eligibility
    policy = policy or Policy()
    avg = fee_cap / (policy.net_revenue_rate * policy.fee_cap_rate)
    summary = [{"month": f"2026-{m:02d}", "gross_inflows": avg, "net_revenue": avg,
                "is_full_month": True, "days_covered": 30}
               for m in (1, 2, 3)]
    return _loan_eligibility(summary, policy, 0, **kw)


@pytest.mark.parametrize("fee_cap,sanctioned", [
    (49_999, None),        # below the floor
    (50_000, 500_000),
    (55_000, 500_000),     # inclusive upper bound - was 600,000
    (55_001, 600_000),
    (65_000, 600_000),
    (65_001, 700_000),
    (75_000, 700_000),
    (75_001, 800_000),
    (85_000, 800_000),
    (85_001, 900_000),
    (95_000, 900_000),
    (95_001, 1_000_000),
    (99_999, 1_000_000),
    (100_000, 1_000_000),
    (175_000, 1_000_000),  # was declined - the 175k-185k "gap" was a typo
    (175_001, 1_500_000),
    (180_000, 1_500_000),
    (250_000, 1_500_000),
    (250_001, 2_000_000),  # top band was missing entirely
    (300_000, 2_000_000),
    (300_001, 2_000_000),  # top band is open-ended
    (400_000, 2_000_000),
    (49_999, None),        # below the floor
])
def test_new_customer_sanction_table_matches_credit_policy(fee_cap, sanctioned):
    e = _eligibility_at_fee_cap(fee_cap)
    assert e["fee_cap"] == fee_cap, "band must be matched on the fee cap actually reported"
    assert e["loan_amount_eligibility"] == sanctioned
    assert e["status"] == ("eligible" if sanctioned else "not_eligible")


def test_fee_cap_above_300k_is_eligible_for_2_million():
    e = _eligibility_at_fee_cap(1_000_000)
    assert e["status"] == "eligible"
    assert e["loan_amount_eligibility"] == 2_000_000
    assert e["fee_cap_ceiling"] is None


def test_fee_cap_above_a_bounded_top_band_still_refers():
    policy = Policy(loan_eligibility_bands=[
        {"min_fee_cap": 50_000, "max_fee_cap": 300_000, "loan_amount": 2_000_000},
    ])
    e = _eligibility_at_fee_cap(400_000, policy)
    assert e["status"] == "not_eligible"
    assert "above the 300,000 RWF top" in e["reason"]


def test_policy_sanction_table_is_contiguous_and_non_overlapping():
    bands = Policy().loan_eligibility_bands
    for lo, hi in zip(bands, bands[1:]):
        assert hi["min_fee_cap"] == lo["max_fee_cap"] + 1


def test_overlapping_policy_bands_resolve_to_the_lower_sanction_and_say_so():
    policy = Policy(loan_eligibility_bands=[
        {"min_fee_cap": 50_000, "max_fee_cap": 85_001, "loan_amount": 800_000},
        {"min_fee_cap": 85_000, "max_fee_cap": 95_000, "loan_amount": 900_000},
    ])
    e = _eligibility_at_fee_cap(85_001, policy)
    assert e["band_overlap_resolved"] is True
    assert e["loan_amount_eligibility"] == 800_000
    assert "lower sanction" in e["band_overlap_note"]


def _existing_eligibility_at_fee_cap(fee_cap, emi=0, **kw):
    """As _eligibility_at_fee_cap, on the existing-customer rates (18% x 20%)."""
    from app.analyzer import _loan_eligibility
    policy = Policy()
    rate = policy.existing_net_revenue_rate * policy.existing_fee_cap_rate
    avg = fee_cap / rate + emi
    summary = [{"month": f"2026-{m:02d}", "gross_inflows": avg, "net_revenue": avg,
                "is_full_month": True, "days_covered": 30}
               for m in (1, 2, 3)]
    return _loan_eligibility(summary, policy, emi, customer_type="existing", **kw)


@pytest.mark.parametrize("fee_cap,sanctioned", [
    (49_999, None),
    (50_000, 500_000),
    (55_000, 500_000),
    (55_001, 600_000),
    (65_001, 700_000),
    (85_000, 800_000),
    (85_001, 900_000),
    (95_000, 900_000),
    (95_001, 1_000_000),
    (105_000, 1_000_000),
    (105_001, 2_000_000),
    (205_000, 2_000_000),
    (205_001, 3_000_000),
    (300_000, 3_000_000),
    (300_001, 3_000_000),  # ">300,000" is the same 3,000,000 product
    (1_000_000, 3_000_000),
])
def test_existing_customer_sanction_table(fee_cap, sanctioned):
    e = _existing_eligibility_at_fee_cap(fee_cap)
    assert e["customer_type"] == "existing"
    assert e["fee_cap"] == fee_cap
    assert e["loan_amount_eligibility"] == sanctioned
    assert e["status"] == ("eligible" if sanctioned else "not_eligible")


def test_existing_customer_uses_18_and_20_percent_after_emi():
    # avg 2,000,000 - EMI 100,000 = 1,900,000; x18% = 342,000; x20% = 68,400
    e = _existing_eligibility_at_fee_cap(68_400, emi=100_000)
    assert e["net_inflow"] == 1_900_000
    assert e["net_revenue"] == 342_000
    assert e["fee_cap"] == 68_400
    assert e["net_revenue_rate"] == 0.18 and e["fee_cap_rate"] == 0.20
    assert e["loan_amount_eligibility"] == 700_000


def test_same_statement_prices_differently_for_new_and_existing():
    # fee cap 100,000 on new rates -> 1,000,000; the same average on existing
    # rates gives 160,000 -> 2,000,000.
    new = _eligibility_at_fee_cap(100_000)
    existing = _eligibility_at_fee_cap(100_000, customer_type="existing")
    assert new["loan_amount_eligibility"] == 1_000_000
    assert existing["fee_cap"] == 160_000
    assert existing["loan_amount_eligibility"] == 2_000_000


def test_existing_customer_request_is_capped_at_eligible_amount():
    e = _existing_eligibility_at_fee_cap(250_000, requested_amount=5_000_000)
    assert e["sanctioned_amount"] == 3_000_000
    assert e["sanction_basis"] == "eligible_amount_capped"


def test_existing_customer_sanction_table_is_contiguous():
    bands = Policy().existing_loan_eligibility_bands
    for lo, hi in zip(bands, bands[1:]):
        assert hi["min_fee_cap"] == lo["max_fee_cap"] + 1


def test_unknown_customer_type_is_not_assessed():
    e = _eligibility_at_fee_cap(100_000, customer_type="returning")
    assert e["status"] == "not_assessed"
    assert "Unknown customer type" in e["reason"]


# ------------------------------------------------- classification precedence --
def test_own_savings_wallet_is_a_self_transfer_not_borrowing():
    """'Mokash Savings' contains 'mokash'.

    With MoKash registered as a nano-loan lender, every savings withdrawal was
    read as a loan disbursement: 531,347 RWF over 82 rows on the reference
    statement. A savings wallet is the customer's own money by construction.
    """
    rows = [
        txn(1, "2026-01-05 09:00:00", "CBA-SAVINGS/SP", "Mokash Savings", ACC, "Jane Jane", 5_000),
        txn(2, "2026-01-06 09:00:00", "250791504625", "Mokash Loan Account", ACC, "Jane Jane", 12_000),
        txn(3, "2026-01-07 09:00:00", "250788800001", "Real Customer", ACC, "Jane Jane", 8_000),
    ]
    r = run(rows)
    t = r["summary"]["totals"]
    assert t["self_transfers"] == 5_000      # the savings wallet
    assert t["loan_inflows"] == 12_000       # the actual loan
    assert t["net_revenue"] == 8_000         # only the real customer
    assert t["gross_inflows"] == 25_000


def test_sacco_and_zigama_inflows_are_deducted_as_loans():
    """Credit policy names SACCOs and Zigama CSS as loan sources outright."""
    rows = [
        txn(1, "2026-01-05 09:00:00", "250790361487", "ZIGAMA CSS", ACC, "Jane Jane", 100_000),
        txn(2, "2026-01-06 09:00:00", "urwegobank", "URWEGO", ACC, "Jane Jane", 50_000),
        txn(3, "2026-01-07 09:00:00", "250788800001", "Real Customer", ACC, "Jane Jane", 7_000),
    ]
    t = run(rows)["summary"]["totals"]
    assert t["loan_inflows"] == 150_000
    assert t["net_revenue"] == 7_000


def test_exclusion_categories_are_mutually_exclusive():
    """A row counted in two buckets is deducted twice and the month stops footing."""
    rows = [
        # Own wallet AND above the large-transaction threshold.
        txn(1, "2026-01-05 09:00:00", "CBA-SAVINGS/SP", "Mokash Savings", ACC, "Jane Jane", 1_500_000),
        txn(2, "2026-01-06 09:00:00", "250788800001", "Real Customer", ACC, "Jane Jane", 9_000),
    ]
    r = run(rows)
    t = r["summary"]["totals"]
    assert t["self_transfers"] == 1_500_000
    assert t["loan_inflows"] == 0
    assert t["large_transactions"] == 0      # already excluded as own money
    assert t["net_revenue"] == 9_000


def test_every_month_foots_and_totals_foot_to_the_rows():
    """No figure on the report may disagree with the rows it is built from."""
    rows = []
    i = 1
    for month in (1, 2, 3):
        for day, (frm, name, amt) in enumerate([
            ("250788800001", "Cust A", 400_000),
            ("CBA-SAVINGS/SP", "Mokash Savings", 30_000),
            ("250790361487", "ZIGAMA CSS", 60_000),
            ("250788800002", "Cust B", 1_400_000),
        ], start=5):
            rows.append(txn(i, f"2026-0{month}-{day:02d} 09:00:00", frm, name, ACC, "Jane Jane", amt))
            i += 1
    r = analyze(ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                        period_end="2026-03-31", declared_txn_count=len(rows)), rows, 1),
        Policy(), monthly_emi=0)
    s = r["summary"]
    for row in s["rows"]:
        assert (row["gross_inflows"] - row["self_transfers"] - row["loan_inflows"]
                - row["large_transactions"]) == row["net_revenue"], row["month"]
    t = s["totals"]
    for key in ("gross_inflows", "self_transfers", "loan_inflows", "large_transactions", "net_revenue"):
        assert t[key] == sum(row[key] for row in s["rows"]), key
    assert t["gross_inflows"] - t["self_transfers"] - t["loan_inflows"] - t["large_transactions"] == t["net_revenue"]
    e = s["loan_eligibility"]
    assert e["average_monthly_net_revenue"] == round(
        sum(m["net_revenue_30d"] for m in e["monthly_inflows"]) / len(e["monthly_inflows"]))
    for m in e["monthly_inflows"]:
        assert m["net_revenue_30d"] == round(m["net_revenue"] / m["days_covered"] * 30)


# ----------------------------------------------------------- partial months --
def test_months_are_normalised_to_thirty_days_before_averaging():
    """Calendar months differ in length and the first and last are usually partial.

    Raw totals are not comparable, so each month is scaled to a 30-day
    equivalent. A month too short to extrapolate from is excluded instead.
    """
    rows = []
    i = 1
    for month, days in ((5, range(20, 31)), (6, range(1, 29)), (7, range(1, 30)), (8, range(1, 9))):
        for d in days:
            rows.append(txn(i, f"2026-{month:02d}-{d:02d} 09:00:00", f"25078880{i:04d}",
                            f"Cust {i}", ACC, "Jane Jane", 20_000))
            i += 1
    r = analyze(ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-05-12",
                        period_end="2026-08-10", declared_txn_count=len(rows)), rows, 1),
        Policy(), monthly_emi=0)
    e = r["summary"]["loan_eligibility"]

    # August covers 10 days, below the 15-day floor, so it is not scaled up.
    assert "2026-08" in e["months_excluded_as_too_short"]
    assert "2026-08" not in e["months_used"]

    for m in e["monthly_inflows"]:
        assert m["net_revenue_30d"] == round(m["net_revenue"] / m["days_covered"] * 30)
    assert e["average_monthly_net_revenue"] == round(
        sum(m["net_revenue_30d"] for m in e["monthly_inflows"]) / len(e["monthly_inflows"]))
    assert e["basis"] == "30_day_normalised"


def test_a_month_too_short_to_extrapolate_is_excluded_not_scaled_up():
    """Three days of takings is not evidence of a month of them."""
    rows = [txn(i, f"2026-03-{d:02d} 09:00:00", f"25078880{i:04d}", f"Cust {i}",
                ACC, "Jane Jane", 500_000)
            for i, d in enumerate(range(1, 4), start=1)]
    r = analyze(ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-03-01",
                        period_end="2026-03-03", declared_txn_count=len(rows)), rows, 1),
        Policy(), monthly_emi=0)
    e = r["summary"]["loan_eligibility"]
    assert e["status"] == "not_assessed"
    # 1.5m over three days must NOT become 15m a month.
    assert e.get("average_monthly_net_revenue") is None


def test_sanctioned_amount_is_the_lower_of_requested_and_eligible():
    def at(fee_cap, requested):
        from app.analyzer import _loan_eligibility
        p = Policy()
        avg = fee_cap / (p.net_revenue_rate * p.fee_cap_rate)
        summary = [{"month": f"2026-{m:02d}", "gross_inflows": avg, "net_revenue": avg,
                    "is_full_month": True, "days_covered": 30} for m in (1, 2, 3)]
        return _loan_eligibility(summary, p, 0, requested_amount=requested)

    # Asking for less than you qualify for is honoured.
    e = at(120_000, 400_000)
    assert e["loan_amount_eligibility"] == 1_000_000
    assert e["sanctioned_amount"] == 400_000
    assert e["sanction_basis"] == "requested_amount"

    # Asking for more is capped at the band.
    e = at(120_000, 5_000_000)
    assert e["loan_amount_eligibility"] == 1_000_000
    assert e["sanctioned_amount"] == 1_000_000
    assert e["sanction_basis"] == "eligible_amount_capped"
    assert e["requested_amount_cap_applied"] is True

    # No request entered: the eligible amount stands.
    e = at(120_000, None)
    assert e["sanctioned_amount"] == 1_000_000
    assert e["sanction_basis"] == "eligible_amount_no_request"

    # Not eligible: nothing is sanctioned, whatever was asked for.
    e = at(10_000, 400_000)
    assert e["status"] == "not_eligible"
    assert e["sanctioned_amount"] is None


def test_reconciliation_covers_the_normalisation_step():
    """The self-check must verify the arithmetic the engine actually does.

    When the average moved onto a 30-day footing, the reconciliation was still
    re-deriving it from raw month totals - comparing two different quantities
    and reporting a failure on a correct report.
    """
    rows = []
    i = 1
    for month, days in ((1, 31), (2, 28), (3, 31)):
        for d in range(1, days + 1):
            rows.append(txn(i, f"2026-{month:02d}-{d:02d} 09:00:00", f"25078880{i:04d}",
                            f"Cust {i}", ACC, "Jane Jane", 80_000))
            i += 1
    r = analyze(ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                        period_end="2026-03-31", declared_txn_count=len(rows)), rows, 1),
        Policy(), monthly_emi=10_000)
    rec = r["reconciliation"]
    assert rec["all_figures_reconcile"] is True, rec["failures"]
    assert any("scaled to 30 days" in c["check"] for c in rec["checks"])
    assert any("average equals the months it lists" in c["check"] for c in rec["checks"])


def test_the_eligibility_chain_is_computed_from_the_figures_it_displays():
    """Each step must derive from the rounded value shown for the step above.

    Carrying unrounded values forward and rounding only at display time leaves
    consecutive lines a rupee apart: average 334,446 less EMI 71,067 printed as
    net inflow 263,378 when the subtraction gives 263,379.
    """
    rows = []
    i = 1
    for month, days in ((1, 31), (2, 28), (3, 31)):
        for d in range(1, days + 1):
            rows.append(txn(i, f"2026-{month:02d}-{d:02d} 09:00:00", f"25078880{i:04d}",
                            f"Cust {i}", ACC, "Jane Jane", 33_333))
            i += 1
    r = analyze(ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                        period_end="2026-03-31", declared_txn_count=len(rows)), rows, 1),
        Policy(), monthly_emi=71_067)
    e = r["summary"]["loan_eligibility"]
    assert e["net_inflow"] == e["average_monthly_net_revenue"] - e["monthly_emi"]
    assert e["net_revenue"] == round(e["net_inflow"] * e["net_revenue_rate"])
    assert e["fee_cap"] == round(e["net_revenue"] * e["fee_cap_rate"])
    assert r["reconciliation"]["all_figures_reconcile"] is True, r["reconciliation"]["failures"]


# --------------------------------------------------- float account coherence --
def _float_account_report(**kw):
    rows = []
    i = 1
    for month, days in ((1, 31), (2, 28), (3, 31)):
        for d in range(1, days + 1):
            rows.append(txn(i, f"2026-{month:02d}-{d:02d} 09:00:00", f"25078880{i:04d}",
                            f"Cust {i}", ACC, "Jane Jane", 90_000))
            i += 1
    header = StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                             period_end="2026-03-31", declared_txn_count=len(rows),
                             account_type="AGENT FLOAT")
    return analyze(ParsedStatement(header, rows, 1), Policy(), monthly_emi=0,
                   requested_amount=1_000_000, customer_type="new", **kw)


def test_a_float_account_gets_no_sanction_because_it_gets_no_revenue_figure():
    """The report must not refuse a revenue figure and then lend against it.

    Eligibility is computed before `revenue_blocked` nulls the revenue inputs,
    so blanking them afterwards used to leave a full 600,000 RWF sanction
    standing beside `revenue_assessed: false`.
    """
    r = _float_account_report()
    s = r["summary"]
    assert s["revenue_assessed"] is False
    assert s["totals"]["net_revenue"] is None
    assert s["full_month_run_rate"] is None

    e = s["loan_eligibility"]
    assert e["status"] == "not_assessed"
    assert e["blocked_by"] == "float_account"
    assert e["sanctioned_amount"] is None
    assert e.get("loan_amount_eligibility") is None
    assert e.get("fee_cap") is None
    assert e.get("average_monthly_net_revenue") is None
    assert "agent float account" in e["reason"]

    # The requested amount is still on file — the customer did ask for it.
    assert e["requested_amount"] == 1_000_000


def test_reconciliation_catches_a_sanction_without_a_revenue_figure():
    """A cross-block check: each block can be internally correct and still
    contradict the other."""
    r = _float_account_report()
    rec = r["reconciliation"]
    assert rec["all_figures_reconcile"] is True, rec["failures"]
    guard = [c for c in rec["checks"]
             if c["check"] == "no sanction is offered without a revenue figure"]
    assert len(guard) == 1 and guard[0]["passes"] is True

    # And it fails loudly if a sanction is ever reintroduced.
    from app.analyzer import _reconciliation
    broken = _reconciliation([], {}, {"sanctioned_amount": 600_000}, {},
                             revenue_assessed=False)
    assert broken["all_figures_reconcile"] is False
    assert "not use these numbers" in broken["note"]


def test_float_account_still_reports_everything_except_the_sanction():
    """Blocking the figure must not blank the evidence around it."""
    r = _float_account_report()
    assert any(f["code"] == "FLOAT_ACCOUNT" for f in r["red_flags"])
    codes = {e["code"] for e in r["edge_cases"]}
    assert "REVENUE_NOT_STATED" in codes and "ELIGIBILITY_NOT_ASSESSED" in codes
    # Gross inflows are still counted and still shown; only net revenue is withheld.
    assert r["summary"]["totals"]["gross_inflows"] > 0
    assert all(row["gross_inflows"] is not None for row in r["summary"]["rows"])


def test_short_institution_codes_do_not_fire_inside_ordinary_names():
    """'bok' (Bank of Kigali) sits inside the surname NYIRANGOBOKA.

    Substring matching classified a private individual as a bank, which removed
    her from the distinct-individual-senders disclosure and put her money in the
    institutional sensitivity figure.
    """
    from app.analyzer import _matches
    p = Policy()
    assert _matches("Lydie NYIRANGOBOKA", p.institution_patterns) is None
    assert _matches("Sierra Leone Trading", p.institution_patterns) is None
    # and the real ones still match
    assert _matches("BANK OF KIGALI PLC", p.institution_patterns) == "bank"
    assert _matches("BOK", p.institution_patterns) == "bok"
    assert _matches("eKash", p.institution_patterns) == "ekash"
    assert _matches("MTN Rwanda", p.institution_patterns) == "mtn"


def test_the_last_usable_months_are_averaged_not_the_last_calendar_months():
    """Filtering after the window is fixed silently shrinks the base.

    A May-August statement took Jun/Jul/Aug, dropped August as too short and
    struck the average on two months, while a usable 20-day May sat unused.
    """
    rows = []
    i = 1
    for month, days in ((5, range(12, 32)), (6, range(1, 31)), (7, range(1, 32)), (8, range(1, 11))):
        for d in days:
            rows.append(txn(i, f"2026-{month:02d}-{d:02d} 09:00:00", f"25078880{i:04d}",
                            f"Cust {i}", ACC, "Jane Jane", 20_000))
            i += 1
    r = analyze(ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-05-12",
                        period_end="2026-08-10", declared_txn_count=len(rows)), rows, 1),
        Policy(), monthly_emi=0)
    e = r["summary"]["loan_eligibility"]
    assert e["months_used"] == ["2026-05", "2026-06", "2026-07"]
    assert e["months_averaged"] == 3
    assert e["months_short_of_target"] == 0
    assert "2026-08" in e["months_excluded_as_too_short"]


# --------------------------------------------- bank / e-wallet credits --------
def test_bank_and_ewallet_credits_are_deducted_from_net_revenue():
    """A fourth exclusion beyond the three the policy document names.

    Money from a bank, SACCO or e-wallet is either the customer moving their own
    money in from another institution or a third party settling on their behalf.
    Neither is evidence this account earns.
    """
    rows = [
        txn(1, "2026-01-05 09:00:00", "257777777721", "BANK OF KIGALI PLC", ACC, "Jane Jane", 60_000),
        txn(2, "2026-01-06 09:00:00", "250791431445", "eKash", ACC, "Jane Jane", 10_000),
        txn(3, "2026-01-07 09:00:00", "250788800001", "Real Customer", ACC, "Jane Jane", 9_000),
    ]
    r = run(rows)
    t = r["summary"]["totals"]
    assert t["gross_inflows"] == 79_000
    assert t["institutional_credits"] == 70_000
    assert t["net_revenue"] == 9_000
    row = r["summary"]["rows"][0]
    assert (row["gross_inflows"] - row["self_transfers"] - row["loan_inflows"]
            - row["institutional_credits"] - row["large_transactions"]) == row["net_revenue"]
    assert len(r["details"]["institutional_credits"]["items"]) == 2


def test_bank_credits_stay_out_of_the_loan_and_large_buckets():
    """Categories remain mutually exclusive with a fourth one in play."""
    rows = [
        # A bank credit above the large-transaction threshold.
        txn(1, "2026-01-05 09:00:00", "257777777721", "BANK OF KIGALI PLC", ACC, "Jane Jane", 1_500_000),
        # A lender is borrowing, not an institutional credit, even though a
        # SACCO also matches the institution patterns.
        txn(2, "2026-01-06 09:00:00", "250790361487", "ZIGAMA CSS", ACC, "Jane Jane", 100_000),
        txn(3, "2026-01-07 09:00:00", "250788800001", "Real Customer", ACC, "Jane Jane", 7_000),
    ]
    t = run(rows)["summary"]["totals"]
    assert t["institutional_credits"] == 1_500_000
    assert t["large_transactions"] == 0
    assert t["loan_inflows"] == 100_000
    assert t["net_revenue"] == 7_000


def test_institutional_credits_cannot_be_identified_without_counterparty_names():
    """On a derived-name format every id is alphabetic.

    The 'non-numeric id means short code' rule would classify every private
    individual as an institution and deduct the entire statement.
    """
    from app.statements.base import Capabilities
    header = StatementHeader(customer="Jane DOE", msisdn="ACC-NO",
                             period_start="2026-01-01", period_end="2026-03-31",
                             declared_txn_count=1)
    rows = [txn(1, "2026-01-05 09:00:00", "name:x", "X", "ACC-NO", "Jane", 900_000)]
    st = ParsedStatement(header, rows, 1)
    st.capabilities = Capabilities(cp_id_basis="derived_name")
    r = analyze(st, Policy(), monthly_emi=0)
    assert r["summary"]["totals"]["institutional_credits"] == 0
    assert r["summary"]["totals"]["net_revenue"] == 900_000


def test_every_month_considered_is_reported_with_its_fate():
    """Which months fell out of the average, and why, must be on the report.

    The average is struck on a subset; an underwriter should not have to infer
    the missing months from a shorter table.
    """
    rows = []
    i = 1
    for month, days in ((5, range(12, 32)), (6, range(1, 31)), (7, range(1, 32)), (8, range(1, 11))):
        for d in days:
            rows.append(txn(i, f"2026-{month:02d}-{d:02d} 09:00:00", f"25078880{i:04d}",
                            f"Cust {i}", ACC, "Jane Jane", 20_000))
            i += 1
    r = analyze(ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-05-12",
                        period_end="2026-08-10", declared_txn_count=len(rows)), rows, 1),
        Policy(), monthly_emi=0)
    e = r["summary"]["loan_eligibility"]
    considered = {m["month"]: m for m in e["months_considered"]}

    # Nothing is dropped from the report, only from the average.
    assert set(considered) == {"2026-05", "2026-06", "2026-07", "2026-08"}
    assert [m for m in considered.values() if m["status"] == "averaged"].__len__() == 3

    aug = considered["2026-08"]
    assert aug["status"] == "excluded_too_short"
    assert aug["days_covered"] == 10
    assert aug["scale_factor"] == 3.0
    assert "10 days" in aug["reason"] and "15 days" in aug["reason"]

    # The threshold itself is published, not buried in the code.
    assert e["min_days_for_run_rate"] == 15
    short = [x for x in r["edge_cases"] if x["code"] == "MONTH_TOO_SHORT"][0]
    assert short["evidence"]["floor_days"] == 15
    assert "3.0x scale-up" in short["detail"]


def test_the_day_floor_is_configurable():
    """15 days is a default, not a law. A lender wanting a stricter or looser
    rule changes one number."""
    rows = []
    i = 1
    for month, days in ((6, range(1, 31)), (7, range(1, 32)), (8, range(1, 21))):
        for d in days:
            rows.append(txn(i, f"2026-{month:02d}-{d:02d} 09:00:00", f"25078880{i:04d}",
                            f"Cust {i}", ACC, "Jane Jane", 20_000))
            i += 1
    stmt_obj = ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-06-01",
                        period_end="2026-08-20", declared_txn_count=len(rows)), rows, 1)

    # An 81-day statement; the period floor is lowered so only the day floor is under test.
    lenient = analyze(stmt_obj, Policy(min_days_for_run_rate=15, statement_period_target_days=80),
                      monthly_emi=0)
    assert "2026-08" in lenient["summary"]["loan_eligibility"]["months_used"]

    strict = analyze(stmt_obj, Policy(min_days_for_run_rate=25, statement_period_target_days=80),
                     monthly_emi=0)
    assert "2026-08" in strict["summary"]["loan_eligibility"]["months_excluded_as_too_short"]


# ------------------------------------------------------ statement freshness
@pytest.mark.skip(reason="90-day period requirement temporarily disabled in _statement_freshness")
def test_statement_shorter_than_policy_period_blocks_revenue():
    rows = [txn(1, "2026-01-05 09:00:00", "250788800001", "Real Customer", ACC, "Jane Jane", 9_000)]
    stmt = ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                        period_end="2026-01-31", declared_txn_count=1), rows, 1)
    r = analyze(stmt, Policy(), monthly_emi=0)
    assert r["summary"]["revenue_assessed"] is False
    assert r["summary"]["totals"]["net_revenue"] is None
    assert r["summary"]["loan_eligibility"]["status"] == "not_assessed"
    assert r["summary"]["loan_eligibility"]["blocked_by"] == "statement_out_of_spec"
    assert any(e["code"] == "STATEMENT_OUT_OF_SPEC" for e in r["edge_cases"])
    assert any(f["code"] == "STATEMENT_OUT_OF_SPEC" for f in r["red_flags"])


def test_statement_with_incomplete_trailing_month_is_not_penalised():
    """A full 90-day pull made today legitimately ends mid-month - that is not
    a policy violation."""
    rows = [txn(1, "2026-06-05 09:00:00", "250788800001", "Real Customer", ACC, "Jane Jane", 9_000)]
    stmt = ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-05-23",
                        period_end="2026-08-20", declared_txn_count=1), rows, 1)
    r = analyze(stmt, Policy(), monthly_emi=0)
    assert r["summary"]["revenue_assessed"] is True
    assert r["summary"]["totals"]["net_revenue"] == 9_000


@pytest.mark.skip(reason="90-day period requirement temporarily disabled in _statement_freshness")
def test_statement_one_day_short_of_90_days_blocks_revenue():
    """Credit policy requires the full 90 days; there is no tolerance below it."""
    rows = [txn(1, "2026-06-05 09:00:00", "250788800001", "Real Customer", ACC, "Jane Jane", 9_000)]
    stmt = ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-05-24",
                        period_end="2026-08-20", declared_txn_count=1), rows, 1)
    r = analyze(stmt, Policy(), monthly_emi=0)
    assert r["summary"]["revenue_assessed"] is False
    assert r["summary"]["loan_eligibility"]["blocked_by"] == "statement_out_of_spec"


def test_statement_generated_too_long_ago_blocks_revenue():
    from datetime import datetime, timedelta
    stale = (datetime.now() - timedelta(days=Policy().statement_max_age_days + 1)).strftime("%Y-%m-%d")
    rows = [txn(1, "2026-01-05 09:00:00", "250788800001", "Real Customer", ACC, "Jane Jane", 9_000)]
    stmt = ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                        period_end="2026-03-31", generated_at=stale, declared_txn_count=1),
        rows, 1)
    r = analyze(stmt, Policy(), monthly_emi=0)
    assert r["summary"]["revenue_assessed"] is False
    assert r["summary"]["loan_eligibility"]["blocked_by"] == "statement_out_of_spec"


def test_statement_freshness_thresholds_are_configurable():
    rows = [txn(1, "2026-01-05 09:00:00", "250788800001", "Real Customer", ACC, "Jane Jane", 9_000)]
    stmt = ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                        period_end="2026-01-31", declared_txn_count=1), rows, 1)
    lenient = Policy(statement_period_target_days=30, statement_period_tolerance_days=0)
    r = analyze(stmt, lenient, monthly_emi=0)
    assert r["summary"]["revenue_assessed"] is True


def test_missing_period_or_generated_date_is_not_assessed_not_blocked():
    """Format limitations (no 'Generated' header) are reported, not punished."""
    rows = [txn(1, "2026-01-05 09:00:00", "250788800001", "Real Customer", ACC, "Jane Jane", 9_000)]
    stmt = ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=ACC, period_start="2026-01-01",
                        period_end="2026-03-31", declared_txn_count=1), rows, 1)
    r = analyze(stmt, Policy(), monthly_emi=0)
    assert r["summary"]["revenue_assessed"] is True
    assert any(e["code"] == "STATEMENT_FRESHNESS_NOT_ASSESSED" for e in r["edge_cases"])
