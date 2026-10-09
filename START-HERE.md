# START HERE

You do not need to understand any of the files to run this.

Download the whole `momo-underwriter` folder, keep its structure intact, and run
three commands. That's it.

---

## Deploy on Windows

**1. Install the two prerequisites** (skip any you already have)

- Python 3.11 or newer — https://python.org/downloads
  During install, tick **"Add python.exe to PATH"**. This is the step people miss.
- Node.js 18 or newer (LTS) — https://nodejs.org
  Accept every default.

Confirm both are visible. Open PowerShell and run:

```powershell
python --version
node --version
```

If either says "not recognized", close PowerShell, reopen it, and try again.
Still failing means PATH wasn't set — reinstall and tick the PATH box.

**2. Set it up** (once, takes about 3 minutes)

```powershell
cd C:\path\to\momo-underwriter\deploy\windows
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Or just double-click `setup.bat`.

You should see `Setup complete.` If you see a red ERROR line, it tells you
exactly what is missing.

**3. Start it**

```powershell
.\start.ps1
```

Or double-click `start.bat`, which also opens your browser.

**4. Use it**

Open http://localhost:8000. The process is two steps, in this order:

1. **Drop the credit bureau report.** Takes about 4 seconds. You get the band,
   the gate decision, existing debt and arrears.
2. **If the band clears, pick the provider and drop the statement.** Choose which
   institution issued the file — MTN MoMo or Bank of Kigali — then upload it. Takes
   about 20 seconds. Pick the wrong provider and the upload is refused rather than
   misread.

If the band is below the minimum, step 2 never appears and no statement is
processed.

To stop it, press Ctrl+C in the PowerShell window.

---

## That's the whole deployment

Everything below is optional.

### Make it start automatically with Windows

Only needed if this runs on a shared machine that must survive reboots.

```powershell
# Right-click PowerShell, "Run as Administrator"
winget install NSSM.NSSM
cd C:\path\to\momo-underwriter\deploy\windows
.\install-service.ps1
```

Undo with `.\uninstall-service.ps1`.

### Let colleagues on the office network use it

```powershell
.\start.ps1 -Lan
```

It prints the address to share, e.g. `http://192.168.1.42:8000`.

### Use Docker instead

If Docker Desktop is already installed, this replaces steps 1–3 entirely:

```powershell
cd C:\path\to\momo-underwriter
docker compose up -d --build
```

Same result at http://localhost:8000. You do not need Python or Node installed.

---

## Which files matter?

**Files you run** — only these three, all in `deploy\windows\`:

| File | When |
|------|------|
| `setup.ps1` | Once, first time |
| `start.ps1` | Every time you want to use it |
| `install-service.ps1` | Only if it must start with Windows |

**Files that do the work** — you never open these, but don't delete them:

| File | What it does |
|------|--------------|
| `backend/app/statements/` | Reads a PDF into transaction rows — one adapter per provider format |
| `backend/app/parser.py` | Thin re-export of the above, kept so older imports still work |
| `backend/app/analyzer.py` | Calculates net revenue, exclusions, red flags |
| `backend/app/main.py` | The web server |
| `backend/app/crb_parser.py` | Reads the credit bureau PDF |
| `backend/app/crb_analyzer.py` | The band gate and bureau findings |
| `backend/app/config.py` | **The one file you might edit** — the minimum band, thresholds and lender names |
| `frontend/src/` | The web page |

**The one file worth editing.** `backend/app/config.py` holds your credit
policy: the **minimum bureau band** (`min_band = "DD"`), the large-transaction
threshold, the minimum-transaction count, the credit cap, and the list of known
lenders. Change a value there, restart, done.

To tighten the gate to BB and above, set `min_band = "BB"`. Bands must be one of
AA, BB, CC, DD, EE, FF, GG, HH, II, JJ — anything else stops the service at startup
with a message naming the valid values, rather than failing on the first upload.

**Everything else is supporting cast** — build config, tests, `.gitignore`,
Docker files, documentation. Safe to ignore.

---

## Check it worked

```powershell
Invoke-RestMethod http://localhost:8000/api/v1/health
```

Expected: `status: ok`.

---

## If something goes wrong

| Symptom | Cause | Fix |
|---------|-------|-----|
| `python : not recognized` | Python not on PATH | Reinstall Python, tick "Add python.exe to PATH" |
| `running scripts is disabled` | PowerShell blocks unsigned scripts | Use the full command with `-ExecutionPolicy Bypass`, or double-click the `.bat` |
| `Virtual environment not found` | `setup.ps1` hasn't run yet, or failed | Run step 2 again and read the error |
| Port 8000 already in use | Something else has it | `.\start.ps1 -Port 8080` |
| Page loads but upload fails | Not the original PDF | Scans and screenshots have no text layer. Use the file from the MoMo app |
| "Does not look like a credit bureau report" | Statement dropped in the bureau slot | They are different documents. Bureau report first, statement second |
| Upload seems to hang | It hasn't | A 62-page statement takes ~20 seconds. Windows Defender can add more on first run |

Nothing is written outside this folder, and no statement data is saved anywhere.
Each upload lives in memory for one request and is discarded.
