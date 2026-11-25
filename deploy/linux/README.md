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
`certbot --nginx` for the certificate. `client_max_body_size` caps the whole
request, so keep it above `MAX_UPLOAD_MB` times the number of files one
assessment sends, or nginx rejects large uploads with a 413.

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

### 7. Intake sheet (optional)

Applications can be queued in a Google Sheet, one row per application with
Drive links to the PDFs, and assessed from a **Ledger** menu in that sheet.
The script runs as the sheet owner, so the server needs no Google
credentials for this. It does need a fixed public HTTPS address. Setup is in
[apps-script/README.md](../../apps-script/README.md).

### Updating

```bash
cd /opt/momo-underwriter
# pull or copy the new code
deploy/linux/setup.sh
sudo systemctl restart momo-underwriter
```

---

## Option B: Docker

The server builds the image from a checkout and runs it behind nginx. To build
once on a workstation and only pull on the server, see
[`../docker/README.md`](../docker/README.md) instead.

### 1. Before you start

- A DNS **A record** for your domain (e.g. `underwriting.example.com`) pointing
  at the server's public IP. certbot needs it to issue the certificate.
- Ports 80 and 443 open in any cloud firewall or security group. Port 8000
  stays closed.

### 2. Docker and the code

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && sudo systemctl enable docker   # log out and back in

# The repo is private: give the server a read-only deploy key
ssh-keygen -t ed25519 -f ~/.ssh/ledger_deploy -N ''
cat ~/.ssh/ledger_deploy.pub     # GitHub repo -> Settings -> Deploy keys -> Add (leave write off)
GIT_SSH_COMMAND='ssh -i ~/.ssh/ledger_deploy' \
  git clone git@github.com:yashwant17-SMIT/Crb-Statement-analyzer.git ~/crb-statement-analyzer
git -C ~/crb-statement-analyzer config core.sshCommand 'ssh -i ~/.ssh/ledger_deploy'
```

### 3. Settings

```bash
cd ~/crb-statement-analyzer
cp deploy/docker/env.example .env && chmod 600 .env
nano .env
```

| Setting | Value |
|---|---|
| `ACCESS_PASSWORD` | Required. The console login |
| `ALLOWED_ORIGINS` | `https://underwriting.example.com` |
| `INTAKE_TOKEN` | `openssl rand -hex 32`, only if the Apps Script intake is used |
| `GOOGLE_SHEETS_ID`, `GOOGLE_SERVICE_ACCOUNT_FILE` | Only for Save to Sheets; see below |
| `HOST_PORT` | Only if 8000 is taken; then change `proxy_pass` in nginx to match |

For Save to Sheets, copy the service-account key in and let the container's
user (uid 10001) read it:

```bash
mkdir -p secrets
cp /path/to/service_account.json secrets/service-account.json
sudo chown 10001 secrets/service-account.json && chmod 600 secrets/service-account.json
# .env: GOOGLE_SERVICE_ACCOUNT_FILE=/run/secrets/service-account.json
```

### 4. Build and start

```bash
docker compose up -d --build
docker compose ps                                   # wait for "healthy"
curl -s http://127.0.0.1:8000/api/v1/health
```

### 5. nginx and HTTPS

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp deploy/linux/nginx.conf.example /etc/nginx/sites-available/crb-statement-analyzer
sudo nano /etc/nginx/sites-available/crb-statement-analyzer    # server_name; port if HOST_PORT changed
sudo ln -s /etc/nginx/sites-available/crb-statement-analyzer /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d underwriting.example.com --redirect
```

certbot adds the TLS lines, redirects HTTP to HTTPS, and renews the
certificate by itself (`sudo certbot renew --dry-run` checks that).

[`nginx.conf.example`](nginx.conf.example) is ready as it is: it proxies to
`127.0.0.1:8000`, allows 100 MB per request (one assessment can carry
several statements), waits 180 s for parsing, and adds basic security headers.

### 5b. nginx already runs in Docker

When another site on the server is already served by nginx in a container,
skip step 5: that container holds ports 80 and 443, and inside it
`127.0.0.1:8000` is the container itself, not Ledger. Instead, put Ledger on
its network and add a server block to it.

Find the container, its network, and where its config and certificates live:

```bash
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}' | grep -Ei 'nginx|:443'
docker inspect <nginx-container> -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
docker inspect <nginx-container> -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

Join the network, in `.env`, then recreate the container:

```bash
COMPOSE_FILE=compose.yaml:compose.proxy.yaml
PROXY_NETWORK=<network from above>
```
```bash
docker compose up -d
docker exec <nginx-container> wget -qO- http://crb-statement-analyzer:8000/api/v1/health   # or curl
```

Add the site: copy [`nginx.docker.conf.example`](nginx.docker.conf.example)
into the host folder mounted at the container's `/etc/nginx/conf.d` (from the
mounts above), set `server_name`, and make the TLS lines match how the
existing site gets certificates:

| Existing setup | Certificate for the subdomain |
|---|---|
| Certificate files in a mounted folder (e.g. `/etc/ssl/<domain>.crt`), shared by several subdomains | Probably a wildcard: check with the `openssl` command below, and if it lists `*.yourdomain.com`, point `ssl_certificate*` at the same files |
| A `certbot` container sharing `/etc/letsencrypt` and `/var/www/certbot` with nginx | Comment out the `443` block, reload, then `docker compose run --rm certbot certonly --webroot -w /var/www/certbot -d ledger.example.com` in that stack, uncomment, reload |
| certbot on the host, certificates mounted into the container | Comment out the `443` block, reload, `sudo certbot certonly --webroot -w <host folder mounted at /var/www/certbot> -d ledger.example.com`, uncomment, reload |
| `nginx-proxy` with `acme-companion` | No config file: add `VIRTUAL_HOST`, `VIRTUAL_PORT=8000` and `LETSENCRYPT_HOST` set to the subdomain to Ledger's `environment` |
| Cloudflare in front, origin certificate | Point `ssl_certificate*` at the origin certificate the existing site uses, if it covers `*.yourdomain.com` |

```bash
# Which names a certificate file covers (run on the host, against the mounted file)
openssl x509 -in <host path>.crt -noout -subject -enddate -ext subjectAltName

docker exec <nginx-container> nginx -t && docker exec <nginx-container> nginx -s reload
```

The DNS A record for the subdomain points at the same server IP as the
existing site. Nothing else on that site changes.

### 6. Firewall

```bash
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
```

The container is bound to `127.0.0.1`, which is what keeps port 8000 off the
internet: Docker's port rules bypass ufw.

### 7. Check it and connect the intake sheet

```bash
curl -s https://underwriting.example.com/api/v1/health                 # 200, no password
curl -s -o /dev/null -w '%{http_code}\n' https://underwriting.example.com/   # 401: password on
```

In the Apps Script **Script properties**, set `LEDGER_URL` to
`https://underwriting.example.com` and `LEDGER_TOKEN` to the `INTAKE_TOKEN`
from `.env`, then run **Ledger → Check connection**. No ngrok is needed.

### Updates and rollback

```bash
cd ~/crb-statement-analyzer
git pull && docker compose up -d --build          # update; a plain restart keeps the old image
git checkout <commit> && docker compose up -d --build   # roll back
docker compose logs -f                            # logs
docker compose up -d                              # after editing .env
```

The container's filesystem is read-only; the API never writes to disk.

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

**Console shows but uploads fail with 413** – raise `client_max_body_size` in nginx; it caps the whole request, not each file.
