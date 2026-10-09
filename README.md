# Ledger — MoMo statement underwriting

**Just want to run it? Read [START-HERE.md](START-HERE.md).** Three commands,
no need to understand anything below.

This file is the technical reference: API contract, design decisions, test
results. Read it when you need to change something.

Two-stage consumer lending assessment for Rwanda.

**Stage 1 — credit bureau.** A TransUnion consumer report is parsed and the
applicant's band decides whether the application proceeds at all. Bands AA, BB,
CC and DD pass; EE through JJ stop, and no statement is processed.

**Stage 2 — MoMo statement.** Only for applicants who cleared stage 1: the
statement PDF becomes a defensible net revenue figure with full exclusion
detail, mandatory disclosures and red flags.

A React console drives both.

```
momo-underwriter/
├── backend/          FastAPI + pdfplumber + pandas
│   ├── app/
│   │   ├── config.py       credit policy: min band, thresholds, lender registry
│   │   ├── crb_parser.py   bureau report PDF -> structured credit data
│   │   ├── crb_analyzer.py the band gate and bureau findings
│   │   ├── parser.py       statement PDF -> normalised transaction rows
│   │   ├── analyzer.py     exclusions, disclosures, red flags
│   │   └── main.py         HTTP layer
│   ├── tests/test_engine.py, test_crb.py
│   └── requirements.txt
├── frontend/         React 18 + Vite
│   ├── src/
│   ├── scripts/render-smoke.jsx
│   └── report.sample.json   captured API response, used by the smoke test
├── deploy/windows/   setup, start, service install, Windows guide
├── deploy/linux/     setup, start, systemd unit, nginx example, Linux guide
├── Dockerfile        full stack: builds the console, serves it from the API
└── compose.yaml
```

## Run it

**Windows:** see [deploy/windows/README.md](deploy/windows/README.md) — one
command sets up a venv, builds the console, and optionally installs a Windows
service. Docker Desktop works too: `docker compose up -d --build`.

**Linux:** see [deploy/linux/README.md](deploy/linux/README.md) — `setup.sh`
builds the venv and console, `install-service.sh` installs a hardened systemd
unit, and `nginx.conf.example` adds HTTPS.

**Docker, any platform:**

```bash
docker compose up -d --build     # console + API on http://localhost:8000
```

**From source:**

```bash
# API — http://localhost:8000  (docs at /docs)
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload

# Console — http://localhost:5173
cd ../frontend
npm install
npm run dev
```

The Vite dev server proxies `/api` to `localhost:8000`, so no CORS setup is
needed locally.

For a single-box deployment, build the console instead and let the API serve
it — one process, one port, no CORS at all:

```bash
cd frontend && npm run build          # writes frontend/dist
cd ../backend && uvicorn app.main:app --port 8000
```

The API mounts `frontend/dist` at `/` when it exists (override the location
with `FRONTEND_DIST`) and falls back to API-only when it does not. The static
mount is registered last, so it never shadows `/api/v1/...` or `/docs`.

To run them apart instead, set `VITE_API_BASE` to the API origin and
`ALLOWED_ORIGINS` on the API to the console origin.

## API

| Method | Path                          | Purpose |
|--------|-------------------------------|---------|
| POST   | `/api/v1/crb/check`           | **Stage 1.** Bureau PDF → band, gate decision, exposure, findings |
| POST   | `/api/v1/applications/assess` | **Enforced flow.** Bureau + statement; the statement is untouched unless the gate opens |
| POST   | `/api/v1/analyze`             | Stage 2 engine on its own. Applies no gate — use `assess` for the real process |
| POST   | `/api/v1/transactions`  | Multipart PDF → normalised rows only |
| GET    | `/api/v1/policy`        | Current default credit policy |
| GET    | `/api/v1/health`        | Liveness |
| GET    | `/`                     | The console, when `frontend/dist` exists |

```bash
curl -F "file=@statement.pdf" \
     -F "min_inflow_count=50" \
     -F "large_txn_threshold=1000000" \
     http://localhost:8000/api/v1/analyze
```

Optional form fields override credit policy per request:
`large_txn_threshold`, `min_inflow_count`, `credit_cap`.

### Response shape

