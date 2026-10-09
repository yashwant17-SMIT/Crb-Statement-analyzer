"""
Several statements, one outcome.

Two shapes are supported: several exports of the SAME account (merged into one
statement, overlapping rows kept once) and DIFFERENT accounts (analysed apart,
then added month by month, with money moved between them deducted once).
"""
from __future__ import annotations

from datetime import datetime

from app.analyzer import analyze
from app.combine import account_label, combine_reports, group_by_account
from app.config import Policy
from app.parser import ParsedStatement, StatementHeader, Txn
from app.statements import Capabilities

MOMO = "250700000001"
BANK = "00040-0000000-01"
# Statements must be freshly generated to be underwritten at all.
TODAY = datetime.now().strftime("%Y-%m-%d 00:00:00")


def txn(i, ts, frm, frm_name, to, to_name, amount, direction=None):
    return Txn(str(i), "SUCCESSFUL", "TRANSFER", ts, frm, frm_name, to, to_name,
               amount, None, 1000, 1, direction)


def momo(rows, start, end, generated=TODAY):
    return ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=MOMO, period_start=start, period_end=end,
                        generated_at=generated, provider="MTN Rwanda MoMo"),
        rows, 1)


def bank(rows, start="2026-01-01 00:00:00", end="2026-03-31 00:00:00"):
    return ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn=BANK, period_start=start, period_end=end,
                        generated_at=TODAY, provider="Bank of Kigali"),
        rows, 1, source_format="bk_statement",
        # What the real BK adapter declares: names from narration, no ids.
        capabilities=Capabilities(counterparty_id=False, txn_type=False, status=False,
                                  cp_id_basis="derived_name", timestamp_precision="day"))


def sale(i, ts, amount, acct=MOMO):
    return txn(i, ts, f"25078{i:07d}", f"Customer {i}", acct, "Jane DOE", amount)


def underwrite(items, policy=None):
    policy = policy or Policy()
    groups = group_by_account(items)
    for g in groups:
        g["label"] = account_label(g["statement"])
        g["report"] = analyze(g["statement"], policy, monthly_emi=0)
    if len(groups) == 1:
        return groups, groups[0]["report"]
    return groups, combine_reports(groups, policy, monthly_emi=0)


def test_two_exports_of_one_wallet_merge_and_drop_the_overlap():
    jan = [sale(1, "2026-01-10 10:00:00", 10_000), sale(2, "2026-01-31 10:00:00", 20_000)]
    feb = [sale(2, "2026-01-31 10:00:00", 20_000), sale(3, "2026-02-10 10:00:00", 30_000)]
    groups, report = underwrite([
        ("jan.pdf", momo(jan, "2026-01-01 00:00:00", "2026-01-31 23:59:59")),
        ("feb.pdf", momo(feb, "2026-01-31 00:00:00", "2026-02-28 23:59:59")),
    ])
    assert len(groups) == 1
    assert groups[0]["duplicates_dropped"] == 1
    assert report["summary"]["totals"]["gross_inflows"] == 60_000
    assert report["account"]["period_start"].startswith("2026-01-01")
    assert report["account"]["period_end"].startswith("2026-02-28")


def test_gap_between_exports_of_one_wallet_is_reported():
    groups, _ = underwrite([
        ("jan.pdf", momo([sale(1, "2026-01-10 10:00:00", 1)], "2026-01-01", "2026-01-31")),
        ("mar.pdf", momo([sale(2, "2026-03-10 10:00:00", 1)], "2026-03-01", "2026-03-31")),
    ])
    assert groups[0]["gaps"] == [{"from": "2026-02-01", "to": "2026-02-28"}]


def test_the_oldest_export_decides_freshness():
    groups, _ = underwrite([
        ("a.pdf", momo([sale(1, "2026-01-10 10:00:00", 1)], "2026-01-01", "2026-01-31",
                       generated="2025-01-01 00:00:00")),
        ("b.pdf", momo([sale(2, "2026-02-10 10:00:00", 1)], "2026-02-01", "2026-02-28",
                       generated=TODAY)),
    ])
    assert groups[0]["statement"].header.generated_at == "2025-01-01 00:00:00"


def test_momo_and_bank_are_added_month_by_month():
    m = [sale(1, "2026-01-10 10:00:00", 100_000), sale(2, "2026-02-10 10:00:00", 100_000),
         sale(3, "2026-03-10 10:00:00", 100_000)]
    b = [txn(10, "2026-01-12", "", "Client A", BANK, "", 50_000, "in"),
         txn(11, "2026-02-12", "", "Client B", BANK, "", 50_000, "in"),
         txn(12, "2026-03-12", "", "Client C", BANK, "", 50_000, "in")]
    groups, report = underwrite([
        ("momo.pdf", momo(m, "2026-01-01 00:00:00", "2026-03-31 23:59:59")),
        ("bk.pdf", bank(b)),
    ])
    assert len(groups) == 2
    assert report["combined"] is True
    assert [r["net_revenue"] for r in report["summary"]["rows"]] == [150_000] * 3
    assert report["summary"]["totals"]["net_revenue"] == 450_000
    # The usual 30-day footing: Jan/Mar scaled down from 31 days, Feb up from 28.
    expected = round(sum(round(150_000 / d * 30) for d in (31, 28, 31)) / 3)
    assert report["summary"]["loan_eligibility"]["average_monthly_net_revenue"] == expected
    assert report["reconciliation"]["all_figures_reconcile"]
    assert {a["label"] for a in report["accounts"]} == {
        "MTN Rwanda MoMo · " + MOMO, "Bank of Kigali · " + BANK}


