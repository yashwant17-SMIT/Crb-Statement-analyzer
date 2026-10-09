#!/usr/bin/env bash
# Remove the underwriting systemd unit.
#
# Usage:
#   sudo ./uninstall-service.sh [--name momo-underwriter] [--purge]
#
#   --purge  Also delete /etc/<name>/env. The service account is left in place.
# Re-run under bash if started with sh/dash (e.g. "sh setup.sh").
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

NAME=momo-underwriter
PURGE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)  NAME="$2"; shift 2 ;;
        --purge) PURGE=1; shift ;;
        -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0" >&2; exit 1; }

UNIT="/etc/systemd/system/$NAME.service"
if [[ -f "$UNIT" ]]; then
    systemctl disable --now "$NAME" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "Service '$NAME' removed."
else
    echo "No unit at $UNIT."
fi

if [[ "$PURGE" == 1 && -d "/etc/$NAME" ]]; then
    rm -rf "/etc/$NAME"
    echo "Removed /etc/$NAME."
fi