```
account          holder, number, period, pages, closing balance
policy           thresholds and lender registry actually applied
summary          rows[] per month + totals + full_month_run_rate
sensitivity      net with bank / e-wallet credits also stripped
details          self_transfers, loan_inflows (+ledger), large_transactions,
                 bidirectional_counterparties
disclosures      the six mandatory items incl. concentration per month
monthly_flow     inflow / outflow / retained / passthrough_ratio per month
red_flags[]      code, title, severity, metric, summary, evidence
assumptions[]    what the engine took as given
meta             filename, rows_parsed vs rows_declared, elapsed_ms
```

### Errors

| Status | `error` | Meaning |
|--------|---------|---------|
| 415 | `unsupported_type` | Not a PDF |
| 413 | `file_too_large`   | Above `MAX_UPLOAD_MB` (default 25) |
| 422 | `statement_unreadable` | Scanned, encrypted, or not a MoMo statement |

### Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `MAX_UPLOAD_MB` | `25` | Upload ceiling |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins; irrelevant when serving the console from the API |
| `FRONTEND_DIST` | `../frontend/dist` | Where to find the built console |
| `ACCESS_PASSWORD` | unset | When set, every route except `/api/v1/health` requires HTTP Basic auth |
| `ACCESS_USERNAME` | `underwriter` | Username paired with `ACCESS_PASSWORD` |
| `LOG_LEVEL` | `INFO` | Python log level |
| `PYTHONUTF8` | unset | Set to `1` on Windows — the default console codepage cannot encode every character the engine emits. The Linux scripts set it too |

## Statement formats

The engine reads three formats. The operator selects the **provider** in the console;
the format within that provider is detected, because a user knows which institution
issued a file and cannot reliably know which export version it is. Selecting the wrong
provider produces a refusal, not a wrong number.

| Provider | Format | Notes |
|---|---|---|
| MTN MoMo | `mtn_momo_v1` | The app/portal PDF. Names, types, running balance, declared row count. |
| MTN MoMo | `mtn_momo_excel_v1` | Spreadsheet export. MSISDNs on both legs, but no names and no balance. |
| Bank of Kigali | `bk_statement_v1` | Debit/credit columns, narration instead of a counterparty id, booked by date. |

Adding a fourth means writing one file under `backend/app/statements/` with a `sniff()`
and a `parse()`, and registering it. `analyzer.py` is not touched: every adapter returns
the same `ParsedStatement`, so the manipulation logic is shared.

### The same rules, different evidence

What cannot stay identical across formats is which *evidence* a rule uses. Two of the
three carry no usable counterparty name, and the strict self-transfer rule is a
name match. Each adapter therefore declares its `Capabilities`, and the engine adapts:

* **Self-transfer** — name match where names exist; identity (`sender == the account`)
  where they don't, which is exact and needs no near-match review queue. Where neither
  is possible it is reported as unassessable, never as zero.
* **Loans** — a confirmed lender/product name match, an explicit loan transaction type, or a conservative pattern match: a round principal inflow followed by round repayments to the same counterparty within the configured loan-cycle window. A single repayment can qualify when it is explicitly labelled as a repayment/installment; otherwise at least two round repayments are required. Pattern matches are shown as evidence.
  The MoMo spreadsheet export prints `CUSTOM_LOAN_PAYOUT` with no lender name at all.
* **Everything else** — a check that could not run appears under
  `disclosures.capability_notes` with a reason. A check that could never have fired must
  not read as a check that passed. The console renders these as their own panel.

### Agent float accounts

A statement whose header types the account as agent float is not an income document.
On the reference file, 677 of 727 credits are the holder funding his own float from his
own MoMo and his own commission account — 136.7M RWF of credits and close to zero trade
revenue. Such a statement parses and reports normally, but `summary.revenue_assessed` is
`false`, `net_revenue` is `null`, and a severe `FLOAT_ACCOUNT` flag explains why. Set
`block_revenue_on_float_accounts = False` in `config.py` to compute a figure anyway; the
flag stays either way.

### Reconciliation

Each format is checked against whatever it can prove about itself:

* MoMo app statement — declared transaction count versus rows recovered.
* Bank of Kigali — `opening + credits − debits == closing`, which checks money rather
  than cardinality and fails on a single misread digit.
* MoMo spreadsheet export — neither. This is stated in the assumptions rather than
  left to look like a passed check.

