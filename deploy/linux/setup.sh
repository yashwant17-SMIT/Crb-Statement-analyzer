#!/usr/bin/env bash
# One-time setup for the MoMo underwriting service on Linux.
#
# Creates a Python virtual environment, installs dependencies, and builds the
# React console into frontend/dist so the API can serve it on a single port.
#
# Usage:
#   ./setup.sh                  # API + console
#   ./setup.sh --skip-frontend  # API only
#   PYTHON=python3.12 ./setup.sh
# Re-run under bash if started with sh/dash (e.g. "sh setup.sh").
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

SKIP_FRONTEND=0
for arg in "$@"; do
    case "$arg" in
        --skip-frontend) SKIP_FRONTEND=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

# Repo root is two levels above deploy/linux
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
VENV="$BACKEND/.venv"
VENV_PY="$VENV/bin/python"

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$1"; }
fail() { printf '\n\033[31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

step 'Checking Python'

# Honour $PYTHON; otherwise take the first supported interpreter on PATH.
py_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
    for cand in python3.13 python3.12 python3.11 python3.14 python3; do
        if command -v "$cand" >/dev/null && py_ok "$cand"; then PY="$cand"; break; fi
    done
fi
[[ -n "$PY" ]] || fail "Python 3.11 or newer not found. Install it, e.g. on Debian/Ubuntu: sudo apt install python3 python3-venv"
py_ok "$PY" || fail "$PY is older than 3.11 (pandas and numpy wheels need 3.11+)."
PY_VER="$("$PY" -c 'import sys;print("%d.%d" % sys.version_info[:2])')"
"$PY" -c 'import venv, ensurepip' 2>/dev/null \
    || fail "The venv module is missing. On Debian/Ubuntu: sudo apt install python${PY_VER}-venv"
ok "Python $PY_VER ($(command -v "$PY"))"

step 'Creating virtual environment'
# A venv copied over from Windows has Scripts\python.exe, not bin/python, and
# cannot be reused. It is a build artefact, so replace it.
if [[ -d "$VENV" && ! -x "$VENV_PY" ]]; then
    echo "    Found a non-Linux virtual environment at $VENV, recreating it"
    rm -rf "$VENV"
fi
if [[ -x "$VENV_PY" ]]; then
    ok 'Already exists, reusing'
else
    "$PY" -m venv "$VENV"
    [[ -x "$VENV_PY" ]] || fail "Virtual environment was not created at $VENV"
    ok "$VENV"
fi

step 'Installing Python dependencies'
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install -r "$BACKEND/requirements.txt" || fail 'pip install failed. See the output above.'
ok 'Dependencies installed'

step 'Verifying the engine imports'
(cd "$BACKEND" && "$VENV_PY" -c 'import app.main; print("    ok")') || fail 'The application failed to import.'
ok 'Engine imports cleanly'

if [[ "$SKIP_FRONTEND" == 1 ]]; then
    step 'Skipping the console build (--skip-frontend)'
else
    step 'Building the console'
    command -v npm >/dev/null || fail "Node.js not found. Install Node 18+ (https://nodejs.org or your distro's nodejs package), or re-run with --skip-frontend."
    NODE_VER="$(node --version | sed 's/^v//')"
    [[ "${NODE_VER%%.*}" -ge 18 ]] || fail "Node $NODE_VER found, but 18 or newer is required by Vite 6."
    ok "Node $NODE_VER"

    # node_modules installed on Windows carries win32 esbuild/rollup binaries
    # and npm will not swap them for the Linux ones. Start clean.
    if compgen -G "$FRONTEND/node_modules/@esbuild/win32-*" >/dev/null \
       || compgen -G "$FRONTEND/node_modules/@rollup/rollup-win32-*" >/dev/null; then
        echo "    Found Windows node_modules, reinstalling for Linux"
        rm -rf "$FRONTEND/node_modules"
    fi

    (
        cd "$FRONTEND"
        if [[ -f package-lock.json ]]; then
            npm ci --no-audit --no-fund
        else
            npm install --no-audit --no-fund
        fi
        npm run build
    ) || fail 'The console build failed.'

    [[ -f "$FRONTEND/dist/index.html" ]] || fail 'The build finished but frontend/dist/index.html is missing.'
    ok 'Console built to frontend/dist'
fi

printf '\n\033[32mSetup complete.\033[0m\n'
echo "  Start it now:        ./start.sh"
echo "  Install as service:  sudo ./install-service.sh"
