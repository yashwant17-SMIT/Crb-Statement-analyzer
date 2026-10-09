# Running on Windows

Two supported paths. Pick one.

| | Native Python | Docker Desktop |
|---|---|---|
| Best for | A single underwriter's PC, or a small office server | Anywhere Docker is already the standard |
| Needs | Python 3.11+, Node 18+ | Docker Desktop with WSL2 |
| Starts with Windows | Yes, via NSSM or Task Scheduler | Yes, `restart: unless-stopped` |
| Setup time | ~5 min | ~5 min, mostly image build |

Both serve the console and the API **on one port**, so there is no CORS to configure.

---

## Path 1 — Native Python

### Install

Open PowerShell in `deploy\windows` and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Or double-click `setup.bat`.

This creates `backend\.venv`, installs dependencies, builds the console into
`frontend\dist`, and verifies the engine imports. If Node isn't installed and
you only want the API, pass `-SkipFrontend`.

### Run

```powershell
.\start.ps1                    # http://localhost:8000, this machine only
.\start.ps1 -Lan               # reachable from the network, adds a firewall rule
.\start.ps1 -Lan -Port 8080
.\start.ps1 -Reload            # development, restarts on source change
```

Or double-click `start.bat`, which also opens the browser.

### Start with Windows

```powershell
# As Administrator
winget install NSSM.NSSM
.\install-service.ps1 -Lan
```

NSSM runs uvicorn as a real Windows service: automatic start, restart on crash
with a 5-second backoff, and rotating logs in `logs\`. Manage it with
`nssm stop|restart MomoUnderwriting` or `services.msc`.

No NSSM and can't install it? Use the built-in scheduler:

```powershell
.\install-service.ps1 -UseTaskScheduler
```

That runs at boot as SYSTEM without anyone logging in, but **it will not restart
on crash**. NSSM is the better option where you have the choice.

Remove either with `.\uninstall-service.ps1` (add `-UseTaskScheduler` to match).

---

## Path 2 — Docker Desktop

From the repo root:

```powershell
docker compose up -d --build
```

Console and API at `http://localhost:8000`. The image is multi-stage: Node
builds the console, then a slim Python image serves it. Logs with
`docker compose logs -f`, stop with `docker compose down`.

---

## Windows-specific things worth knowing

**There is no gunicorn on Windows.** It depends on `fcntl`, which is POSIX-only.
Every "run FastAPI in production" guide that reaches for gunicorn does not apply
here. Uvicorn alone is the right answer on this platform; NSSM supplies the
supervision gunicorn would otherwise provide.

**Scaling out means separate processes.** `uvicorn --workers` uses `spawn` on
Windows, which re-imports the app per worker and roughly multiplies memory —
each parse holds a whole page tree. If one underwriter at a time is the reality,
leave it single-process. If not, prefer Docker or a Linux host over fighting
this.

**UTF-8 is not the default console encoding.** Windows still defaults to cp1252
in many consoles, and the engine emits characters it cannot encode — the sigma
in the profile-deviation metric, for one. The start and service scripts set
`PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` for exactly this reason. If you
launch uvicorn by hand, set them yourself.

**Windows Defender will slow the first parse.** Real-time scanning inspects the
uploaded PDF and every wheel during install. A ~19 s parse can stretch past 30 s
on a cold cache. If you are running this on a dedicated box, consider excluding
the install directory in Defender settings.

**PowerShell blocks unsigned scripts by default.** That is why the commands
above pass `-ExecutionPolicy Bypass`, which applies to that one invocation only
and changes nothing system-wide. Do not weaken the machine policy for this.

**Paths with spaces.** Every script quotes its paths, so `C:\Program Files\...`
is fine. Very deep paths can still hit the 260-character limit during
`npm install`; installing under a short root such as `C:\momo` avoids it.

**Port 8000 in use?** Check with `netstat -ano | findstr :8000`, then either
stop the owner or pass `-Port 8080`.

---

## Sharing it for testing (ngrok)

Two windows.

```powershell
# Window 1 - the service, with a password
.\start.ps1 -Password "pick-something-long"

# Window 2 - the public tunnel
.\share.ps1
```

`share.ps1` calls `/api/v1/health` first and **refuses to publish an
unprotected service**. That check is deliberate: the moment this has a public
URL it is serving national IDs, dates of birth, phone numbers and full
transaction histories to anyone holding the link. Override with `-NoPassword`
only for throwaway test data.

First-time ngrok setup:

```powershell
winget install ngrok.ngrok
# sign up at https://dashboard.ngrok.com/signup, copy your authtoken
ngrok config add-authtoken YOUR_TOKEN_HERE
```

ngrok prints a `Forwarding` line - that HTTPS URL is what you share. Send the
username and password through a different channel from the link.

A stable URL, instead of a new one on every restart:

```powershell
# claim your one free static domain in the ngrok dashboard, then
.\share.ps1 -Domain your-name.ngrok-free.app
```

What to expect on the free plan: visitors see an ngrok interstitial and click
through (normal, not an error); allowances are around 1 GB of bandwidth and
20k requests a month, which is thousands of assessments; and the tunnel closes
when you press Ctrl+C or the machine sleeps. Check current limits at
https://ngrok.com/pricing.

Turn sharing off by closing window 2. The service keeps running locally.

### If it has to stay up

ngrok on a laptop is for demos. For anything ongoing, put the Docker image on a
small cloud VM behind a real TLS certificate, and replace the password gate with
proper accounts. The password gate is one shared credential with no audit trail
of who looked at whose data - fine for a test, not fine for production.

---

## Verify the deployment

```powershell
# Should return: status ok
Invoke-RestMethod http://localhost:8000/api/v1/health

# End-to-end with a real statement
$form = @{ file = Get-Item 'C:\path\to\statement.pdf' }
$r = Invoke-RestMethod -Uri http://localhost:8000/api/v1/analyze -Method Post -Form $form
"{0:N0} RWF net, {1} flags" -f $r.summary.totals.net_revenue, $r.red_flags.Count
```

`Invoke-RestMethod -Form` needs PowerShell 7+. On Windows PowerShell 5.1, use
the console in a browser or `curl.exe` (which ships with Windows 10 1803+):

```powershell
curl.exe -F "file=@C:\path\to\statement.pdf" http://localhost:8000/api/v1/analyze
```

Expect roughly 20 seconds for a 62-page statement. That is the parse, not a hang.

---

## Backing out

```powershell
.\uninstall-service.ps1        # removes service and firewall rule
Remove-Item -Recurse backend\.venv, frontend\node_modules, frontend\dist
```

Nothing is written outside the repo folder, and no statement data is persisted
anywhere — the service holds each upload in memory for the length of one
request and discards it.
