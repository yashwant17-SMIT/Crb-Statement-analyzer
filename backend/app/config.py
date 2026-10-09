"""
Tunable policy for the underwriting engine.

Everything a credit policy team would want to change lives here or can be
overridden per-request on the /analyze endpoint. Nothing in parser.py or
analyzer.py hardcodes a threshold.
"""
from dataclasses import dataclass, field
from typing import Dict, List

# Bands run AA (lowest risk) to JJ (highest). Kept here so Policy can validate
# min_band without importing the bureau parser.
BAND_LADDER: List[str] = ["AA", "BB", "CC", "DD", "EE", "FF", "GG", "HH", "II", "JJ"]


# --------------------------------------------------------------------------
# Lender registry
#
# Two ways an inflow gets classified as borrowed money:
#   1. NAME  - the sender matches a known lender below.
#   2. SHAPE - a round principal in, followed by a round repayment out to the
#              same institution within `loan_cycle_days` (see analyzer).
#
# `confirmed` lenders are deducted from net revenue.
# `suspected` institutions are reported but NOT deducted - a bank appearing on
# both sides of the ledger may be a loan, a savings account, or the customer's
# own current account, and a statement cannot tell them apart.
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Own-account wallet products
#
# These are savings/wallet products tied to the customer's OWN MoMo number.
# Money arriving from them is the customer moving their own money, not income
# and not borrowing. They must be listed before the lender registry, because
# "Mokash Savings" contains "mokash" and would otherwise be read as a nano-loan
# disbursement: on the reference statement that misfiled 531,347 RWF across 82
# transactions as borrowed money.
#
# These are matched as SELF-TRANSFERS (spec: "money the customer moved in from
# his/her own other account"). A savings wallet cannot belong to a third party,
# so this needs no name match against the holder.
# --------------------------------------------------------------------------
OWN_WALLET_PATTERNS: Dict[str, str] = {
    "mokash savings": "MoKash Savings (own savings wallet, NCBA/MTN)",
    "cba-savings": "MoKash Savings short-code (own savings wallet)",
    "cbasavings": "MoKash Savings short-code (own savings wallet)",
}

CONFIRMED_LENDERS: Dict[str, str] = {
    "mokash loan": "MoKash nano-loan (MTN Rwanda / NCBA Bank Rwanda)",
    "cba-personal": "MoKash loan short-code (CBA/NCBA)",
    "cbapersonal": "MoKash loan short-code (CBA/NCBA)",
    "bkloan": "BK nano-loan (Bank of Kigali, short-code BKLOAN.OVA)",
    "bk loan": "BK nano-loan (Bank of Kigali)",
    "moflex": "MoFlex (Ecobank / Mobile Money Rwanda digital loan product)",
    "mofaya": "MoFaya (BPR Bank Rwanda / Mobile Money Rwanda digital loan product)",
    "flow rwanda": "Flow Rwanda Ltd. (digital lending)",
    "flowrwanda": "Flow Rwanda Ltd. (digital lending)",
    "money phone africa": "Money Phone Africa Ltd. (digital lending)",
    "moneyphone": "Money Phone Africa Ltd. (digital lending)",
    "huza financial": "Huza Financial Service Ltd. (digital lending)",
    "standard life rwanda": "Standard Life Rwanda Plc (digital lending)",
    "asante financial": "Asante Financial Services Ltd. (digital lending)",
    "novacore": "Novacore Technologies Ltd. (digital lending)",
    "obrigado": "Obrigado Business Fund (digital lending)",
    # Credit policy names SACCOs and Zigama CSS as loan sources outright, so
    # these are deducted rather than merely reported. A SACCO inflow can also be
    # a savings withdrawal; deducting is the conservative reading and the one
    # the policy document specifies.
    "zigama": "Zigama CSS (SACCO / military savings & credit) - loan per credit policy",
    "urwego": "Urwego Opportunity Bank (microfinance) - loan per credit policy",
    "sacco": "SACCO (savings & credit co-operative) - loan per credit policy",
    "umwarimu": "Umwarimu SACCO - loan per credit policy",
    "ubudasa": "Ubudasa SACCO - loan per credit policy",
    "duterimbere": "Duterimbere IMF (microfinance) - loan per credit policy",
    "vision fund": "Vision Fund Rwanda (microfinance) - loan per credit policy",
    "letshego": "Letshego Rwanda (microfinance) - loan per credit policy",
    "inkunga": "Inkunga Finance (microfinance) - loan per credit policy",
    "abbank": "AB Bank Rwanda (microfinance) - loan per credit policy",
    "ab bank": "AB Bank Rwanda (microfinance) - loan per credit policy",
}

