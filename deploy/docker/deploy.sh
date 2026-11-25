#!/usr/bin/env bash
# Pull an image and (re)start the service. Runs on the prod server.
#
# Usage:
#   ./deploy.sh                  # IMAGE_TAG from .env, else "latest"
#   ./deploy.sh 3f9c2ab          # a specific commit tag (also how to roll back)
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

[ -f .env ] || { echo "Missing .env; copy env.example and fill it in." >&2; exit 1; }
[ $# -ge 1 ] && export IMAGE_TAG="$1"

docker compose pull
docker compose up -d

echo "Waiting for the health check..."
for _ in $(seq 1 30); do
    status="$(docker inspect -f '{{.State.Health.Status}}' momo-underwriter 2>/dev/null || true)"
    if [ "$status" = "healthy" ]; then
        echo "Healthy: $(docker inspect -f '{{.Config.Image}}' momo-underwriter)"
        exit 0
    fi
    sleep 2
done
echo "Not healthy after 60s. Logs:" >&2
docker compose logs --tail 50 >&2
exit 1
