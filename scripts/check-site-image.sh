#!/usr/bin/env bash
# Builds the site image and proves the four acceptance criteria of story #3622 on it: pages equal the site build output (twice, from scratch),
# non-root user, a read-only root file system that rejects a write, a leak check over the image. Needs Docker. Prints `check-site-image: PASS|FAIL`.
set -euo pipefail
export MSYS_NO_PATHCONV=1

cd "$(dirname "$0")/.."
source scripts/site-image-reach.sh
PYTHON="${PYTHON:-python3}"
IMAGE="${SITE_IMAGE:-venom-site:check}"
WORK="build/site-image"
PORT="${SITE_IMAGE_PORT:-18080}"
NAME="venom-site-check"

fail() { echo "check-site-image: FAIL - $*" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "check-site-image: no docker on this machine" >&2; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "check-site-image: no curl on this machine" >&2; exit 2; }

CID=""
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  [ -z "$CID" ] || docker rm -f "$CID" >/dev/null 2>&1 || true
  docker rmi "$IMAGE-again" >/dev/null 2>&1 || true
}
trap cleanup EXIT

export_image() {
  local image="$1" dest="$2"
  mkdir -p "$dest/usr/share/nginx" "$dest/etc/nginx" "$dest/licenses"
  CID="$(docker create "$image")"
  docker cp "$CID:/usr/share/nginx/html" "$dest/usr/share/nginx/" >/dev/null
  docker cp "$CID:/etc/nginx/nginx.conf" "$dest/etc/nginx/" >/dev/null
  docker cp "$CID:/licenses/LICENSE" "$dest/licenses/" >/dev/null
  docker rm "$CID" >/dev/null
  CID=""
}

rm -rf "$WORK"
mkdir -p "$WORK"

echo "== build"
docker build -q -f deploy/site/Dockerfile -t "$IMAGE" . >/dev/null || fail "docker build"
$PYTHON scripts/build_site.py --out "$WORK/host-pages" >/dev/null || fail "host site build"
export_image "$IMAGE" "$WORK/export"
docker inspect "$IMAGE" > "$WORK/inspect.json"
docker history --no-trunc "$IMAGE" > "$WORK/history.txt"

echo "== static checks over the image (pages, user, history, licence, leaks)"
$PYTHON scripts/site_image_check.py --export "$WORK/export" --inspect "$WORK/inspect.json" --history "$WORK/history.txt" \
  --compare-with "$WORK/host-pages" || fail "image check"

echo "== second build from scratch gives the same pages"
docker build -q --no-cache -f deploy/site/Dockerfile -t "$IMAGE-again" . >/dev/null || fail "second docker build"
export_image "$IMAGE-again" "$WORK/export-again"
diff -r "$WORK/export/usr/share/nginx/html" "$WORK/export-again/usr/share/nginx/html" >/dev/null || fail "two builds differ"

echo "== run read-only: a write to a path the user could write without it fails, the site answers"
docker run -d --rm --read-only --tmpfs /tmp -p "$PORT:8080" --name "$NAME" "$IMAGE" >/dev/null || fail "docker run"
reach_site "$NAME" "$PORT" >/dev/null || fail "the site did not answer 200 (see above)"
# /etc/nginx is writable by uid 101 on this base image, so only --read-only can make this write fail
err="$(docker exec "$NAME" touch /etc/nginx/probe 2>&1)" && fail "a write under /etc/nginx succeeded"
case "$err" in *"Read-only file system"*) ;; *) fail "the write failed for another reason: $err" ;; esac

echo "check-site-image: PASS"
