# Running on Linux

Two supported paths. Pick one.

| | Native (venv + systemd) | Docker |
|---|---|---|
| Best for | A VM or office server you manage directly | Hosts where Docker is already the standard |
| Needs | Python 3.11+, Node 18+, systemd | Docker Engine + Compose plugin |
| Starts at boot | Yes, systemd unit | Yes, `restart: unless-stopped` |

Both serve the console and the API **on one port**, so there is no CORS to configure.

Tested on Ubuntu 26.04 (Python 3.14, Node 22). Any distro with systemd works.

---

## Option A: native

### 1. Prerequisites

```bash
# Debian / Ubuntu
sudo apt update
sudo apt install -y python3 python3-venv nodejs npm

# RHEL / Rocky / Fedora
sudo dnf install -y python3 nodejs npm
```

If your distro's Node is older than 18, install a current LTS from
[NodeSource](https://github.com/nodesource/distributions) or `nvm`. Node is only
needed to build the console; skip it with `--skip-frontend` for API-only.

### 2. Put the code somewhere sensible

The service runs as an unprivileged account that must be able to read the code,
so `/opt` is a better home than someone's home directory:

```bash
sudo mkdir -p /opt/momo-underwriter
sudo chown "$USER" /opt/momo-underwriter
# copy or clone the project into /opt/momo-underwriter, then:
cd /opt/momo-underwriter/deploy/linux
chmod +x *.sh
```

Do **not** copy `backend/.venv` or `frontend/node_modules` from a Windows
machine. They contain Windows binaries. If they are there anyway, `setup.sh`
detects them and rebuilds.

### 3. Set up and try it

```bash
./setup.sh                    # venv, pip install, builds frontend/dist
./start.sh                    # http://localhost:8000, Ctrl+C to stop
```

`start.sh` options:

| Flag | Effect |
|---|---|
| `--port 8081` | Listening port (default 8000) |
| `--lan` | Bind `0.0.0.0` so other machines can reach it |
| `--password SECRET` | Require HTTP Basic auth (username `underwriter`, change with `--username`) |
| `--workers 4` | Uvicorn worker processes |
| `--reload` | Restart on code changes (development only) |

### 4. Install as a service

```bash
sudo ./install-service.sh                 # 127.0.0.1:8000, behind a reverse proxy
sudo ./install-service.sh --lan           # 0.0.0.0:8000, reachable directly
```

This creates a `momo` system user, writes `/etc/systemd/system/momo-underwriter.service`
and a settings file at `/etc/momo-underwriter/env`, then enables and starts the unit.
Re-running it is safe: the unit is rewritten, the settings file is kept.

**Set a password before the service is reachable from anywhere else.** It serves
national IDs, dates of birth, phone numbers and full transaction histories:

```bash
sudoedit /etc/momo-underwriter/env        # uncomment ACCESS_PASSWORD
sudo systemctl restart momo-underwriter
```

Day to day:

```bash
systemctl status momo-underwriter
journalctl -u momo-underwriter -f         # logs
sudo systemctl restart momo-underwriter
sudo ./uninstall-service.sh [--purge]
```

### 5. HTTPS (recommended)

Install the service without `--lan` and put nginx in front.
[`nginx.conf.example`](nginx.conf.example) has a ready config, with
`certbot --nginx` for the certificate. Keep `client_max_body_size` at or above
`MAX_UPLOAD_MB`, or nginx will reject large statements with a 413.

### 6. Google Sheets export (optional)

Every finished assessment is recorded in one Google Sheet, laid out to be
worked in rather than just read:

| Tab | Contents |
|---|---|
| **Register** | One row per assessment, appended and never overwritten. Key figures as typed columns, then underwriter columns (decision dropdown, approved amount, underwriter, decision date, comments) that Ledger never touches. Re-assessing a customer marks their earlier rows `Current = no`; filter on `yes` for one row per customer. |
| **Monthly revenue**, **Bureau accounts**, **Findings** | One row per month, account or finding, keyed by Assessment ID, for filters and pivot tables across all customers. |
| **`<applicant> · <national ID>`** | The formatted report for that customer's latest assessment. Generated: replaced on re-assessment, so record decisions in the Register. |

The console saves automatically when an assessment completes or is blocked at
the bureau, and offers **Save to Sheets** otherwise. Generated ranges carry a
warning-only protection so a stray edit prompts before it lands.

1. In the [Google Cloud console](https://console.cloud.google.com/), create or
   pick a project and enable the **Google Sheets API**.
2. **IAM & Admin → Service accounts → Create service account**. No roles are
   needed. Open it, **Keys → Add key → JSON**, and download the key.
3. Create the spreadsheet and **Share** it with the service account's
   `client_email` (from the JSON) as an **Editor**.
4. Install the key and point the service at it:

   ```bash
   sudo install -o root -g momo -m 640 ~/Downloads/<key>.json /etc/momo-underwriter/google-service-account.json
   sudo nano /etc/momo-underwriter/env   # uncomment and fill GOOGLE_SHEETS_ID and GOOGLE_SERVICE_ACCOUNT_FILE
   sudo systemctl restart momo-underwriter
   ```

   `GOOGLE_SHEETS_ID` is the part of the sheet URL between `/d/` and `/edit`.

The key grants write access to anything shared with that account, so keep it
out of the project folder and out of version control.

### Updating

```bash
cd /opt/momo-underwriter
# pull or copy the new code
deploy/linux/setup.sh
sudo systemctl restart momo-underwriter
```

---

## Option B: Docker

```bash
# Docker Engine: https://docs.docker.com/engine/install/
cd /opt/momo-underwriter
docker compose up -d --build              # http://localhost:8000
docker compose logs -f
```

Set `ACCESS_PASSWORD` under `environment:` in `compose.yaml` (or in a `.env`
file next to it) before exposing the port. To keep it local-only behind nginx,
change the port mapping to `"127.0.0.1:8000:8000"`.

---

## Differences from the Windows scripts

| Windows | Linux |
|---|---|
| `setup.ps1` / `setup.bat` | `setup.sh` |
| `start.ps1` / `start.bat` | `start.sh` (default port 8000, not 8001) |
| `install-service.ps1` (NSSM / Task Scheduler) | `install-service.sh` (systemd) |
| Windows Firewall rule | `ufw allow 8000/tcp` (printed as a hint; added automatically by `install-service.sh --lan` when ufw is active) |
| Log files in `logs\` | `journalctl -u momo-underwriter` |
| `share.ps1` (ngrok) | Run `ngrok http 8000` directly, with a password set |
| `PYTHONUTF8=1` required | Set anyway for consistency; Linux locales are UTF-8 already |
| Single process | `--workers N` supported (default 2 under systemd) |

## Troubleshooting

**`\r: command not found` or `bad interpreter`** – the scripts were saved with
Windows line endings. `sed -i 's/\r$//' *.sh` fixes them.

**pip tries to compile pandas/numpy** – your Python is newer than the pinned
wheels. Install a supported one and point setup at it: `PYTHON=python3.12 ./setup.sh`.

**`User 'momo' cannot read ...`** – the code is in a directory the service
account cannot enter (often `/home/you`). Move it to `/opt` as in step 2.

**Console shows but uploads fail with 413** – raise `client_max_body_size` in nginx.