Rows an adapter could not use are collected in `rejected_rows` with a page and a reason
instead of being silently dropped, and rows attributable to neither side of the account
are counted under `disclosures.unattributed_count`.

## Design decisions worth knowing

**The engine never guesses.** Self-transfer is a strict sender-name match; near name
matches are surfaced as `possible_for_review` and are not deducted. Loan inflows are
deducted only where the lender/product is confirmed, the transaction is explicitly
labelled as a loan, or a conservative same-counterparty disbursement/repayment pattern
is present. Bank and SACCO flows remain `possible_for_review` with a sensitivity figure.
Rapid reversals are tested both generically and for a same-counterparty match when the
format provides a stable counterparty identifier.

**Partial months are marked, not annualised.** `full_month_run_rate` uses only
months covering 28+ days.

**`parse_complete`** compares rows recovered against the count the statement
declares in its own header. If it is `false`, reconcile before using any figure.

**Stateless.** Nothing is written to disk and nothing is retained after the
response. Statements are personal financial data — if you add persistence, add
retention limits and encryption at rest with it.

## Tests

```bash
# Backend. Golden-file tests skip unless you point at a real statement.
cd backend
pip install pytest
MOMO_FIXTURE=/path/to/statement.pdf pytest -q        # 18 passed
pytest -q                                            # 8 passed, 10 skipped

# Frontend. Server-renders every component against report.sample.json
# and fails on any NaN or undefined reaching the DOM.
cd frontend && npm install && npm run smoke && npm run build
```

The backend suite pins the golden figures below and covers the edge cases that
actually broke during development — see "Bugs found and fixed".

## Bugs found and fixed

* `min_band` accepted any string. A lowercase value from `config.py` — the file
  START-HERE invites a non-technical reader to edit — reached `_rank()`, returned
  `None`, and 500'd every bureau check with a `TypeError`. `Policy.__post_init__` now
  normalises case and rejects unknown bands by name.
* Upload size was checked after `await file.read()`, so an oversized POST was fully
  buffered into memory before being rejected. Reads are now chunked and abort at the
  ceiling.
* Content-type was trusted. `%PDF-` magic bytes are now verified, so a renamed
  spreadsheet is refused with a useful message.
* Transactions attributable to neither party vanished from every figure. They are now
  counted and disclosed.
* `as_dict()` omitted seven thresholds the engine actually reads, under a console
  heading that says "thresholds actually applied". All of them are now included.

Found by running the full flow rather than reading the code:

| Where | Bug | Fix |
|-------|-----|-----|
| `analyzer.py` | An account with **inflows but no outflows** raised `TypeError: unsupported operand type(s) for \|: 'StringArray' and 'StringArray'`. `Series.apply` on an empty string column returns a string dtype, and two of those cannot be combined with `\|`. Any such statement returned HTTP 500. | Added `_mask()`, which returns a real boolean Series on empty frames. Every boolean mask in the module now goes through it. |
| `analyzer.py` | Month coverage was measured between the first and last transaction, so a **quiet month** (activity on the 5th and the 20th) counted as 16 days, was labelled partial, and was silently dropped from the run rate. | `_period_days_by_month` now measures the calendar month against the statement period, falling back to the data span when the period header is missing. |
| `analyzer.py` | `df.loc[inf.index, inf.columns] = inf` copied classifier flags onto the master frame where they were never read, mixing dtypes across a frame that also held outflow rows. | Removed. |
| `components/index.jsx` | The `Evidence` renderer pushed every non-array value through a number formatter, so the string field `amount_clustering_note` rendered as literal **"NaN"** on the agent-float flag. | Type is checked before formatting: strings render as prose, integers as amounts, floats below 1 as percentages. |
| `components/index.jsx` | Arrays of primitives — `iqr: [110000, 500000]` on the profile-deviation flag — were treated as table rows, producing an **empty `<thead><tr></tr>`** and no cells. | Arrays of objects render as tables; arrays of primitives render inline. |
| `components/index.jsx` | A statement with **no complete month** rendered "From  only." | Falls back to guidance to request a longer statement. |
| `components/index.jsx` | A clean statement rendered an **empty red-flag section**, when the brief calls for "none observed". | Added the none-observed state. |
| `parser.py` / `analyzer.py` | Pandas emitted `UserWarning: Could not infer format` per request and fell back to `dateutil` per element. | `format="ISO8601"` pinned, since the parser already normalises timestamps. |

