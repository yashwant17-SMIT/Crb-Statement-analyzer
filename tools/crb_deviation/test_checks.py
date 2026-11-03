"""Deviation points D1-D6 and the sheet reading, on synthetic reports and API responses."""
from datetime import datetime

import pytest

import checks
from checks import ALLOWED, REFER, REJECT, REVIEW
from google_io import cell_links, drive_id, find_columns
from app.crb_parser import CreditAccount, CreditProfile, ParsedCreditReport

CUTOFF = datetime(2024, 1, 1)


def report(band="FF", accounts=(), summary=None, cheques=()):
    return ParsedCreditReport(
        bureau="TransUnion Rwanda", subscriber="TEST", report_date="01 Oct 2026 09:00 AM", pdf_id="x",
        profile=CreditProfile(names="MR TEST", national_id="1"), score=600, band=band,
        probability_of_default=1.0, adverse_action_reasons=[], summary=summary or {},
        accounts=list(accounts), collaterals=[], collateral_duplicates_removed=0,
        insurance_policies=[], enquiries=[], pages=5, bounced_cheques=list(cheques))


def acct(**kw):
    base = dict(index=1, account_type="Instalment", classification="NORMAL RISK", account_status="CURRENT",
                principal_amount=1_000_000, balance_amount=500_000, arrears_amount=0.0,
                days_in_arrears=0, days_in_arrears_reported=True, is_open=True, listing_date="01-Mar-2024")
    base.update(kw)
    return CreditAccount(**base)


def npa(**kw):
    return acct(is_non_performing=True, classification="LOSS", **kw)


def by_code(rep):
    return {c.code: c.result for c in checks.run_checks(rep)}


def test_a_clean_ff_file_could_be_approved():
    ev = checks.evaluate(report(accounts=[acct()]))
    assert ev["overall"] == ALLOWED and ev["band_rule"] == "Rejected"
    assert ev["group"] == checks.COULD_APPROVE


@pytest.mark.parametrize("band,group", [
    ("DD", checks.NO_CHANGE), ("AA", checks.NO_CHANGE), ("EE", checks.COULD_APPROVE), ("JJ", checks.COULD_APPROVE),
])
def test_the_band_rule_splits_at_dd(band, group):
    assert checks.evaluate(report(band=band))["group"] == group


def test_no_band_is_not_assessed():
    assert checks.evaluate(report(band=None))["group"] == checks.NOT_ASSESSED


# D1
def test_d1_fraud_rejects():
    rep = report(summary={"fraudulent_cases": {"your_sector": 0, "other_sectors": 1}})
    assert by_code(rep)["D1"] == REJECT


def test_d1_cheque_before_the_cutoff_is_old_history():
    old = {"date_bounced": "10-Jun-2022"}
    assert by_code(report(cheques=[old]))["D1"] == ALLOWED
    assert by_code(report(cheques=[{"date_bounced": "10-Jun-2024"}]))["D1"] == REJECT


# D2
def test_d2_open_npa_owing_rejects():
    assert by_code(report(accounts=[npa(balance_amount=10_000)]))["D2"] == REJECT


def test_d2_open_npa_at_zero_is_allowed():
    assert by_code(report(accounts=[npa(balance_amount=0, arrears_amount=0)]))["D2"] == ALLOWED


def test_d2_open_npa_listed_before_the_cutoff_is_ignored_but_named():
    c = checks.d2_open_npa(report(accounts=[npa(listing_date="01-Mar-2022")]), CUTOFF)
    assert c.result == ALLOWED and "not counted, listed before 1 Jan 2024" in c.detail


# D4
@pytest.mark.parametrize("status,listed,open_", [
    ("WRITTEN OFF", "01-Mar-2019", False), ("Written-off", "01-Mar-2024", True), ("CHARGED OFF", None, False),
])
def test_d4_any_write_off_any_date_rejects(status, listed, open_):
    rep = report(accounts=[acct(account_status=status, listing_date=listed, is_open=open_, balance_amount=0)])
    assert by_code(rep)["D4"] == REJECT


