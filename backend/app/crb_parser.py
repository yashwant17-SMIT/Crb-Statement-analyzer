"""
TransUnion Rwanda credit report PDF -> structured data.

Three things make naive extraction wrong on this format:

  1. Collateral entries use the same `N) Trade Sector:` prefix as credit
     accounts. Splitting the whole document on that pattern returns 50
     "accounts" for a report that has 27. Sections must be bounded first.
  2. All collateral entries on this report are byte-identical - one security
     repeated once per facility. Summing them inflates a 28.7M pledge to 661M.
     They are de-duplicated on parse and the duplicate count is reported.
  3. `Account Status: CURRENT` does not mean healthy. An account can read
     CURRENT while 65 days in arrears. Nothing here interprets status; the
     analyzer uses days-in-arrears and classification instead.

The parser extracts. It never judges.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

import pdfplumber

# Band ladder, lowest risk first. Straight off the report's own index page.
BAND_LADDER: List[str] = ["AA", "BB", "CC", "DD", "EE", "FF", "GG", "HH", "II", "JJ"]

# Fallback when the band letters are missing but a score is present.
BAND_SCORE_FLOORS: List[tuple[str, int]] = [
    ("AA", 668), ("BB", 660), ("CC", 654), ("DD", 649), ("EE", 642),
    ("FF", 637), ("GG", 630), ("HH", 624), ("II", 618), ("JJ", 0),
]

_SECTION_HEADS = [
    "PERSONAL PROFILE", "SUMMARY", "PERFORMING ACCOUNTS",
    "NON-PERFORMING ACCOUNTS", "Payment History", "Bounced Cheques",
    "Collaterals", "Insurance Policies",
    "Recent Enquiries", "Credit Score Band",
]

# Instalment frequency -> months per instalment. The bureau prints repayment
# terms as "<count> <FREQUENCY>": 12 MONTHLY, 120 DAILY, 3 WEEKLY. Only the
# count and the frequency are given; nothing states the payment period of the
# Scheduled Payments figure itself.
_FREQ_MONTHS: Dict[str, float] = {
    "DAILY": 1 / 30.436875,
    "WEEKLY": 7 / 30.436875,
    "FORTNIGHTLY": 14 / 30.436875,
    "BIWEEKLY": 14 / 30.436875,
    "MONTHLY": 1.0,
    "MONTHS": 1.0,
    "MONTH": 1.0,
    "MO": 1.0,
    "MOS": 1.0,
    "QUARTERLY": 3.0,
    "ANNUALLY": 12.0,
    "YEARLY": 12.0,
}


class CreditReportParseError(ValueError):
    """Raised when the file is not a readable credit bureau report."""


@dataclass
class CreditAccount:
    index: int
    trade_sector: str = ""
    account_type: str = ""
    repayment_terms: str = ""
    classification: str = ""
    account_status: str = ""
    account_ref: str = ""
    principal_amount: float = 0.0
    balance_amount: float = 0.0
    arrears_amount: float = 0.0
    scheduled_payment: float = 0.0
    scheduled_payment_available: bool = False
    # The bureau prints "Scheduled Payments: RWF 0.00" as a placeholder on
    # accounts that plainly do carry a schedule. A zero against a live balance
    # is not a reported obligation of zero, so it is not treated as reported.
    scheduled_payment_printed_zero: bool = False
    # Scheduled Payments has no stated period. Where the repayment terms say the
    # schedule is not monthly, treating the figure as a monthly EMI is an
    # assumption, and it is flagged rather than silently converted.
    scheduled_payment_period_assumed_monthly: bool = False
    repayment_frequency: Optional[str] = None
    repayment_instalments: Optional[int] = None
    repayment_term_months: Optional[float] = None
    term_basis: Optional[str] = None
    term_unusable_reason: Optional[str] = None
    calculated_emi: float = 0.0
    calculated_emi_available: bool = False
    days_in_arrears: int = 0
    days_in_arrears_reported: bool = False
    worst_arrear_days: int = 0
    listing_date: Optional[str] = None
    last_payment_date: Optional[str] = None
    disputed: bool = False
    is_open: bool = False
    is_non_performing: bool = False
    payment_history: List[Dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Only a zero is ambiguous between "reported as current" and "the field
        # is absent". Any non-zero value can only have come from the report, so
        # callers constructing an account directly need not set the flag.
        if self.days_in_arrears:
            self.days_in_arrears_reported = True

    def dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CreditProfile:
    crn: Optional[str] = None
    names: Optional[str] = None
    national_id: Optional[str] = None
    date_of_birth: Optional[str] = None
    gender: Optional[str] = None
    marital_status: Optional[str] = None
    employment: Optional[str] = None
    address: Optional[str] = None
    phones: List[Dict[str, str]] = field(default_factory=list)
    directorships: List[Dict[str, str]] = field(default_factory=list)

    def dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParsedCreditReport:
    bureau: str
    subscriber: Optional[str]
    report_date: Optional[str]
    pdf_id: Optional[str]
    profile: CreditProfile
    score: Optional[int]
    band: Optional[str]
    probability_of_default: Optional[float]
    adverse_action_reasons: List[str]
    summary: Dict[str, Dict[str, Optional[float]]]
    accounts: List[CreditAccount]
    collaterals: List[Dict[str, Any]]
    collateral_duplicates_removed: int
    insurance_policies: List[Dict[str, Any]]
    enquiries: List[Dict[str, str]]
    pages: int
    bounced_cheques: List[Dict[str, Any]] = field(default_factory=list)
    # Blocks that matched the account pattern but carried no account fields.
    # Kept rather than dropped silently, so a section-bounding regression is
    # visible instead of arriving as a phantom open account.
    skipped_account_blocks: List[Dict[str, str]] = field(default_factory=list)


# --------------------------------------------------------------- helpers ---
def _money(text: Optional[str]) -> float:
    if not text:
        return 0.0
    try:
        return float(text.replace(",", "").replace("RWF", "").strip())
    except ValueError:
        return 0.0


def _first(pattern: str, text: str, group: int = 1) -> Optional[str]:
    m = re.search(pattern, text)
    return m.group(group).strip() if m else None


def _section(text: str, start: str, ends: List[str]) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    j = len(text)
    for e in ends:
        k = text.find(e, i + len(start))
        if 0 <= k < j:
            j = k
    return text[i:j]


def _section_heading(text: str, start: str, ends: List[str]) -> str:
    """Extract a section whose heading occupies its own line.

    The TransUnion report's SUMMARY contains labels such as ``Performing
    Accounts``. A substring search for that phrase therefore starts the
    account parser inside the summary and can make the NPA records appear a
    second time. Account sections use standalone headings, so anchor them to
    a complete line instead.
    """
    match = re.search(rf"(?m)^\s*{re.escape(start)}\s*$", text)
    if not match:
        return ""
    i = match.start()
    j = len(text)
    for end in ends:
        end_match = re.search(
            rf"(?m)^\s*{re.escape(end)}\s*$", text[match.end():]
        )
        if end_match:
            k = match.end() + end_match.start()
            if k < j:
                j = k
    return text[i:j]


def normalise_band(raw: Optional[str], score: Optional[int]) -> Optional[str]:
    """Accept AA / aa / A / 'A A' / '675 - AA' and fall back to the score."""
    if raw:
        letters = re.sub(r"[^A-Za-z]", "", raw).upper()
        if letters:
            if len(letters) == 1:                 # 'A' -> 'AA'
                letters = letters * 2
            letters = letters[:2]
            if letters in BAND_LADDER:
                return letters
    if score is not None:
        for band, floor in BAND_SCORE_FLOORS:
            if score >= floor:
                return band
    return None


# ------------------------------------------------------------- extractors --
def _parse_profile(text: str) -> CreditProfile:
    p = CreditProfile()
    p.crn = _first(r"CRN\s*:\s*(\S+)", text)
    p.names = _first(r"Names\s*:\s*(.+?)(?:\s+\*\*|\n)", text)
    p.national_id = _first(r"(?<!PDF )\bID\s*:\s*(\d{10,})", text)
    p.date_of_birth = _first(r"Date of Birth:\s*(\S+)", text)
    p.gender = _first(r"Gender\s*:\s*(\w)", text)
    p.marital_status = _first(r"Marital Status:\s*(\w+)", text)
    p.employment = _first(r"Position\s*:\s*(.+)", text)
    p.address = _first(r"Physical Address\(es\).*?\n\s*(.+?),", text, 1)

    phone_re = r"^\s*(250\d{9}|0?7\d{8})\s+(\d{2}-\w{3}-\d{4})\s*$"
    for num, date in re.findall(phone_re, text, re.M):
        p.phones.append({"number": num, "last_update": date})

    dsec = _section(text, "Directorship", ["Shareholding", "SUMMARY"])
    title_re = r"(Chairman[\w ]*|Managing Director|Director|Secretary|Partner|Member|Shareholder)"
    for crn, name, title in re.findall(rf"^(\d{{5,}})\s+(.+?)\s+{title_re}\s*$", dsec, re.M):
        p.directorships.append({
            "company_crn": crn, "company": name.strip(), "title": title.strip(),
        })
    return p


def _parse_summary(text: str) -> Dict[str, Dict[str, Optional[float]]]:
    """The two-column 'Your Sector / Other Sectors' block."""
    labels = [
        ("non-performing_accounts", "Non-Performing Accounts"),
        ("open_npa", "Open NPA"),
        ("closed_npa", "Closed NPA"),
        ("npa_open_total_value", "NPA Open Total Value (RWF)"),
        ("performing_accounts", "Performing Accounts"),
        ("open_pa", "Open PA"),
        ("closed_pa", "Closed PA"),
        ("pa_open_total_value", "PA Open Total Value (RWF)"),
        ("credit_history_durationmonths", "Credit History Duration (Months)"),
        ("insurance_policies", "Insurance Policies"),
        ("bounced_cheques", "Bounced Cheques"),
        ("fraudulent_cases", "Fraudulent Cases"),
        ("credit_applications", "Credit Applications"),
        ("enquiries_-_last_30_days", "Enquiries - Last 30 Days"),
        ("enquiries_-_last_31_-_60_days", "Enquiries - Last 31 - 60 Days"),
        ("enquiries_-_last_61_-_90_days", "Enquiries - Last 61 - 90 Days"),
        ("enquiries_>_91_days", "Enquiries > 91 Days"),
    ]
    out: Dict[str, Dict[str, Optional[float]]] = {}
    for key, label in labels:
        # Two traps here. Whitespace around "(RWF)" is inconsistent between the
        # PA and NPA rows, so every literal space is matched loosely. And
        # "PA Open Total Value" is a substring of "NPA Open Total Value", so an
        # unanchored search reads the NPA row's figures into the PA key - the
        # applicant's performing exposure would silently become their
        # non-performing exposure. The lookbehind forbids that.
        pat = (r"(?<![A-Za-z])"
               + r"\s*".join(re.escape(part) for part in label.split(" ") if part)
               + r"\s+([\d,\.]+)\s+([\d,\.]+)")
        m = re.search(pat, text)
        if m:
            out[key] = {"your_sector": _money(m.group(1)), "other_sectors": _money(m.group(2))}
    return out


def _parse_bounced_cheques(text: str) -> List[Dict[str, Any]]:
    """The Bounced Cheques section.

    Previously unbounded and unparsed: its `N) Trade Sector:` header was swept
    into PERFORMING ACCOUNTS and became a zero-value open credit account.
    """
    sec = _section_heading(
        text, "Bounced Cheques",
        ["Collaterals", "Insurance Policies", "Recent Enquiries", "Credit Score Band"],
    )
    out: List[Dict[str, Any]] = []
    for b in re.split(r"\n(?=\d+\)\s*Trade Sector:)", sec):
        if "Cheque" not in b or not re.match(r"\s*\d+\)\s*Trade Sector:", b):
            continue
        out.append({
            "trade_sector": re.split(
                r"\s{2,}|Cheque", (_first(r"Trade Sector:\s*(.+)", b) or ""))[0].strip(),
            "cheque_date": _first(r"Cheque Date:\s*(\d{2}-\w{3}-\d{4})", b),
            "cheque_no": (_first(r"Cheque No\.?:\s*(\S+)", b) or "").strip(),
            "amount": _money(_first(r"Cheque Amount:\s*RWF\s*([\d,\.]+)", b)),
            "date_bounced": _first(r"Date Bounced:\s*(\d{2}-\w{3}-\d{4})", b),
            "reason": (_first(r"Bounce Reason:\s*(.+?)(?:\s{2,}|Date Bounced|$)", b) or "").strip(),
        })
    return out


def _parse_payment_history(block: str) -> List[Dict[str, str]]:
    """Two header/value line pairs of twelve columns each."""
    i = block.find("24 Month Payment History")
    if i < 0:
        return []
    lines = [l.strip() for l in block[i:].splitlines()[1:] if l.strip()]
    history: List[Dict[str, str]] = []
    for h in range(0, len(lines) - 1, 2):
        months = re.findall(r"[A-Z][a-z]{2} \d{2}", lines[h])
        if not months:
            break
        values = lines[h + 1].split()
        for m, v in zip(months, values):
            history.append({"month": m, "status": v})
    return history


def _term_months(terms: str) -> tuple[Optional[int], Optional[str], Optional[float], Optional[str], Optional[str]]:
    """Turn '120 DAILY' into an instalment count, a frequency and a term in months.

    Returns (instalments, frequency, term_months, basis, unusable_reason).

    Only MONTHLY is exact. DAILY and WEEKLY are converted at the mean calendar
    month, which is fine for a 120- or 180-instalment daily microloan and
    meaningless for a schedule shorter than one month, so a sub-month term is
    refused rather than turned into an EMI larger than the whole principal.
    """
    m = re.search(r"\b(\d{1,4})\s*([A-Z]+)\b", (terms or "").upper())
    if not m:
        return None, None, None, None, None
    count, freq = int(m.group(1)), m.group(2)
    per = _FREQ_MONTHS.get(freq)
    if per is None:
        return count, freq, None, None, f"Unrecognised repayment frequency {freq!r}."
    if count <= 0:
        return count, freq, None, None, "Repayment term has no instalment count."
    months = count * per
    basis = "exact_months" if per == 1.0 else f"converted_from_{freq.lower()}"
    if months < 1:
        return count, freq, None, None, (
            f"{count} {freq} is under one month; the bureau does not say whether "
            "this is an instalment count or a payment interval, so no monthly "
            "term is derived."
        )
    return count, freq, months, basis, None


def _parse_accounts(text: str) -> tuple[List[CreditAccount], List[Dict[str, str]]]:
    accounts: List[CreditAccount] = []
    skipped: List[Dict[str, str]] = []
    # Use exact section headings. ``PERFORMING ACCOUNTS`` is also a substring
    # of the ``NON-PERFORMING ACCOUNTS`` heading and appears as a label in the
    # summary; using text.find() here duplicates the NPA records.
    for head, npa in (("NON-PERFORMING ACCOUNTS", True), ("PERFORMING ACCOUNTS", False)):
        ends = [h for h in _SECTION_HEADS if h != head]
        sec = _section_heading(text, head, ends)
        if not sec:
            continue
        blocks = re.split(r"\n(?=\d+\)\s*Trade Sector:)", sec)
        for b in blocks:
            m = re.match(r"\s*(\d+)\)\s*Trade Sector:\s*(.*)", b)
            if not m:
                continue

            # `N) Trade Sector:` is shared by credit accounts, collaterals and
            # bounced cheques. Section bounding is the first defence; this is
            # the second. A credit account always carries at least one of these
            # three fields, and a cheque or a collateral carries none of them.
            if not any(k in b for k in ("Account Type:", "Account Status:", "Principal Amount:")):
                skipped.append({
                    "section": head,
                    "index": m.group(1),
                    "reason": "Block matched the account pattern but carries no "
                              "account type, status or principal - not a credit account.",
                    "raw": " ".join(b.split())[:200],
                })
                continue

            a = CreditAccount(index=int(m.group(1)), is_non_performing=npa)
            # The sector name is followed on the same line by a second column.
            a.trade_sector = re.split(r"\s{2,}|Principal Amount", m.group(2))[0].strip()
            a.account_type = (_first(r"Account Type:\s*(.+?)(?:\s{2,}|Last Payment|Listing Date|$)", b) or "").strip()
            a.repayment_terms = (_first(r"Repayment Terms:\s*(.+?)(?:\s{2,}|Listing Date|Scheduled|$)", b) or "").strip()
            a.classification = (_first(r"Classification:\s*(.+?)(?:\s{2,}|Scheduled|Arrears|$)", b) or "").strip()
            a.account_status = (_first(r"Account Status:\s*(.+)", b) or "").strip()
            a.account_ref = (_first(r"Account Ref:\s*(\S+)", b) or "").strip()
            a.principal_amount = _money(_first(r"Principal Amount:\s*RWF\s*([\d,\.]+)", b))
            a.balance_amount = _money(_first(r"Balance Amount:\s*RWF\s*([\d,\.]+)", b))
            a.arrears_amount = _money(_first(r"Arrears Amount:\s*RWF\s*([\d,\.]+)", b))
            scheduled_raw = _first(r"Scheduled Payments:\s*RWF\s*([\d,\.]+)", b)
            a.scheduled_payment = _money(scheduled_raw)
            # A printed 0.00 against a live balance is a placeholder, not an
            # obligation of zero. Accepting it as reported both understates the
            # monthly commitment and blocks the principal-only fallback below.
            a.scheduled_payment_printed_zero = (
                scheduled_raw is not None and a.scheduled_payment == 0.0
                and a.balance_amount > 0
            )
            a.scheduled_payment_available = (
                scheduled_raw is not None and not a.scheduled_payment_printed_zero
            )

            (a.repayment_instalments, a.repayment_frequency, a.repayment_term_months,
             a.term_basis, a.term_unusable_reason) = _term_months(a.repayment_terms)

            # Scheduled Payments carries no period. Where the schedule is not
            # monthly, reading the figure as a monthly EMI is an assumption.
            if (a.scheduled_payment_available and a.repayment_frequency
                    and _FREQ_MONTHS.get(a.repayment_frequency) not in (None, 1.0)):
                a.scheduled_payment_period_assumed_monthly = True

            # Principal-only estimate. No interest rate or fee is invented.
            if (not a.scheduled_payment_available and a.repayment_term_months
                    and a.principal_amount > 0):
                a.calculated_emi = a.principal_amount / a.repayment_term_months
                a.calculated_emi_available = True

            days_raw = _first(r"Days In Arrears:\s*(\d+)", b)
            a.days_in_arrears_reported = days_raw is not None
            a.days_in_arrears = int(days_raw or 0)
            a.worst_arrear_days = int(_first(r"Worst Arrear:\s*(\d+)", b) or 0)
            a.listing_date = _first(r"Listing Date:\s*(\d{2}-\w{3}-\d{4})", b)
            a.last_payment_date = _first(r"Last Payment:\s*(\d{2}-\w{3}-\d{4})", b)
            a.disputed = (_first(r"Disputed\?\s*(\w+)", b) or "NO").upper() == "YES"
            a.is_open = "CLOSED" not in a.account_status.upper()
            a.payment_history = _parse_payment_history(b)
            accounts.append(a)
    return accounts, skipped


def _parse_collaterals(text: str) -> tuple[List[Dict[str, Any]], int]:
    sec = _section(text, "Collaterals", ["Insurance Policies", "Recent Enquiries", "Credit Score Band"])
    raw: List[Dict[str, Any]] = []
    for b in re.split(r"\n(?=\d+\)\s*Trade Sector:)", sec):
        if "Collateral Type" not in b:
            continue
        raw.append({
            "trade_sector": (_first(r"Trade Sector:\s*(.+)", b) or "").strip(),
            "collateral_type": (_first(r"Collateral Type:\s*(.+?)(?:\s{2,}|Collateral Ref|$)", b) or "").strip(),
            "valuation_amount": _money(_first(r"Valuation Amount:\s*([\d,\.]+)", b)),
            "valuation_date": _first(r"Valuation Date:\s*(\d{2}-\w{3}-\d{4})", b),
            "sale_amount": _money(_first(r"Sale Amount:\s*([\d,\.]+)", b)),
        })
    seen, unique = set(), []
    for c in raw:
        key = (c["trade_sector"], c["collateral_type"], c["valuation_amount"], c["valuation_date"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique, len(raw) - len(unique)


def _parse_insurance(text: str) -> List[Dict[str, Any]]:
    sec = _section(text, "Insurance Policies", ["Recent Enquiries", "Credit Score Band"])
    out = []
    for b in re.split(r"\n(?=\d+\)\s*Policy No)", sec):
        pol = _first(r"Policy No\s*(\S+)", b)
        if not pol:
            continue
        out.append({
            "policy_no": pol,
            "classification": (_first(r"Policy Classification\s*(.+?)(?:\s{2,}|Policy Start|$)", b) or "").strip(),
            "insured_amount": _money(_first(r"Insured Amount\s*RWF\s*([\d,\.]+)", b)),
            "premium_amount": _money(_first(r"Premium Amount\s*RWF\s*([\d,\.]+)", b)),
            "balance_on_policy": _money(_first(r"Balance on Policy\s*RWF\s*([\d,\.]+)", b)),
            "status": (_first(r"Status\s+(\w+)\s+Policy Expiry", b) or "").strip(),
            "days_in_arrears": int(_first(r"Days in arrears\s*(\d+)", b) or 0),
            "start_date": _first(r"Policy Start Date\s*(\d{2}-\w{3}-\d{4})", b),
            "expiry_date": _first(r"Policy Expiry Date\s*(\d{2}-\w{3}-\d{4})", b),
        })
    return out


def _parse_enquiries(text: str) -> List[Dict[str, str]]:
    sec = _section(text, "Recent Enquiries", ["Credit Score Band"])
    out = []
    for m in re.finditer(r"(\d+)\)\s*(.+?)\s+(\d{2} \w{3} \d{4})\s+(.+)", sec):
        out.append({
            "index": m.group(1),
            "trade_sector": m.group(2).strip(),
            "date": m.group(3),
            "reason": m.group(4).strip(),
        })
    return out


# ------------------------------------------------------------------ main ---
def parse_credit_report(data: bytes) -> ParsedCreditReport:
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise CreditReportParseError(f"File could not be opened as a PDF: {exc}") from exc

    with pdf:
        pages = len(pdf.pages)
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    if len(text.strip()) < 50 * pages:
        raise CreditReportParseError(
            "This PDF has no readable text - it is a scanned or 'Print to PDF' image "
            "copy. Upload the original TransUnion PDF as downloaded from the bureau "
            "portal, not a printed or re-saved copy."
        )

    if "Bureau Score" not in text and "PERSONAL PROFILE" not in text:
        raise CreditReportParseError(
            "This does not look like a credit bureau report. Expected a TransUnion "
            "Rwanda consumer report - check you have not uploaded the MoMo statement here."
        )

    score_raw = _first(r"\b(\d{3})\s*-\s*([A-Z]{1,2})\b", text, 1)
    band_raw = _first(r"\b(\d{3})\s*-\s*([A-Z]{1,2})\b", text, 2)
    score = int(score_raw) if score_raw else None
    band = normalise_band(band_raw, score)

    pod = _first(r"Probability Of Default\s*\n?\s*([\d\.]+)\s*%", text)
    if pod is None:
        pod = _first(r"([\d\.]+)\s*%\s*\nScore Definition", text)

    reasons = [r.strip() for _, r in
               re.findall(r"^(\d)\.\s+(.+)$", _section(text, "Adverse Action Reasons",
                                                       ["Score Definition"]), re.M)]

    collaterals, dupes = _parse_collaterals(text)
    accounts, skipped_blocks = _parse_accounts(text)
    if not accounts:
        raise CreditReportParseError(
            "No credit accounts found. The report may be a scanned image or an "
            "unsupported bureau format."
        )

    return ParsedCreditReport(
        bureau="TransUnion Rwanda",
        subscriber=_first(r"\n([A-Z][A-Z0-9 &\.\-]+LTD)\s*\[", text),
        report_date=_first(r"(\d{2} \w{3} \d{4} \d{2}:\d{2} [AP]M)", text),
        pdf_id=_first(r"PDF ID\s*:\s*(\S+)", text),
        profile=_parse_profile(text),
        score=score,
        band=band,
        probability_of_default=float(pod) if pod else None,
        adverse_action_reasons=reasons,
        summary=_parse_summary(text),
        accounts=accounts,
        collaterals=collaterals,
        collateral_duplicates_removed=dupes,
        insurance_policies=_parse_insurance(text),
        enquiries=_parse_enquiries(text),
        pages=pages,
        bounced_cheques=_parse_bounced_cheques(text),
        skipped_account_blocks=skipped_blocks,
    )
