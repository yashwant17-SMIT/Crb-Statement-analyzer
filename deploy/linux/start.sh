#!/usr/bin/env bash
# Run the MoMo underwriting service in the foreground.
#
# Serves the API and, when frontend/dist exists, the console on the same port.
#
# Usage:
#   ./start.sh [--port 8000] [--lan] [--password SECRET] [--username NAME]
#              [--workers N] [--reload]
#
#   --lan       Bind 0.0.0.0 so other machines can reach it. Without it the
#               service binds 127.0.0.1 and is reachable only from this machine.
#   --password  Require HTTP Basic auth on every request. Set this before
#               exposing the service beyond this machine.
#   --workers   Uvicorn worker processes. Default 1.
#   --reload    Restart on source changes. Development only.
# Re-run under bash if started with sh/dash (e.g. "sh setup.sh").
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

PORT=8000
LAN=0
RELOAD=0
WORKERS=1
PASSWORD=""
USERNAME_="underwriter"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)     PORT="$2"; shift 2 ;;
        --lan)      LAN=1; shift ;;
        --reload)   RELOAD=1; shift ;;
        --workers)  WORKERS="$2"; shift 2 ;;
        --password) PASSWORD="$2"; shift 2 ;;
        --username) USERNAME_="$2"; shift 2 ;;
        -h|--help)  sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="$ROOT/backend"
VENV_PY="$BACKEND/.venv/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
    echo "Virtual environment not found. Run ./setup.sh first." >&2
    exit 1
fi

if [[ "$LAN" == 1 ]]; then
    BIND=0.0.0.0
    DISPLAY_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
    DISPLAY_HOST="${DISPLAY_HOST:-$(hostname)}"
else
    BIND=127.0.0.1
    DISPLAY_HOST=localhost
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# Same-origin when the console is served from dist, so no CORS is needed.
export ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-http://localhost:$PORT}"

# Password gate. Off unless --password is supplied.
if [[ -n "$PASSWORD" ]]; then
    export ACCESS_PASSWORD="$PASSWORD"
    export ACCESS_USERNAME="$USERNAME_"
else
    unset ACCESS_PASSWORD
fi

echo
printf '  \033[36mMoMo statement underwriting\033[0m\n'
echo
if [[ -f "$ROOT/frontend/dist/index.html" ]]; then
    echo "  Console   http://$DISPLAY_HOST:$PORT"
else
    printf '  \033[33mConsole   not built - run setup.sh without --skip-frontend\033[0m\n'
fi
echo "  API docs  http://$DISPLAY_HOST:$PORT/docs"
echo "  Health    http://$DISPLAY_HOST:$PORT/api/v1/health"
echo
if [[ -n "$PASSWORD" ]]; then
    echo "  Sign in   $USERNAME_ / (the password you passed)"
    echo "  Password protection is ON. Share credentials separately from the link."
else
    echo "  No password set. Fine on this machine; add --password before sharing."
fi
if [[ "$LAN" == 1 ]] && command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
    echo
    echo "  ufw is active. If other machines cannot connect:  sudo ufw allow $PORT/tcp"
fi
echo
echo "  Ctrl+C to stop."
echo

ARGS=(-m uvicorn app.main:app --host "$BIND" --port "$PORT")
if [[ "$RELOAD" == 1 ]]; then
    ARGS+=(--reload)
elif [[ "$WORKERS" -gt 1 ]]; then
    ARGS+=(--workers "$WORKERS")
fi

cd "$BACKEND"
exec "$VENV_PY" "${ARGS[@]}"