## Verified against a real statement

End-to-end over HTTP against a live server, 62-page statement, 1,747 declared
rows: 1,747 parsed (`parse_complete: true`), 496 inflows / 1,245 outflows /
6 failed.

| Figure | Value |
|--------|------:|
| Gross inflows | 43,685,858 |
| (−) Self-transfers | 0 |
| (−) Loan inflows | 256,000 |
| (−) Large transactions | 9,100,000 |
| **Net revenue** | **34,329,858** |
| Full-month run rate | 12,786,254 |
| Counted inflows | 489 of 496 |
| Distinct senders | 320 (303 individuals) |

Every per-month row reconciles to its own arithmetic, detail tables sum to
their summary lines, and no `NaN` or `Infinity` reaches the JSON.

Error paths verified over HTTP: empty file → 400 `empty_file`; garbage bytes →
422 `statement_unreadable`; wrong MIME type → 415 `unsupported_type`; missing
field → 422; unreachable server → `ApiError(network_unreachable)` surfaced in
the UI. Policy overrides verified: dropping `large_txn_threshold` to 500,000
moves 12 inflows into the exclusion and net to 30,189,858.

Measured wall time **~19 s**, almost
all of it in pdfplumber's table detection — the pandas analysis is milliseconds.

That is fine for a single underwriter uploading one file and watching a
progress bar. It is **not** fine under load or behind a 30 s gateway timeout.
Before production:

- Move `/analyze` to a job queue (Celery/RQ + Redis): `202` with a job id,
  poll or stream the result. The React console's state machine already has the
  `loading` stage to hang this off.
- Or set `laparams=None` and pass explicit `table_settings` to pdfplumber —
  the ruling lines on this statement are clean enough that explicit vertical
  strategy cuts detection time substantially. Benchmark before adopting.
- Cap concurrency per worker; each parse holds the whole page tree in memory.

## Notes for a Spring Boot estate

If this has to live alongside Java services, the clean split is to keep this
service as-is (pdfplumber and pandas have no comfortable JVM equivalent for
this job) and put it behind the existing gateway as an internal service. The
alternative — Apache PDFBox plus a hand-rolled table reconstruction — is
considerably more code for the same result.

## The credit bureau gate

```
bureau report ──► band? ──► AA BB CC DD ──► statement parsed ──► decision
                     │
                     └────► EE FF GG HH II JJ ──► stop, statement never read
```

Set by `Policy.min_band` (default `"DD"`), overridable per request with the
`min_band` form field. `normalise_band` accepts `AA`, `aa`, `A`, `"675 - AA"`
and falls back to mapping the raw score through the report's own band table
when the letters are missing.

**The band is the only thing that opens the gate.** Everything else the report
reveals is a *finding*, and findings have their own severity. A `blocking`
finding sets `gate.recommendation` to `refer` even when the gate is open.

This distinction is not academic. On the reference file the applicant sits in
band **AA** with a stated 1.16% default probability, and simultaneously carries
**10,134,341 RWF outstanding, 3,962,660 in arrears, and an account 65 days past
due**. A band-only decision approves that file. The findings do not:

| Finding | Severity |
|---------|----------|
| `CAP_BREACH` — exposure 3.4× the cap | blocking |
| `ARREARS` — 39% of open balance unpaid | blocking |
| `DELINQUENT` — open account 65 days past due | blocking |
| `SCORE_DIVERGENCE` — band contradicts the account detail | blocking |
| `DETERIORATING`, `ADVERSE_CLASSIFICATION`, `ENQUIRY_VELOCITY` | warning |
| `INSURANCE_ARREARS`, `NO_SECTOR_HISTORY` | informational |

### Bureau parser traps

Five things break naive extraction of this report format, all handled and all
covered by tests:

1. **Collateral blocks share the `N) Trade Sector:` prefix with credit accounts.**
   Splitting the document on that pattern returns 50 "accounts" for a 27-account
   report. Sections are bounded before splitting.
2. **So do bounced cheques.** `Bounced Cheques` and `Payment History` were missing
   from the section bounds, so `PERFORMING ACCOUNTS` ran on into them and the cheque
   record became a zero-value open credit account with no type, class or status. Both
   headings are now bounds, and a second guard skips any block carrying none of
   `Account Type`, `Account Status` or `Principal Amount`, recording it under
   `skipped_account_blocks` rather than dropping it silently.
