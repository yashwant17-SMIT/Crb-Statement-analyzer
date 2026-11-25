#!/usr/bin/env bash
# Build the full-stack image on this machine and push it to the registry.
# The prod server then only pulls it; it never needs the source or Node.
#
# Usage:
#   ./release.sh                 # tag with the current commit and "latest"
#   ./release.sh v1.2.0          # also tag v1.2.0
#   IMAGE=docker.io/me/momo-underwriter ./release.sh   # another registry
#   PUSH=0 ./release.sh          # build only, no push
#
# One-time: docker login ghcr.io -u <github-user>   (token with write:packages)
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/yashwant17-smit/momo-underwriter}"
PLATFORM="${PLATFORM:-linux/amd64}"
PUSH="${PUSH:-1}"

# Repo root is two levels above deploy/docker
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [ -n "$(git status --porcelain)" ]; then
    echo "Working tree has uncommitted changes; the image would not match any commit." >&2
    echo "Commit or stash them first." >&2
    exit 1
fi

SHA="$(git rev-parse --short HEAD)"
TAGS=(-t "$IMAGE:$SHA" -t "$IMAGE:latest")
[ $# -ge 1 ] && TAGS+=(-t "$IMAGE:$1")

OUTPUT=--load
[ "$PUSH" = "1" ] && OUTPUT=--push

docker buildx build --platform "$PLATFORM" "${TAGS[@]}" \
    --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
    "$OUTPUT" .

echo
echo "Built $IMAGE:$SHA${1:+ and :$1} (also :latest)"
[ "$PUSH" = "1" ] && echo "On the server:  IMAGE_TAG=$SHA ./deploy.sh"