# D5
def closed_npa(listed, term, last_paid):
    a = npa(is_open=False, account_status="CLOSED", listing_date=listed, last_payment_date=last_paid, balance_amount=0)
    a.repayment_term_months = term
    return a


def test_d5_matured_and_cleared_before_the_cutoff_is_allowed():
    # Taken Jan '21, matures Jan '22, cleared before Jan '24.
    assert by_code(report(accounts=[closed_npa("15-Jan-2021", 12, "20-Jun-2023")]))["D5"] == ALLOWED


def test_d5_maturing_after_the_cutoff_rejects():
    # Taken Jan '23, matures Jan '24, cleared before Oct '24.
    assert by_code(report(accounts=[closed_npa("15-Jan-2023", 12, "20-Sep-2024")]))["D5"] == REJECT


@pytest.mark.parametrize("principal,result", [(50_000, REFER), (50_001, REJECT)])
def test_d5_after_the_cutoff_is_refer_up_to_50k_loan_size(principal, result):
    a = closed_npa("15-Jan-2023", 12, "20-Sep-2024")
    a.principal_amount, a.arrears_amount = principal, 0
    assert by_code(report(accounts=[a]))["D5"] == result


def test_d5_cleared_after_the_cutoff_rejects():
    assert by_code(report(accounts=[closed_npa("15-Jan-2021", 12, "05-Feb-2024")]))["D5"] == REJECT


@pytest.mark.parametrize("listed,term", [(None, 12), ("11-Oct-2021", None), (None, None)])
def test_d5_cleared_before_the_cutoff_is_allowed_with_maturity_unknown(listed, term):
    # Akimana Pacifique #2: a 2021 mobile loan, no readable term, cleared Aug 2023.
    c = checks.d5_closed_npa(report(accounts=[closed_npa(listed, term, "15-Aug-2023")]), CUTOFF)
    assert c.result == ALLOWED and "term end unknown" in c.detail


def test_d5_unknown_maturity_shows_the_printed_terms():
    a = closed_npa("11-Oct-2021", None, "15-Aug-2023")
    a.repayment_terms = "Bullet"
    assert "terms printed as 'Bullet'" in checks.d5_closed_npa(report(accounts=[a]), CUTOFF).detail


def test_d5_no_clearance_date_is_review():
    assert by_code(report(accounts=[closed_npa("15-Jan-2021", 12, None)]))["D5"] == REVIEW


def test_d5_a_known_late_date_rejects_even_with_another_missing():
    assert by_code(report(accounts=[closed_npa(None, None, "01-Mar-2025")]))["D5"] == REJECT


# D6
@pytest.mark.parametrize("dpd,arrears,result", [
    (0, 0, ALLOWED), (15, 150_000, ALLOWED),
    (16, 0, REFER), (90, 100_000, REFER),
    (5, 150_001, REJECT), (16, 100_001, REJECT), (91, 0, REJECT), (120, 50_000, REJECT),
    (19, 1_039_200, REJECT),   # Akimana Pacifique #10: 10x the Refer limit
])
def test_d6_days_past_due_and_arrears(dpd, arrears, result):
    assert by_code(report(accounts=[acct(days_in_arrears=dpd, arrears_amount=arrears)]))["D6"] == result


def test_d6_worst_account_wins_and_old_accounts_are_ignored():
    rep = report(accounts=[acct(), acct(index=2, days_in_arrears=200, arrears_amount=900_000,
                                        listing_date="01-Mar-2023")])
    assert by_code(rep)["D6"] == ALLOWED
    rep.accounts.append(acct(index=3, days_in_arrears=100, arrears_amount=60_000))
    assert by_code(rep)["D6"] == REJECT


@pytest.mark.parametrize("arrears,result", [(5_000, REVIEW), (150_000, REVIEW), (150_001, REJECT)])
def test_d6_arrears_without_printed_days(arrears, result):
    rep = report(accounts=[acct(days_in_arrears_reported=False, arrears_amount=arrears)])
    assert by_code(rep)["D6"] == result