3. **`PA Open Total Value` is a substring of `NPA Open Total Value`.** An unanchored
   search reads the applicant's non-performing exposure into the performing figure —
   the more flattering of the two. Summary labels are anchored with a lookbehind and
   tolerate the inconsistent spacing before `(RWF)`.
4. **All 23 collateral entries are identical** — one security repeated per
   facility. Summing them turns a 28,743,400 pledge into 661,098,200. They are
   de-duplicated and the removed count is reported.
5. **`Account Status: CURRENT` does not mean healthy.** The 65-day account reads
   CURRENT. Days-in-arrears and classification are used instead; status is
   displayed but never relied on.

### What counts as a monthly obligation

`Scheduled Payments` is the bureau's own figure and is used where it is real. Three
things stop it being taken at face value:

* **A printed `0.00` against a live balance is a placeholder, not an obligation of
  zero.** Treating it as reported both understates the commitment and suppresses the
  fallback below. It is recorded as unreported and flagged.
* **Repayment terms are not always monthly.** `12 MONTHLY` is exact; `120 DAILY` and
  `180 DAILY` are converted at the mean calendar month; a derived term under one month
  (`3 WEEKLY` — ambiguous between three instalments and a three-week cycle) is refused
  with a reason rather than turned into an EMI larger than the principal. The estimate
  is principal ÷ term. No interest rate or fee is invented.
* **The payment's period is never stated.** Where the schedule is non-monthly, reading
  the figure as a monthly EMI is an assumption, raised as `EMI_PERIOD_ASSUMED`.

Non-performing facilities (LOSS, DOUBTFUL, SUBSTANDARD) are excluded from the EMI
deduction: **credit policy counts only open performing (PA) accounts.** This matters
because it does not match a literal reading of "every loan with CURRENT status" -
TransUnion prints `Account Status: CURRENT` on non-performing accounts too. On the
reference report all five open accounts read CURRENT, so a status-based rule would
deduct 235,190 RWF where the classification-based rule deducts 71,067. Excluded
facilities surface under `exposure.unserviced_obligations` behind a blocking
`UNSERVICED_OBLIGATION` finding, and the console states the rule and its direction
on the exposure panel. **Note the direction** - excluding them leaves more revenue
apparently available. Set `deduct_emi_on_non_performing = True` in `config.py` to
follow the status-based reading instead.

## Classification precedence

Exclusion categories are mutually exclusive and applied in a fixed order: own money,
then borrowing, then size. A row in two buckets would be deducted twice and the month
would stop footing.

**Own-wallet products are self-transfers, not loans.** `Mokash Savings` contains
`mokash`, so with MoKash registered as a nano-loan lender every savings withdrawal
read as a disbursement: 531,347 RWF across 82 rows on the reference statement. A
savings wallet cannot belong to a third party, so it is the customer's own money
regardless of whether the sender string matches the holder's name. Own-wallet patterns
are matched before the lender registry.

**SACCOs and Zigama CSS are confirmed lenders.** Credit policy names them as loan
sources, so they are deducted rather than merely reported. A SACCO inflow can also be
a savings withdrawal; deducting is the conservative reading and the one the policy
document specifies.

Every deducted row carries a `basis` naming why it was excluded - `name`, `identity`,
`own_wallet_product`, `repayment_pattern` - or the lender it matched. No amount is
deducted without a reason attached to the line itself.

## Thirty-day normalisation

Calendar months run 28-31 days and a statement's first and last months are usually
partial, so raw monthly totals are not comparable with each other. Every month is
scaled to a 30-day equivalent before averaging:

    net_30d = net_revenue / days_covered * 30

A month covering fewer than `min_days_for_run_rate` days is reported but kept out of
the average. **The floor is 15 days, and it is set by the multiplier rather than by the
day count.** Scaling to 30 days multiplies by 30 / days:

    20 days -> 1.5x     15 days -> 2.0x     10 days -> 3.0x     5 days -> 6.0x

At 15 days - half a month - the factor is exactly 2.0, so no month is ever averaged on
the strength of more than doubling it: the observed period is still at least as long as
the part being inferred. Past that the figure is mostly arithmetic. Ten days of takings
tripled is a projection, not a measurement; on the reference statement, including a
10-day August would have lifted the average 39% on that basis alone.

