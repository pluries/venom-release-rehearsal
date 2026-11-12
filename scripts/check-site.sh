#!/usr/bin/env bash
# Checks the deployed site from outside (story #3623): home page and marker, certificate lifetime, http to https redirect, no cookie,
# Content-Security-Policy, the search and llms files, and three paths the server must refuse (POST /, /.git/config, /fonts/).
# Usage: check-site.sh [host]. The default host is the public site name. Prints `check-site: PASS|FAIL`.
set -euo pipefail

HOST="${1:-venom.pluries.dev}"
HTTPS_URL="${CHECK_SITE_HTTPS_URL:-https://$HOST}"
HTTP_URL="${CHECK_SITE_HTTP_URL:-http://$HOST}"
CERT_ENDPOINT="${CHECK_SITE_CERT_ENDPOINT:-$HOST:443}"
MARKER="${CHECK_SITE_MARKER:-Pluries Venom}"
MIN_DAYS="${CHECK_SITE_MIN_DAYS:-30}"
PATH_PROBE="${CHECK_SITE_PATH:-/quickstart/}"

for tool in curl openssl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "check-site: no $tool on this machine" >&2; exit 2; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FAILURES=0
ok() { echo "ok - $*"; }
bad() { echo "FAIL - $*"; FAILURES=$((FAILURES + 1)); }

fetch() {
  local label="$1" url="$2" rc=0
  shift 2
  STATUS="$(curl -sS -D "$TMP/$label.h" -o "$TMP/$label.b" -w '%{http_code}' "$@" "$url" 2>"$TMP/$label.e")" || rc=$?
  : >>"$TMP/$label.h"
  return "$rc"
}

probe_refused() {
  local label="$1" url="$2" rc=0
  shift 2
  fetch "$label" "$url" "$@" || rc=$?
  case "$rc:$STATUS" in
    0:2??) bad "$label: $url answered $STATUS, expected it refused" ;;
    0:*) ok "$label: $url refused with $STATUS" ;;
    *) bad "$label: request to $url failed (curl exit $rc)" ;;
  esac
}

header() {
  tr -d '\r' <"$TMP/$1.h" | grep -i "^$2:" | tail -n 1 | sed 's/^[^:]*:[[:space:]]*//; s/[[:space:]]*$//' || true
}

rc=0
fetch https "$HTTPS_URL/" || rc=$?
HOME_STATUS="$STATUS"
if [ "$rc" -eq 6 ]; then
  echo "check-site: cannot resolve $HOST" >&2
  exit 2
fi
if [ "$rc" -ne 0 ]; then
  bad "home page: request to $HTTPS_URL/ failed (curl exit $rc)"
elif [ "$HOME_STATUS" != "200" ]; then
  bad "home page: $HTTPS_URL/ answered $HOME_STATUS, expected 200"
elif ! grep -qF -- "$MARKER" "$TMP/https.b"; then
  bad "home page: marker '$MARKER' not found in the body"
else
  ok "home page answers 200 and carries the marker '$MARKER'"
fi

probe_refused probe-post "$HTTPS_URL/" --data ''
probe_refused probe-git "$HTTPS_URL/.git/config"
probe_refused probe-dir "$HTTPS_URL/fonts/"

not_after="$(openssl s_client -connect "$CERT_ENDPOINT" -servername "$HOST" </dev/null 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null || true)"
if [ "${not_after#notAfter=}" = "$not_after" ]; then
  bad "certificate: no expiry date read from $CERT_ENDPOINT"
elif ! end_epoch="$(date -d "${not_after#notAfter=}" +%s 2>/dev/null)"; then
  bad "certificate: expiry date '${not_after#notAfter=}' not understood"
else
  days=$(((end_epoch - $(date +%s)) / 86400))
  if [ "$days" -gt "$MIN_DAYS" ]; then
    ok "certificate expires in $days days (more than $MIN_DAYS)"
  else
    bad "certificate expires in $days days, expected more than $MIN_DAYS"
  fi
fi

rc=0
fetch http "$HTTP_URL$PATH_PROBE" || rc=$?
REDIRECT_STATUS="$STATUS"
if [ "$rc" -ne 0 ]; then
  bad "redirect: request to $HTTP_URL$PATH_PROBE failed (curl exit $rc)"
elif [ "$REDIRECT_STATUS" != "301" ]; then
  bad "redirect: $HTTP_URL$PATH_PROBE answered $REDIRECT_STATUS, expected 301"
else
  location="$(header http location)"
  if [ "$location" = "https://$HOST$PATH_PROBE" ]; then
    ok "http redirects with 301 to https://$HOST$PATH_PROBE"
  else
    bad "redirect: Location is '$location', expected https://$HOST$PATH_PROBE"
  fi
fi

if grep -qi '^set-cookie:' "$TMP/https.h" "$TMP/http.h"; then
  bad "cookie: a Set-Cookie header was sent"
else
  ok "no Set-Cookie header on any response"
fi

if [ "$HOME_STATUS" != "200" ]; then
  bad "Content-Security-Policy: no successful https response to read it from"
elif header https content-security-policy | grep -qF "default-src 'none'"; then
  ok "Content-Security-Policy carries default-src 'none'"
else
  bad "Content-Security-Policy header missing or without default-src 'none'"
fi

for file in search-index.json llms.txt llms-full.txt; do
  rc=0
  fetch "file-$file" "$HTTPS_URL/$file" || rc=$?
  if [ "$rc" -ne 0 ]; then
    bad "$file: request to $HTTPS_URL/$file failed (curl exit $rc)"
  elif [ "$STATUS" != "200" ]; then
    bad "$file: $HTTPS_URL/$file answered $STATUS, expected 200"
  elif [ "$file" != "search-index.json" ] && ! header "file-$file" content-type | grep -qi 'charset=utf-8'; then
    bad "$file: Content-Type without charset=utf-8"
  else
    ok "$file answers 200"
  fi
done

if [ "$FAILURES" -eq 0 ]; then
  echo "check-site: PASS"
else
  echo "check-site: FAIL"
  exit 1
fi