@pytest.mark.parametrize("band,group", [("FF", checks.REJECTED_REFER), ("BB", checks.APPROVED_REFER)])
def test_a_refer_puts_the_customer_in_the_refer_group(band, group):
    assert checks.evaluate(report(band=band, accounts=[acct(days_in_arrears=30, arrears_amount=20_000)]))["group"] == group


def test_review_outranks_refer():
    rep = report(accounts=[acct(days_in_arrears=30, arrears_amount=20_000),
                           closed_npa("15-Jan-2021", 12, None)])
    assert checks.evaluate(rep)["overall"] == REVIEW


def test_an_approved_band_with_a_write_off_would_be_rejected():
    ev = checks.evaluate(report(band="BB", accounts=[acct(account_status="WRITTEN OFF", is_open=False)]))
    assert ev["group"] == checks.WOULD_REJECT


# The sheet
CRB_URL = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view"


def test_links_are_found_in_chips_hyperlinks_formulas_and_text():
    assert cell_links({"chipRuns": [{"chip": {"richLinkProperties": {"uri": CRB_URL}}}]}) == [CRB_URL]
    assert cell_links({"hyperlink": CRB_URL, "formattedValue": "crb"}) == [CRB_URL]
    assert cell_links({"userEnteredValue": {"formulaValue": f'=HYPERLINK("{CRB_URL}","x")'}}) == [CRB_URL]
    assert cell_links({"textFormatRuns": [{"format": {"link": {"uri": CRB_URL}}}]}) == [CRB_URL]
    assert cell_links({"formattedValue": f"see {CRB_URL}"}) == [CRB_URL]


def test_drive_ids():
    assert drive_id(CRB_URL) == "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    assert drive_id("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUvWxYz012345") is not None
    assert drive_id("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345") is None
    assert drive_id("https://example.com/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345") is None


def test_the_ops_header_is_found_under_a_title():
    cells = lambda *t: [{"text": x, "links": []} for x in t]
    rows = [cells("Loan applications"), [],
            cells("Application ID", "Customer Name", "CRB band (AA, BB etc)", "PA Open Total Value(RWF) (from CRB report)",
                  "CRB Link", "Status Signed off by Credit risk team")]
    header, cols = find_columns(rows)
    assert header == 2
    assert cols == {"crb": 4, "customer": 1, "application_id": 0, "sheet_band": 2, "sheet_status": 5}


# Exact reasons a row could not be assessed
import run  # noqa: E402
import report as xl  # noqa: E402
from google_io import CannotAssess, download  # noqa: E402

ACCOUNT = "ops@example.com"


class Res:
    def __init__(self, code, body=None, content=b""):
        self.status_code, self._body, self.content = code, body or {}, content
        self.ok, self.text = code == 200, str(body)

    def json(self):
        return self._body


class Sess:
    def __init__(self, meta, media=None):
        self.meta, self.media = meta, media

    def get(self, url, params=None):
        return self.media if params.get("alt") == "media" else self.meta


def row_of(text="", links=()):
    return [{"text": text, "links": list(links)}]


def reason(text="", links=(), sess=None, tmp=None):
    return run.assess_row(sess, row_of(text, links), {"crb": 0}, 5, tmp, ACCOUNT)


@pytest.mark.parametrize("text,links,expected", [
    ("", (), "CRB cell is empty"),
    ("No record found", (), "No CRB report for this customer"),
    ("Invalid CRN provided", (), "No CRB report for this customer"),
    ("a.b@in.example.net pls update this", (), "No link in the CRB cell"),
    ("folder", ("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345",), "CRB link is not a Drive file"),
])
def test_cell_problems_have_their_own_reason(text, links, expected, tmp_path):
    rec = reason(text, links, tmp=tmp_path)
    assert rec["group"] == checks.NOT_ASSESSED and rec["reason"] == expected and rec["action"]


