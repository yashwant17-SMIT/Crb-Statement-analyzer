#!/usr/bin/env bash
# Install the underwriting service as a systemd unit so it starts at boot and
# restarts on crash. Run as root (sudo), after ./setup.sh.
#
# Usage:
#   sudo ./install-service.sh [--port 8000] [--lan] [--workers 2]
#                             [--user momo] [--name momo-underwriter]
#
#   --lan      Bind 0.0.0.0 so other machines can reach it directly. Leave it
#              off when nginx/Caddy on this host proxies to it (recommended).
#   --workers  Uvicorn worker processes. Default 2.
#   --user     System account the service runs as. Created if missing.
#              Default "momo".
#
# Secrets (ACCESS_PASSWORD) and other settings live in /etc/<name>/env, which
# this script creates once and never overwrites.
# Re-run under bash if started with sh/dash (e.g. "sh setup.sh").
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

PORT=8000
LAN=0
WORKERS=2
SVC_USER=momo
NAME=momo-underwriter

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)    PORT="$2"; shift 2 ;;
        --lan)     LAN=1; shift ;;
        --workers) WORKERS="$2"; shift 2 ;;
        --user)    SVC_USER="$2"; shift 2 ;;
        --name)    NAME="$2"; shift 2 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

fail() { printf '\n\033[31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Run as root: sudo $0 $*"
command -v systemctl >/dev/null || fail "systemd not found. Use ./start.sh under your own supervisor, or Docker."

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="$ROOT/backend"
VENV_PY="$BACKEND/.venv/bin/python"
ENV_DIR="/etc/$NAME"
ENV_FILE="$ENV_DIR/env"
UNIT="/etc/systemd/system/$NAME.service"

[[ -x "$VENV_PY" ]] || fail "Virtual environment not found. Run ./setup.sh first."

# --- service account -------------------------------------------------------
if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
    echo "Created system user '$SVC_USER'."
fi

# The account only needs to read the code. Fail now rather than at first boot.
if ! runuser -u "$SVC_USER" -- test -r "$BACKEND/app/main.py" -a -x "$VENV_PY"; then
    fail "User '$SVC_USER' cannot read $BACKEND.
       Move the project somewhere readable (e.g. /opt/momo-underwriter) and
       re-run setup.sh there, or grant access:  sudo chmod -R o+rX $ROOT
       (every parent directory also needs o+x)."
fi

# --- settings --------------------------------------------------------------
BIND=127.0.0.1
[[ "$LAN" == 1 ]] && BIND=0.0.0.0

mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<EOF
# Settings for $NAME. Restart after editing:  sudo systemctl restart $NAME
MAX_UPLOAD_MB=25
LOG_LEVEL=INFO
ALLOWED_ORIGINS=http://localhost:$PORT

# Require HTTP Basic auth on every route except /api/v1/health.
# Set this before the service is reachable from any other machine.
#ACCESS_USERNAME=underwriter
#ACCESS_PASSWORD=change-me

# Google Sheets export: one tab per customer. The key file must be readable by
# '$SVC_USER' (chown root:$SVC_USER, chmod 640) and the spreadsheet shared with
# the service account's client_email as an Editor. See deploy/linux/README.md.
#GOOGLE_SHEETS_ID=the-id-between-/d/-and-/edit-in-the-sheet-url
#GOOGLE_SERVICE_ACCOUNT_FILE=$ENV_DIR/google-service-account.json
EOF
    echo "Wrote $ENV_FILE"
else
    echo "Keeping existing $ENV_FILE"
fi
chown root:"$SVC_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

# --- unit ------------------------------------------------------------------
cat > "$UNIT" <<EOF
[Unit]
Description=MoMo Statement Underwriting (API + console)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$BACKEND
Environment=PYTHONUTF8=1
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_PY -m uvicorn app.main:app --host $BIND --port $PORT --workers $WORKERS --proxy-headers
Restart=on-failure
RestartSec=5
# Uploads are parsed in memory; the service writes nothing to disk.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOF
echo "Wrote $UNIT"

systemctl daemon-reload
systemctl enable "$NAME" >/dev/null
systemctl restart "$NAME"

if [[ "$LAN" == 1 ]] && command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
    ufw allow "$PORT"/tcp >/dev/null && echo "ufw: allowed $PORT/tcp"
fi

# --- health check ----------------------------------------------------------
echo
echo -n "Waiting for the service"
for _ in $(seq 1 20); do
    if "$VENV_PY" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$PORT/api/v1/health', timeout=2)" 2>/dev/null; then
        printf '\n\033[32mService %s is up on port %s.\033[0m\n' "$NAME" "$PORT"
        break
    fi
    echo -n .
    sleep 1
done
systemctl is-active --quiet "$NAME" || { echo; journalctl -u "$NAME" -n 30 --no-pager; fail "Service failed to start."; }

echo
echo "  Console:   http://localhost:$PORT"
echo "  Settings:  sudoedit $ENV_FILE"
echo "  Status:    systemctl status $NAME"
echo "  Logs:      journalctl -u $NAME -f"
echo "  Restart:   sudo systemctl restart $NAME"
echo "  Remove:    sudo ./uninstall-service.sh"