def test_money_moved_between_own_accounts_is_counted_once():
    """MoMo -> bank of 80,000: it is revenue on neither side. The bank credit
    names a private-looking sender, so only the cross-account match catches it."""
    m = [sale(1, "2026-01-10 10:00:00", 100_000),
         txn(2, "2026-01-15 09:00:00", MOMO, "Jane DOE", "BK", "Bank of Kigali", 80_000)]
    b = [txn(10, "2026-01-15", "", "J Doe transfer", BANK, "", 80_000, "in"),
         txn(11, "2026-01-20", "", "Client A", BANK, "", 40_000, "in")]
    _, report = underwrite([
        ("momo.pdf", momo(m, "2026-01-01 00:00:00", "2026-01-31 23:59:59")),
        ("bk.pdf", bank(b, "2026-01-01", "2026-01-31")),
    ])
    jan = report["summary"]["rows"][0]
    assert jan["net_revenue"] == 140_000
    assert report["details"]["self_transfers"]["by_basis"]["inter_account"] == {
        "count": 1, "total": 80_000}
    assert any(e["code"] == "INTER_ACCOUNT_TRANSFERS" for e in report["edge_cases"])
    assert report["reconciliation"]["all_figures_reconcile"]


def test_a_payment_to_a_person_is_not_an_own_account_transfer():
    """Same amount, same day, but the MoMo outflow paid a person: coincidence."""
    m = [txn(2, "2026-01-15 09:00:00", MOMO, "Jane DOE", "250799999999", "Agent X", 80_000)]
    b = [txn(10, "2026-01-15", "", "Client A", BANK, "", 80_000, "in")]
    _, report = underwrite([
        ("momo.pdf", momo(m, "2026-01-01 00:00:00", "2026-01-31 23:59:59")),
        ("bk.pdf", bank(b, "2026-01-01", "2026-01-31")),
    ])
    assert report["details"]["inter_account_transfers"] == []
    assert report["summary"]["totals"]["net_revenue"] == 80_000


def momo2(rows):
    return ParsedStatement(
        StatementHeader(customer="Jane DOE", msisdn="250700000002",
                        period_start="2026-01-01 00:00:00", period_end="2026-01-31 23:59:59",
                        generated_at=TODAY, provider="MTN Rwanda MoMo"), rows, 1)


def test_two_wallets_match_only_on_identity_not_on_coincident_amounts():
    w1 = [sale(1, "2026-01-10 10:00:00", 500),                        # a customer
          txn(2, "2026-01-12 10:00:00", "250700000002", "J D", MOMO, "Jane DOE", 30_000)]
    w2 = [txn(20, "2026-01-10 11:00:00", "250700000002", "Jane DOE", "250781111111", "Shop", 500),
          txn(21, "2026-01-12 09:59:00", "250700000002", "Jane DOE", MOMO, "J D", 30_000)]
    _, report = underwrite([
        ("w1.pdf", momo(w1, "2026-01-01 00:00:00", "2026-01-31 23:59:59")),
        ("w2.pdf", momo2(w2)),
    ])
    moved = report["details"]["inter_account_transfers"]
    # The 30,000 came from wallet two; the 500 from a customer is revenue,
    # whatever wallet two happened to pay out that day.
    assert [t["amount"] for t in moved] == [30_000]


def test_an_account_that_cannot_state_revenue_is_left_out_of_the_sum():
    stale = momo([sale(1, "2026-01-10 10:00:00", 100_000)], "2026-01-01", "2026-01-31",
                 generated="2020-01-01 00:00:00")
    b = [txn(10, "2026-01-12", "", "Client A", BANK, "", 50_000, "in")]
    _, report = underwrite([("old.pdf", stale), ("bk.pdf", bank(b, "2026-01-01", "2026-01-31"))])
    assert report["summary"]["totals"]["net_revenue"] == 50_000
    excluded = [a for a in report["accounts"] if not a["included_in_combined"]]
    assert [a["account_id"] for a in excluded] == [MOMO]
    assert any(e["code"] == "ACCOUNT_EXCLUDED_FROM_COMBINED" for e in report["edge_cases"])


def _named(name, acct):
    return ParsedStatement(StatementHeader(customer=name, msisdn=acct, provider="MTN Rwanda MoMo"), [], 1)


def test_name_check_compares_statements_with_each_other_without_a_bureau():
    from app.main import _name_check
    nc = _name_check([_named("Jane UWERA", "1"), _named("Eric MUGISHA", "2")])
    assert nc["reference"] == "first_statement"
    assert [i["status"] for i in nc["items"]] == ["match", "mismatch"]
    assert nc["needs_review"] and nc["mismatch_count"] == 1


def test_name_check_against_the_bureau_tolerates_name_order_and_titles():
    from app.main import _name_check
    nc = _name_check([_named("Audace TUYISENGE", "1")], bureau_name="MR TUYISENGE AUDACE")
    assert nc["reference"] == "bureau"
    assert nc["items"][0]["status"] == "match"
    assert not nc["needs_review"]


def test_a_single_statement_without_a_bureau_has_nothing_to_check():
    from app.main import _name_check
    assert not _name_check([_named("Jane DOE", "1")])["needs_review"]