SUSPECTED_CREDIT_INSTITUTIONS: Dict[str, str] = {
    "bpr": "Banque Populaire du Rwanda (commercial bank - could be own account)",
}

# Transaction types that name a loan outright. Where a statement format carries
# a type column, this is exact and needs no name match - the MoMo spreadsheet
# export prints CUSTOM_LOAN_PAYOUT / CUSTOM_LOAN_REPAYMENT and no lender name.
LOAN_TYPE_PATTERNS: List[str] = ["loan"]

# Account types that are not income documents. An agent float account exists to
# hold the operator's money; its credits are the agent funding his own float,
# not revenue. Matched against the account type printed in the statement header.
FLOAT_ACCOUNT_PATTERNS: List[str] = ["float", "agent float", "till", "nostro"]

# Institutions whose credits are almost never third-party trade revenue:
# the customer's own bank/e-wallet accounts, remittance rails, operator credits.
INSTITUTION_PATTERNS: List[str] = [
    "bank", "banque", "equity", "urwego", "zigama", "ncba", "sacco", "bpr",
    "ekash", "e-kash", "worldremit", "world remit", "rwandacell", "system",
    "mokash", "bok", "kigali", "cogebanque", "gtbank", "im bank", "access bank",
    "unguka", "mtn", "airtel", "push and pull", "abbank", "ab bank",
    "western union", "mobicash", "spenn", "irembo", "rra",
]

# Sub-strings that mark a counterparty as a business/merchant rather than a
# private individual, for the distinct-individual-senders disclosure.
# Words that make an otherwise unknown counterparty plausibly lender-like for
# pattern-based loan detection. This is intentionally narrower than the general
# institution/merchant classifier so an ordinary person-to-person transfer is
# not silently turned into a loan.
LENDER_PATTERN_HINTS: List[str] = [
    "finance", "financial", "credit", "loan", "lending", "capital",
    "microfinance", "mfi", "sacco", "bank", "fund", "money phone",
]

MERCHANT_PATTERNS: List[str] = [
    " ltd", "ltd ", "limited", "s.a", "plc", "company", "supermarket",
    "petroleum", "petroliere", "pharmacy", "pharmacie", "restaurant", "hotel",
    "shop", "boutique", "group", "enterprise", "entreprise", "holdings",
    "investment", "trading", "services", "agency", "insurance", "hospital",
    "hopital", "clinic", "school", "station", "oil", "cafe", "café",
]


