# UI redesign — "Instrument"

Full visual redesign of the console. Backend untouched; no API contract changed.

## Installing

This archive is the **complete project**, with `backend/.venv/` and
`frontend/node_modules/` stripped out. `setup.ps1` recreates both.

Extract it somewhere clean — do not merge it into an older copy — then:

```powershell
cd <wherever>\momo-underwriter\deploy\windows
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\start.ps1
```

`setup.ps1` needs a working internet connection: it runs `pip install` and
`npm install`, and neither dependency tree ships in this archive.

**`frontend/dist/` here is the OLD build.** It is included so the service still
runs if the console build fails. `setup.ps1` overwrites it during the build
step, and it hard-fails if the build does not succeed, so you will not
accidentally end up serving the old design without being told.

## The governing rule

The chrome is greyscale. Colour is reserved for data semantics — if something
on screen is coloured, an underwriter needs to act on it.

| Token      | Meaning                                        |
|------------|------------------------------------------------|
| `--risk`   | severe findings. Nothing else may use this      |
| `--caution`| elevated findings, partial periods, incomplete data |
| `--clear`  | gate cleared, check passed                      |
| `--link`   | interactive affordances                         |

The previous build used red for both excluded amounts and severe risk. An
exclusion is bookkeeping, not risk, so excluded amounts are now set in ink and
the minus is carried by the column header (`(−) Loan inflows`).

## Typography

Two faces, from three. **Instrument Sans** carries every word; **JetBrains
Mono** carries every number, identifier and label. All figures are tabular.

## What changed structurally

**One navigation.** `ReportNav` is deleted. It listed the same seven sections
as the rail, immediately below it, with nothing indicating which was
authoritative. The rail is now the only navigation, grouped into *Findings* and
*Audit trail* — the groups are the hierarchy.

**One identity.** A new `Chrome` component holds applicant name, national ID,
band and statement source in a fixed bar. Previously the name appeared in a
page header and again in a sticky bar 90px below it.

**Three visual weights** instead of nine identical white cards:

1. `Verdict` — inverted decision panel, one per report
2. `Tier label="Findings"` — white panels at full weight
3. `Tier recessed` — audit trail, on a sunken ground

**Signature element:** `BandGauge` in the rail. The AA→JJ ladder decides
whether a statement is ever read, so it stays on screen — a ten-cell scale with
the policy floor drawn as a dashed line and a caret marking the applicant.

## Bugs fixed

- Red-flag title and metric were both inline `<span>`s and ran together on one
  line: *"Agent / float contamination122 cash deposits from 62 depositors"*.
- The revenue waterfall drew zero and near-zero deductions as a 4px dot that
  read as a rendering fault. Zero now draws no bar; small values get a small one.
- `78.6% retained` collided with the net revenue figure.
- The flow ribbon's beige inflow bar was near-invisible against white.
- Rail applicant metadata (national ID, band, existing debt) was 11px grey mono
  on near-black — the least legible treatment on the page applied to the most
  identity-critical data.

## Copy

Engine vocabulary removed from the interface:

| Was            | Now                           |
|----------------|-------------------------------|
| `STEP 2A`      | `Deduction · own accounts`    |
| `STEP 2B` (×2) | `Deduction · borrowing` / `Deduction · inferred` |
| `STEP 2C`      | `Deduction · above threshold` |
| `STEP 3`       | `Policy checks`               |
| `STEP 4`       | `Findings`                    |
| `STEP 5`       | `Confidence`                  |
| `SIGNATURE VIEW` | `Retained vs moved on`      |

Two sections both carried `STEP 2B`, and the numbers appeared on screen in
descending order. `SIGNATURE VIEW` was a design-process label that shipped.

## Files changed

```
frontend/index.html                  font pairing
frontend/src/styles.css              rewritten
frontend/src/App.jsx                 shell, tiers, single nav
frontend/src/components/index.jsx    Chrome, Rail, BandGauge, Tier, Verdict, copy
frontend/src/components/crb.jsx      metric block, exposure figures
frontend/scripts/render-smoke.jsx    extended to cover the new components
```

## Verification

`npm run smoke` renders all 25 components server-side and fails on any `NaN` or
`undefined` in the output. All pass. Print layout was checked under print media:
the decision panel inverts to ink-on-white, the chrome collapses to a header
line, and the rail and action buttons drop out.

## Note on the archive

`backend/.venv/` and `frontend/node_modules/` are already correctly listed in
the respective `.gitignore` files — but they were still present in the zip,
which is why it came to 94 MB. Use `git archive` or exclude those paths when
packaging.