This is a chosen default rather than a regulatory figure, and one number in `config.py`
changes it. The reasoning is published in the response as `min_days_rationale` and
`max_scale_factor`, and rendered above the months table, so an underwriter reading the
report sees why a month was dropped without reading the source.

`months_considered` lists EVERY month in the statement with its days covered, scale
factor, 30-day figure, status (`averaged` / `excluded_too_short` / `not_in_window`) and
the reason - so which months fell out of the average, and why, is on the report rather
than inferred from a shorter table. The console renders it under "Months considered"
with the threshold stated above the table, and an excluded month raises a
`MONTH_TOO_SHORT` edge case naming the scale-up it avoided.

## Which months are averaged

The last `eligibility_months` **usable** months are averaged, not the last N calendar
months with the unusable ones removed afterwards. Filtering after the window is fixed
silently shrinks the base: on a 12 May - 10 Aug statement the window was Jun/Jul/Aug,
August was dropped as too short, and the average was struck on two months while a
perfectly usable 20-day May sat unused. That cost 46,253 RWF of monthly average.
`months_averaged` and `months_short_of_target` are reported, and a short base raises
`FEWER_MONTHS_THAN_POLICY`.

## Bank and e-wallet credits are a fourth exclusion

Net revenue is gross inflows less **self-transfers, loan inflows, bank/e-wallet
credits and large transactions**. The fourth is an extension beyond the three the
credit policy document names, added on instruction: money arriving from a bank, SACCO,
e-wallet, remittance operator or system account is either the customer moving their own
money in from another institution or a third party settling on their behalf, and
neither is evidence that the account earns.

This replaces the old `sensitivity` block, which reported the same figure as a what-if
alongside net revenue rather than deducting it. That key is gone from the response.
Set `exclude_institutional_credits = False` in `config.py` to go back to counting them
as revenue.

Precedence runs own money, borrowing, institutional, then size, so the categories stay
mutually exclusive and the monthly rows still foot. A SACCO is a confirmed lender, so
a SACCO credit lands in loan inflows, not here.

**Where it cannot be done.** Identifying these credits needs counterparty names. On a
format that carries none, nothing is deducted and the capability note says net revenue
is overstated by whatever they amount to, rather than reporting a clean zero.

One hazard this exposed: `is_institution` treated any non-numeric counterparty id as a
short code. That holds where the id column carries a real identifier, but on a
`derived_name` basis the id is manufactured from the counterparty's name, so every id
is alphabetic. Harmless while institutional credits were only a what-if; once they are
deducted it would have zeroed out the revenue of every statement in that format. The
rule is now gated on `cp_id_basis`.

## Short institution codes need word boundaries

`bok` (Bank of Kigali) sits inside the Rwandan surname NYIRANGOBOKA, and plain substring
matching classified a private individual as a bank - removing her from the
distinct-individual-senders disclosure and putting her money in the institutional
sensitivity figure. Patterns in `_BOUNDED_PATTERNS` (bok, rra, mtn, bpr, ncba, spenn,
sacco, ekash, mfi, im bank, system) now match only on a word boundary. Longer patterns
still match as substrings, where they are unambiguous.

## The statement must belong to the bureau applicant

The console parses a bureau report and then a statement, and nothing used to check that
the two described the same person: a statement could be underwritten against someone
else's exposure, EMI and affordability without a word. `/api/v1/analyze` now accepts
`bureau_name` and `bureau_phones`, which the console always sends. Names are compared on
tokens (the bureau prints `MR TUYISENGE AUDACE` where the statement prints
`Audace TUYISENGE`); the MSISDN is checked against the bureau phone list, which lags
often enough that a miss is a warning rather than a failure. A NAME mismatch raises a
severe red flag and a high-severity `IDENTITY_MISMATCH` edge case at the top of the
report.

## Rounding: every figure derives from the one displayed above it

The eligibility chain rounds at each step and the next step consumes the rounded value.
Carrying unrounded floats forward and rounding only for display leaves consecutive
lines a rupee apart - average 334,446 less EMI 71,067 printed as net inflow 263,378
when the subtraction gives 263,379 - which makes a correct report look broken. The same
rule governs the per-month 30-day column (rounded once, so the column is the mean the
average reports), the per-account EMI contributions, and the fee cap used for band
matching.