@pytest.mark.parametrize("meta,expected,in_detail", [
    (Res(404, {"error": {"message": "File not found"}}), "No access to the CRB file", ACCOUNT),
    (Res(200, {"name": "crb.jpg", "mimeType": "image/jpeg"}), "CRB file is not a PDF", "an image (JPEG)"),
    (Res(200, {"name": "crb", "mimeType": "application/vnd.google-apps.document"}), "CRB file is not a PDF", "a Google Doc"),
    (Res(200, {"name": "CRBs", "mimeType": "application/vnd.google-apps.folder"}), "CRB link is a folder", "CRBs"),
    (Res(200, {"name": "crb.pdf", "mimeType": "application/pdf", "capabilities": {"canDownload": False}}),
     "Download turned off", "crb.pdf"),
    (Res(403, {"error": {"message": "slow down", "errors": [{"reason": "userRateLimitExceeded"}]}}),
     "Google was busy", "slow down"),
])
def test_drive_problems_say_exactly_what_went_wrong(meta, expected, in_detail, tmp_path):
    with pytest.raises(CannotAssess) as e:
        download(Sess(meta), "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", tmp_path, ACCOUNT)
    assert e.value.reason == expected and in_detail in e.value.detail + e.value.action


def test_a_download_is_cached_with_its_name(tmp_path):
    sess = Sess(Res(200, {"name": "crb.pdf", "mimeType": "application/pdf"}), Res(200, content=b"%PDF-1.4 x"))
    assert download(sess, "f" * 33, tmp_path, ACCOUNT) == (b"%PDF-1.4 x", "crb.pdf")
    assert download(None, "f" * 33, tmp_path, ACCOUNT) == (b"%PDF-1.4 x", "crb.pdf")


def test_an_unreadable_pdf_names_the_file(tmp_path):
    with pytest.raises(CannotAssess) as e:
        run.parse(b"%PDF-1.4 broken", "crb.pdf")
    assert e.value.reason == "CRB report could not be read" and "'crb.pdf'" in e.value.detail


# The workbook
def scored(band, accounts, n):
    return run.score(run.empty_record(n, customer=f"Customer {n}"), report(band=band, accounts=accounts))


def test_the_workbook_has_the_ops_tabs_and_explains_each_row(tmp_path):
    from openpyxl import load_workbook
    late = closed_npa("15-Jan-2023", 12, "20-Sep-2024")
    recs = [scored("FF", [acct()], 3), scored("GG", [late], 4), scored("BB", [acct(days_in_arrears=30, arrears_amount=20_000)], 5),
            run.cannot(run.empty_record(6), CannotAssess("No access to the CRB file", "not shared", "request access")),
            scored(None, [acct()], 7)]
    out = tmp_path / "x.xlsx"
    xl.write(recs, out, {"source": "sheet", "account": ACCOUNT}, checks.Policy())
    wb = load_workbook(out)
    assert wb.sheetnames == ["Summary", "Could be approved", "Refer to credit head", "Would be rejected",
                             "Manual check", "Could not assess", "All customers", "Loan details"]
    allc = {r[0]: r for r in wb["All customers"].iter_rows(min_row=2, values_only=True)}
    assert allc[3][5] == "Could be approved" and allc[3][7].startswith("Passes all 5 checks:")
    assert allc[4][5] == "Stays rejected"
    assert "✗ Fail – Closed NPA" in allc[4][7] and "last payment Sep 2024" in allc[4][7]
    assert allc[5][5] == "Refer to credit head" and allc[5][4] == "Approved"
    assert allc[6][5] == "Could not assess" and allc[6][6] == "request access"
    assert allc[7][5] == "Could not assess" and allc[7][7].startswith("No band on the CRB report")
    cna = list(wb["Could not assess"].iter_rows(min_row=2, values_only=True))
    assert cna[0][3:6] == ("No access to the CRB file", "not shared", "request access")
    loans = list(wb["Loan details"].iter_rows(min_row=2, values_only=True))
    assert any(r[0] == 4 and r[17] == "✗ Fail – Closed NPA" for r in loans)
