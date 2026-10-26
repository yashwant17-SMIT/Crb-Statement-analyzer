# CRB deviation back-test (one-off)

Scores every customer on the intake sheet against the CRB deviation points
D1–D6 and writes an Excel file showing:

- **Could be approved**: rejected today on band (EE–JJ), but every deviation point is Allowed.
- **Would be rejected**: approved today on band (AA–DD), but a deviation point is a Reject.
- **Needs a look**: a point is outside Allowed but not a stated Reject, or a date the rule needs is missing.

This is separate from the live tool. It reads the intake sheet and the CRB PDFs
read-only, and writes only the local `.xlsx`. It reuses the tool's bureau
parser (`backend/app/crb_parser.py`) so reports are read the same way.

## The rules (`checks.py`)

Loans listed from **1 Jan 2024** (the tool's `crb_history_cutoff`) are considered,
except where a rule says otherwise. An account with no listing date is counted.

| # | Point | Allowed | Reject |
|---|---|---|---|
| D1 | Fraud / bounced cheque | None | Any fraud case; any bounced cheque since the cutoff (same rule as the live tool) |
| D2 | Open NPA | None, or RWF 0 owing | Any open NPA owing anything. Open NPAs listed before the cutoff are ignored but named in the detail. |
| D4 | Write-off | None | Any write-off, paid or unpaid, any amount, **any date** |
| D5 | Closed NPA | Matured (listing date + term) **and** cleared before the cutoff | Matured or cleared on/after the cutoff |
| D6 | Open performing accounts, per account | ≤ 15 days past due **and** arrears ≤ RWF 150,000 | > 90 days past due **and** arrears > RWF 50,000 |

- D3 is not in the spec.
- D5: the report has no clearance date, so the **last payment date** stands in for it.
- Anything between Allowed and Reject, or a missing date, is **Review**.
- A customer passes only when every point is Allowed.
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
- Output: `crb_deviation_<date>.xlsx` here, or `--out <file>`. It has the tabs
  Summary, Could be approved, Would be rejected, Needs a look and All customers.
  Every check has a result and a detail column naming the accounts, dates and
  amounts behind it.

`cache/`, `token.json`, `client_secret.json` and the `.xlsx` are git-ignored.
They hold customer data or credentials, so delete them when you're done.

## Tests

```bash
cd tools/crb_deviation && ../../.venv/bin/python -m pytest -q test_checks.py
```
