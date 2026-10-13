# Intake sheet via Apps Script

Ops queue applications as rows in a Google Sheet. A script bound to that
sheet fetches each row's PDFs from Drive and sends them to Ledger. Ledger
holds **no Google credentials**: the script runs as the sheet owner and can
open exactly the files they can.

```
Intake sheet ── Ledger ▸ Assess … ──▶ Apps Script (runs as the owner)
                                       │ DriveApp: download the PDFs
                                       │ read the results workbook's tabs + Register
                                       ▼
                         HTTPS POST /api/v1/intake/assess  (X-Intake-Token)
                                       │ bureau gate → statement analysis
                                       │ → the Sheets write that records it
                                       ▼
   Apps Script applies that write to the results workbook, then sets the row:
   Ledger status · Ledger result · Ledger assessed at · Ledger report (link)
```

## Where the output goes

All output goes to one **results workbook**, laid out exactly as Ledger's
Sheets export lays it out:

| Tab | Contents |
|---|---|
| **Register** | One row per assessment, never overwritten, with underwriter columns (decision dropdown, approved amount, underwriter, date, comments). Re-assessing a customer marks their earlier rows `Current = no`. |
| **Monthly revenue**, **Bureau accounts**, **Findings** | One row per month, account or finding, keyed by Assessment ID. |
| **`<applicant> · <national ID>`** | The formatted report for the customer's latest assessment. |

The intake sheet only gets the four Ledger columns. **Ledger report** links
to the customer's tab in the results workbook.

The results workbook is set by `RESULTS_SHEET_ID`. Leave it unset to put the
results tabs into the intake spreadsheet itself, next to the applications.
Ledger chooses what to write; the script just applies it, so the layout is
the same as the export and changes with it.

## The sheet

One row per application. Columns are found by header, in any order, and
extra columns are ignored.

| Column | Accepted headers |
|---|---|
| Customer *(optional)* | Customer, Customer name, Name, Applicant |
| Bureau report | CRB, CRB link, CRB report, Bureau report, Credit report |
| Statement(s) | Any header containing "statement". Several columns, or several links in one cell, are combined. |
| Customer type | Customer type, Type: `New` or `Existing` |
| Requested amount | Requested amount, Amount, Loan amount: `1000000`, `1,000,000` or `RWF 1 000 000` |
| Provider *(optional)* | Provider, Statement provider: `MTN MoMo` or `Bank of Kigali`. Empty means detect. |

Links can be pasted URLs, hyperlinked text, `=HYPERLINK(...)` or Drive smart
chips. Only Google Drive file links are used. If a smart chip shows
*"Not a Google Drive file link"* or *"No CRB link"*, paste the plain URL.

On first run the script adds **Ledger status**, **Ledger result**,
**Ledger assessed at** and **Ledger report** after your last column. A row
is pending while Ledger status is empty, so clear it to run that row again.
Rows with a problem (a missing link, an unreadable amount) are marked
**Error** with the reason and are not retried until you clear the status.

## Setup

### 1. Server

Pick a long random token and set it on the server:

```bash
openssl rand -hex 32
sudo nano /etc/momo-underwriter/env      # INTAKE_TOKEN=<that value>
sudo systemctl restart momo-underwriter
```

The intake route is off while `INTAKE_TOKEN` is unset. It checks this token
instead of the console password (`ACCESS_PASSWORD`), because a script cannot
answer a browser login prompt. All other routes still require the password.

### 2. A fixed public HTTPS address

Google's servers run the script, so Ledger must be reachable from the
internet over HTTPS, at an address that does not change. Use either of these.
Neither opens a port on your router.

**Cloudflare named tunnel** (needs a domain on Cloudflare; the tunnel is free)

```bash
cloudflared tunnel login
cloudflared tunnel create ledger
cloudflared tunnel route dns ledger ledger.yourdomain.com
cat > ~/.cloudflared/config.yml <<EOF
tunnel: ledger
credentials-file: /home/$USER/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: ledger.yourdomain.com
    service: http://localhost:8000
  - service: http_status:404
EOF
sudo cloudflared service install          # starts on boot, reconnects by itself
```

Use a *named* tunnel. The quick `cloudflared tunnel --url` form gets a new
random address on every restart.

**Tailscale Funnel** (free, no domain needed)

```bash
sudo tailscale up
sudo tailscale funnel --bg 8000           # https://<machine>.<tailnet>.ts.net
```

The address stays the same as long as the machine keeps its name.

### 3. The results workbook

Create an empty spreadsheet for the results, or reuse the one Ledger's
Sheets export already writes to. The person who authorises the script needs
**edit** access to it. Copy its ID, the part of the URL between `/d/` and
`/edit`. To keep everything in the intake spreadsheet instead, skip this step.

### 4. The script

1. Open the intake sheet → **Extensions → Apps Script**.
2. Replace `Code.gs` with [`Code.gs`](Code.gs) from this folder.
3. **Project Settings** → tick *Show "appsscript.json" manifest file* →
   replace it with [`appsscript.json`](appsscript.json). This turns on the
   Google Sheets advanced service the script uses to apply Ledger's write,
   and limits Drive access to read-only.
4. **Project Settings → Script properties**:

   | Property | Value |
   |---|---|
   | `LEDGER_URL` | `https://ledger.yourdomain.com` (no trailing path) |
   | `LEDGER_TOKEN` | the server's `INTAKE_TOKEN` |
   | `RESULTS_SHEET_ID` | *(optional)* the results workbook; default is this spreadsheet |
   | `INTAKE_TAB` | *(optional)* the tab with the applications; default is the first tab |

5. Reload the sheet. A **Ledger** menu appears. Run **Ledger → Check
   connection** and approve the permissions Google asks for: edit
   spreadsheets you can access, read Drive files, and call an external
   service.

## Use

- **Ledger → Assess selected rows**: select any cells in the rows you want.
- **Ledger → Assess all pending rows**: every row with an empty status.
- **Ledger → Turn on automatic runs**: pending rows every 10 minutes, with
  nobody clicking.

Each run stops at about 4½ minutes, under Apps Script's 6-minute limit. The
rows it didn't reach stay pending for the next run.

Runs from one intake sheet never overlap. Don't point two intake sheets, or
an intake sheet and the console's own Sheets export, at the same results
workbook: two writers at once can both add a row for the same customer
without marking the other superseded.

## Who owns it

The script acts as whoever authorised it. If that person leaves or loses
access to the PDF folder or the results workbook, runs fail with
*"check it is shared with …"* or *"cannot open the results workbook"*.
Authorise it from a shared team account, keep the PDFs in a shared drive,
and turn automatic runs on from that account.

## Testing without Google

```bash
node apps-script/test/run.mjs                       # fakes for Sheets, Drive and Ledger
LEDGER_LIVE=http://127.0.0.1:8000 LEDGER_TOKEN=... \
  node apps-script/test/run.mjs                     # posts to a running Ledger with curl
```