@dataclass
class Policy:
    # --- Step 0: the credit bureau gate -----------------------------------
    # The bureau check runs before the statement is parsed. Only applicants at
    # or above this band proceed. Ladder is AA (lowest risk) to JJ (highest);
    # min_band="DD" therefore admits AA, BB, CC and DD.
    min_band: str = "DD"

    # A stale bureau report is a gate failure, exactly like a failing band: the
    # applicant's position may have moved since the pull, so the statement is
    # not assessed against it. Set from the report's own "Generated" timestamp.
    crb_max_age_days: int = 15

    # Accounts listed before this date are not counted toward NPA or arrears
    # findings (including days past due and a worsening arrears trend), nor
    # are cheques bounced before it - they are old history,
    # not the applicant's current position. The bureau's own totals and the
    # exposure figures (balances, EMI, cap) are unaffected; only the findings
    # that would otherwise fire on a stale event are suppressed. An account
    # whose listing date cannot be read is NOT suppressed - only a date this
    # side of the cutoff or an unreadable one is decided by inclusion, never
    # by absence of proof.
    crb_history_cutoff: str = "2024-01-01"

    # Advisory thresholds. These do NOT open or close the gate - they raise
    # findings, and a blocking finding means a credit officer must decide.
    max_days_in_arrears: int = 30      # any open account at/above this is blocking
    max_arrears_refer: int = 500_000   # arrears above this are blocking, below are a warning
    max_enquiries_90d: int = 3         # enquiry velocity warning

    # A non-performing facility (LOSS, DOUBTFUL, SUBSTANDARD) is not being
    # serviced, so a "monthly EMI" for it describes a repayment schedule that
    # is not happening. Default False: it is reported as an unserviced
    # obligation instead of deducted. Note the direction - excluding it makes
    # MORE revenue available, so it is paired with a blocking NPA finding and
    # must never be read as the account being harmless.
    deduct_emi_on_non_performing: bool = False

    # --- Step 2 exclusions -------------------------------------------------
    # A single inflow STRICTLY GREATER THAN this is excluded in full.
    large_txn_threshold: int = 1_000_000

    # --- Step 3 disclosure 5 ----------------------------------------------
    # Minimum inflow count for the product. Set to None if your credit policy
    # has not defined one - the API then reports "not_assessed" rather than
    # inventing a pass/fail.
    min_inflow_count: int | None = None
    min_active_months: int | None = None

    # --- Regulatory / product ceiling -------------------------------------
    credit_cap: int = 3_000_000

    # --- Red-flag sensitivities -------------------------------------------
    # Share of a day's inflow that must leave the same day to call it a
    # pass-through day.
    passthrough_day_ratio: float = 0.90
    passthrough_day_min_inflow: int = 500_000
    # Rapid round-trip window.
    roundtrip_window_minutes: int = 60
    roundtrip_min_amount: int = 200_000
    roundtrip_ratio: float = 0.90
    # Concentration: flag a month if the single largest counterparty exceeds
    # this share of that month's net, or the top three exceed top3_share.
    concentration_single_share: float = 0.20
    concentration_top3_share: float = 0.35
    # A month is also flagged when one inflow alone contributes at least this
    # share of gross inflow, even if the transaction is already excluded as a
    # large transaction. This keeps single-payment inflation visible.
    concentration_single_txn_share: float = 0.50
    # Profile deviation band.
    profile_sigma: float = 3.0
    # Round-number test.
    round_step: int = 10_000
    round_value_share_flag: float = 0.50
    # Loan-shape detection window. Pattern detection is deliberately conservative:
    # require a meaningful round principal and repayments that look like a bounded
    # repayment schedule rather than arbitrary repeated transfers.
    loan_cycle_days: int = 45
    loan_pattern_min_principal: int = 200_000
    loan_pattern_max_repayment_multiple: float = 1.50
    loan_pattern_min_repayment_ratio: float = 0.50
    # A partial month is one covering fewer than this many days of the period.
    # Credits from banks, SACCOs, e-wallets and operators are deducted from net
    # revenue. This is a FOURTH exclusion beyond the three the credit policy
    # document names (self-transfers, loan inflows, large transactions), added
    # on instruction because such credits are not trade income.
    exclude_institutional_credits: bool = True
    full_month_min_days: int = 28
    # A month is scaled to a 30-day equivalent before averaging. Below this many
    # days the extrapolation multiplier gets large enough that the estimate says
    # more about the multiplier than about the trade, so the month is reported
    # and excluded from the average instead.
    #
    # Set to 15 - half a month - and the reason is the multiplier, not the day
    # count. Scaling to 30 days multiplies by 30/days:
    #
    #     20 days -> 1.5x     15 days -> 2.0x     10 days -> 3.0x     5 days -> 6.0x
    #
    # At 15 the factor is exactly 2.0, so no month is ever averaged on the
    # strength of more than doubling it: the observed period is at least as long
    # as the part being inferred. Past that the figure is mostly arithmetic. Ten
    # days of takings tripled is a projection, not a measurement, and on the
    # reference statement including a 10-day August would have lifted the
    # average 39% on that basis alone.
    #
    # A chosen default, not a regulatory figure. Raise it for a stricter rule
    # (25 accepts only near-complete months); lower it to accept thinner
    # evidence.
    min_days_for_run_rate: int = 15

    # --- Statement loan eligibility ----------------------------------------
    # NEW CUSTOMER rates. Existing Flow customers use their own rates and
    # sanction table, configured below the new-customer table.
    eligibility_months: int = 3
    net_revenue_rate: float = 0.15
    fee_cap_rate: float = 0.15

    # NEW CUSTOMER sanction table. Bounds are INCLUSIVE at both ends, matching
    # the credit policy document: a fee cap of exactly 55,000 sanctions 500,000,
    # not 600,000.
    #
    # The bands are contiguous and non-overlapping. Bands are still matched in
    # order, so if a future table overlaps the lower sanction wins and the
    # response flags it as `band_overlap_resolved` rather than silently picking.
    #
    # The top band is open-ended (`max_fee_cap: None`): any fee cap of 250,001
    # or more is eligible for 2,000,000. Only a fee cap below the 50,000 floor
    # falls outside the table.
    loan_eligibility_bands: List[dict] = field(default_factory=lambda: [
        {"min_fee_cap": 50_000, "max_fee_cap": 55_000, "loan_amount": 500_000},
        {"min_fee_cap": 55_001, "max_fee_cap": 65_000, "loan_amount": 600_000},
        {"min_fee_cap": 65_001, "max_fee_cap": 75_000, "loan_amount": 700_000},
        {"min_fee_cap": 75_001, "max_fee_cap": 85_000, "loan_amount": 800_000},
        {"min_fee_cap": 85_001, "max_fee_cap": 95_000, "loan_amount": 900_000},
        {"min_fee_cap": 95_001, "max_fee_cap": 175_000, "loan_amount": 1_000_000},
        {"min_fee_cap": 175_001, "max_fee_cap": 250_000, "loan_amount": 1_500_000},
        {"min_fee_cap": 250_001, "max_fee_cap": None, "loan_amount": 2_000_000},
    ])

    # EXISTING CUSTOMER rates: net revenue is 18% of the monthly average
    # inflow (after the bureau EMI, exactly as for a new customer) and the fee
    # cap is 20% of that.
    existing_net_revenue_rate: float = 0.18
    existing_fee_cap_rate: float = 0.20

    # EXISTING CUSTOMER sanction table. Same rules as the new-customer table:
    # inclusive bounds, matched in order, top band open-ended.
    #
    # The policy document prints 75,001-85,001 / 85,000-95,001 / 95,001-105,000,
    # which overlap at 85,000, 85,001 and 95,001. They are read as contiguous
    # bands here, as the same typos in the new-customer table were.
    # 205,001-300,000 and ">300,000" both sanction 3,000,000, so they are a
    # single open-ended band.
    existing_loan_eligibility_bands: List[dict] = field(default_factory=lambda: [
        {"min_fee_cap": 50_000, "max_fee_cap": 55_000, "loan_amount": 500_000},
        {"min_fee_cap": 55_001, "max_fee_cap": 65_000, "loan_amount": 600_000},
        {"min_fee_cap": 65_001, "max_fee_cap": 75_000, "loan_amount": 700_000},
        {"min_fee_cap": 75_001, "max_fee_cap": 85_000, "loan_amount": 800_000},
        {"min_fee_cap": 85_001, "max_fee_cap": 95_000, "loan_amount": 900_000},
        {"min_fee_cap": 95_001, "max_fee_cap": 105_000, "loan_amount": 1_000_000},
        {"min_fee_cap": 105_001, "max_fee_cap": 205_000, "loan_amount": 2_000_000},
        {"min_fee_cap": 205_001, "max_fee_cap": None, "loan_amount": 3_000_000},
    ])

    # --- Statement document validity ---------------------------------------
    # Credit policy asks for at least a 90-day statement, generated recently -
    # not an old export that happens to cover the right months. Both are gate
    # failures: no revenue or sanction figure is stated until a document that
    # meets them is supplied. The period check is a FLOOR: a longer statement
    # is more history, never a problem. Credit policy requires the full 90
    # days, so there is no tolerance below the floor by default.
    statement_period_target_days: int = 90
    statement_period_tolerance_days: int = 0
    statement_max_age_days: int = 10

    # --- Format handling ---------------------------------------------------
    # Refuse to state a revenue figure for an account the header identifies as
    # an agent float account. Set False to compute one anyway; the report still
    # carries the warning either way.
    block_revenue_on_float_accounts: bool = True

    own_wallet_patterns: Dict[str, str] = field(default_factory=lambda: dict(OWN_WALLET_PATTERNS))
    loan_type_patterns: List[str] = field(default_factory=lambda: list(LOAN_TYPE_PATTERNS))
    float_account_patterns: List[str] = field(default_factory=lambda: list(FLOAT_ACCOUNT_PATTERNS))
    confirmed_lenders: Dict[str, str] = field(default_factory=lambda: dict(CONFIRMED_LENDERS))
    suspected_lenders: Dict[str, str] = field(default_factory=lambda: dict(SUSPECTED_CREDIT_INSTITUTIONS))
    institution_patterns: List[str] = field(default_factory=lambda: list(INSTITUTION_PATTERNS))
    merchant_patterns: List[str] = field(default_factory=lambda: list(MERCHANT_PATTERNS))
    lender_pattern_hints: List[str] = field(default_factory=lambda: list(LENDER_PATTERN_HINTS))

    def __post_init__(self) -> None:
        """Normalise and validate what a non-technical user might edit here.

        START-HERE.md invites the reader to change min_band in this file. A
        lowercase or unknown band used to reach _rank(), return None, and 500
        every bureau check with a TypeError. Fail loudly at construction
        instead, naming the valid values.
        """
        band = (self.min_band or "").strip().upper()
        if band not in BAND_LADDER:
            raise ValueError(
                f"min_band={self.min_band!r} is not a valid band. "
                f"Use one of: {', '.join(BAND_LADDER)}."
            )
        self.min_band = band

        from datetime import datetime as _dt
        try:
            _dt.strptime(self.crb_history_cutoff, "%Y-%m-%d")
        except (ValueError, TypeError):
            raise ValueError(
                f"crb_history_cutoff={self.crb_history_cutoff!r} is not a valid "
                "date. Use YYYY-MM-DD."
            )

    def as_dict(self) -> dict:
        return {
            "min_band": self.min_band,
            "crb_max_age_days": self.crb_max_age_days,
            "crb_history_cutoff": self.crb_history_cutoff,
            "statement_period_target_days": self.statement_period_target_days,
            "statement_period_tolerance_days": self.statement_period_tolerance_days,
            "statement_max_age_days": self.statement_max_age_days,
            "max_days_in_arrears": self.max_days_in_arrears,
            "max_arrears_refer": self.max_arrears_refer,
            "max_enquiries_90d": self.max_enquiries_90d,
            "deduct_emi_on_non_performing": self.deduct_emi_on_non_performing,
            "large_txn_threshold": self.large_txn_threshold,
            "min_inflow_count": self.min_inflow_count,
            "min_active_months": self.min_active_months,
            "credit_cap": self.credit_cap,
            "passthrough_day_ratio": self.passthrough_day_ratio,
            "roundtrip_window_minutes": self.roundtrip_window_minutes,
            "concentration_single_share": self.concentration_single_share,
            "concentration_top3_share": self.concentration_top3_share,
            "concentration_single_txn_share": self.concentration_single_txn_share,
            "passthrough_day_min_inflow": self.passthrough_day_min_inflow,
            "roundtrip_min_amount": self.roundtrip_min_amount,
            "roundtrip_ratio": self.roundtrip_ratio,
            "profile_sigma": self.profile_sigma,
            "round_step": self.round_step,
            "round_value_share_flag": self.round_value_share_flag,
            "exclude_institutional_credits": self.exclude_institutional_credits,
            "full_month_min_days": self.full_month_min_days,
            "min_days_for_run_rate": self.min_days_for_run_rate,
            "eligibility_months": self.eligibility_months,
            "net_revenue_rate": self.net_revenue_rate,
            "fee_cap_rate": self.fee_cap_rate,
            "loan_eligibility_bands": list(self.loan_eligibility_bands),
            "existing_net_revenue_rate": self.existing_net_revenue_rate,
            "existing_fee_cap_rate": self.existing_fee_cap_rate,
            "existing_loan_eligibility_bands": list(self.existing_loan_eligibility_bands),
            "loan_cycle_days": self.loan_cycle_days,
            "loan_pattern_min_principal": self.loan_pattern_min_principal,
            "loan_pattern_max_repayment_multiple": self.loan_pattern_max_repayment_multiple,
            "loan_pattern_min_repayment_ratio": self.loan_pattern_min_repayment_ratio,
            "block_revenue_on_float_accounts": self.block_revenue_on_float_accounts,
            "own_wallet_patterns": sorted(set(self.own_wallet_patterns.values())),
            "confirmed_lenders": sorted(set(self.confirmed_lenders.values())),
            "suspected_lenders": sorted(set(self.suspected_lenders.values())),
            "lender_pattern_hints": list(self.lender_pattern_hints),
        }


DEFAULT_POLICY = Policy()
