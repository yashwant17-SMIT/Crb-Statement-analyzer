# CRB deviation back-test (one-off)

Scores every customer on the intake sheet against the CRB deviation points
D1–D6 and writes an Excel file showing:

- **Could be approved**: rejected today on band (EE–JJ), but every deviation point is Allowed.
- **Would be rejected**: approved today on band (AA–DD), but a deviation point is a Reject.
- **Refer to credit head**: no point is a Reject, but at least one is Refer.
- **Needs a look**: the report lacks a date or figure a rule needs.

This is separate from the live tool. It reads the intake sheet and the CRB PDFs
read-only, and writes only the local `.xlsx`. It reuses the tool's bureau
parser (`backend/app/crb_parser.py`) so reports are read the same way.

## The rules (`checks.py`)

Loans listed from **1 Jan 2024** (the tool's `crb_history_cutoff`) are considered,
except where a rule says otherwise. An account with no listing date is counted.

| # | Point | Allowed | Refer (credit head) | Reject |
|---|---|---|---|---|
| D1 | Fraud / bounced cheque | None | – | Any fraud case; any bounced cheque since the cutoff (same rule as the live tool) |
| D2 | Open NPA | None, or RWF 0 owing | – | Any open NPA owing anything. Open NPAs listed before the cutoff are ignored but named in the detail. |
| D4 | Write-off | None | – | Any write-off, paid or unpaid, any amount, **any date** |
| D5 | Closed NPA | Cleared before the cutoff, and not maturing (listing date + term) on/after it | Matured or cleared on/after the cutoff, loan (principal) ≤ RWF 50,000 | The same, loan > RWF 50,000 |
| D6 | Open performing accounts, per account | 0–15 days past due **and** arrears ≤ RWF 150,000 | 16–90 days past due **and** arrears ≤ RWF 100,000 | Anything beyond Refer |

- D3 is not in the spec.
- D5: the report has no clearance date, so the **last payment date** stands in for it.
  The 50,000 limit is on the loan's principal: the bureau prints 0 arrears on every closed account.
  A loan cleared before the cutoff is Allowed even when its maturity can't be
  worked out (the detail then shows the printed repayment terms). Only a missing
  clearance date is **Review**.
- D6: arrears with no days past due printed is **Review**, or Reject above RWF 150,000.
- A customer passes only when every point is Allowed. The overall result is the
  worst point, in the order Allowed, Refer, Review, Reject.
- The D6 limits are constants at the top of `checks.py`.

## One-time setup

**1. Google sign-in client.** Whoever sets this up does it once; the person
with access to the sheet and the CRB files then just signs in.

1. In [Google Cloud console](https://console.cloud.google.com/), create or choose a project.
2. Enable the **Google Sheets API** and the **Google Drive API**.
3. Set up the **OAuth consent screen**: choose External and leave it in Testing.
   Add the email of the person who will sign in under **Test users**.
4. Go to **Credentials**, then **Create credentials → OAuth client ID → Desktop app**.
   Download the JSON and save it here as `client_secret.json`.

**2. Python.** From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt -r tools/crb_deviation/requirements.txt
```

## Quick test on local PDFs (no Google)

```bash
.venv/bin/python tools/crb_deviation/run.py --pdf path/to/crb1.pdf path/to/crb2.pdf
```

This prints the band, the group and each check with its detail. Use it to
check the rules on a few reports whose outcome you already know.

## Run

```bash
.venv/bin/python tools/crb_deviation/run.py --sheet "<intake sheet URL>" --tab "<tab name>"
```

- A browser opens to sign in. Use the account that can open the sheet and the
  CRB files. The sign-in is kept in `token.json`, so later runs skip it.
- The CRB column is found by header, as the intake Apps Script does it, and the
  header row may sit under a title. If it isn't found, pass `--crb-column "<header>"`.
- Downloaded PDFs are cached in `cache/`, so a rerun (e.g. after changing a
  threshold) does not download them again.
- Output: `crb_deviation_<date>.xlsx` here, or `--out <file>`. See below.

## The output, for the ops team

| Tab | What's in it |
|---|---|
| **Summary** | What the file is, the count for each outcome with what it means and what to do, why customers couldn't be assessed, and the checks in plain words |
| **Could be approved** | Rejected on band today, passes every check |
| **Refer to credit head** | No check fails, but an overdue loan is within the Refer limits |
| **Would be rejected** | Approved on band today, fails a check |
| **Manual check** | The CRB report is missing a date or figure a check needs |
| **Could not assess** | The exact reason for each row (e.g. the file isn't shared with the account the report ran as, it's an image not a PDF, the cell says "No record found") and what to do |
| **All customers** | Every row: outcome, what to do, **Why** (each failing check, loan by loan, with dates and amounts), and ✓ Pass / ⚠ Refer / ? Check / ✗ Fail per check |
| **Loan details** | Every loan on each CRB report, with the loans that affected the result flagged |

Outcome and check cells are coloured, and every tab has filter arrows on its header.

`cache/`, `token.json`, `client_secret.json` and the `.xlsx` are git-ignored.
They hold customer data or credentials, so delete them when you're done.

## Tests

```bash
cd tools/crb_deviation && ../../.venv/bin/python -m pytest -q test_checks.py
```