## A sanction may not outlive the revenue it came from

Eligibility is computed before the agent-float check nulls the revenue inputs, so
blanking them afterwards used to leave a full sanction standing beside
`revenue_assessed: false` - the report refused to state a revenue figure and then
offered to lend against it. On a float account the sanction is now withdrawn too, with
`blocked_by: "float_account"`, while the gross inflows, red flag and edge cases stay
visible: only the figure that cannot be justified is withheld.

The reconciliation gained a matching cross-block check, `no sanction is offered without
a revenue figure`. Every other check verifies that a figure foots to its own rows; this
one catches two blocks that are each internally correct and contradict each other,
which is what this bug was.

## Eligible amount vs sanctioned amount

Two different numbers, kept apart. The **eligible amount** is what the fee-cap band
supports. The **sanctioned amount** is what is actually offered: the lower of that and
the customer's request. Asking for less than you qualify for is honoured; asking for
more is capped at the band. `sanction_basis` records which side won.

## Two things the console will not let you miss

`reconciliation` re-derives every published figure from the rows beneath it and reports
each check with both values. It runs on the real statement rather than only in tests,
so a number can be trusted without re-adding it by hand; a failure is a calculation bug
and the console says so instead of presenting the figure as final.

`edge_cases` collects every exclusion, assumption, unassessable check and partial month
into one severity-ordered block rendered directly under the verdict. Each item also
appears beside the figure it qualifies - the collection exists so none can be missed by
scrolling past the section that carries it.

## Product eligibility (new customers)

Net revenue is 15% of the average monthly statement net revenue less the open-PA
monthly EMI; the fee cap is 15% of that. The fee cap is rounded once and the sanction
table is matched on the rounded figure, because the band bounds are whole rupees and
matching an unrounded float put the displayed cap and the matched band out of step at
every boundary.

Both ends of every band are inclusive. The top band is **open-ended**: any fee cap of
250,001 RWF or more is eligible for 2,000,000 RWF. A fee cap below 50,000 RWF is not
eligible. The bands are contiguous and do not overlap; bands
are still matched in order, so should a future table overlap, the lower sanction wins and
is reported as `band_overlap_resolved`.

Existing Flow customers (`customer_type=existing`) go through the same steps, but net
revenue is 18% of net inflow and the fee cap is 20% of net revenue, matched against
`existing_loan_eligibility_bands` (500,000 up to 3,000,000; a fee cap of 205,001 or more
sanctions 3,000,000). The policy's gates on on-time-repayment history and completed
facility count are not applied yet.

### Reconciling against the bureau's own summary

The SUMMARY block on page 1 states Open PA, Open NPA and their open values
independently of the account detail pages, which makes it a checksum.
`exposure.portfolio` reports the performing/non-performing split, the bureau's own
your-sector / other-sectors breakdown, and five checks of parsed figures against
stated ones. A mismatch raises blocking `SUMMARY_MISMATCH`, because a mis-sectioned
report makes every downstream figure wrong. This is the check that catches trap 2
above.

### An unreported field is not a zero

Non-performing blocks in this format carry no `Days In Arrears` line at all.
Defaulting it to `0` put "0 days late" on screen beside 954,497 RWF in arrears —
the most reassuring possible reading of a field that was never printed.
`days_in_arrears_reported` now separates absent from zero,
`exposure.worst_days_in_arrears` is `null` when nothing reports it, and
`SCORE_DIVERGENCE` triggers on arrears alone so it does not depend on a field this
format routinely omits.

Also reported, never asserted: `Scheduled Payments` is printed as-is. On the
reference file it reads 1,493,333 against a 7,000,000 principal over 30 monthly
instalments, which implies 44.8M repaid on a 7M loan. It is shown but excluded
from any debt-service calculation until confirmed with the lender.

### Identity linkage

`/applications/assess` compares the two documents and returns `identity_check`.
Names are matched on tokens, because the bureau prints `MR TUYISENGE AUDACE`
where the statement prints `Audace TUYISENGE`. The statement MSISDN is checked
against the bureau phone list — on the reference file it is **not** among the 14
numbers listed, which is common because bureau phone data lags. That is reported
as an unverified linkage, never as evidence of fraud.
