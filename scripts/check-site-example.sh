#!/usr/bin/env bash
set -euo pipefail

example=site/examples/quickstart.yml

if [ -z "${VENOM_BIN:-}" ]; then
  if ! command -v go >/dev/null 2>&1; then
    echo "check-site-example: go is not on PATH and VENOM_BIN is not set, cannot build venom"
    exit 2
  fi
  go build -o build/venom ./cmd/venom
  VENOM_BIN=build/venom
fi

if [ ! -f "$VENOM_BIN" ] || [ ! -x "$VENOM_BIN" ]; then
  echo "check-site-example: VENOM_BIN '$VENOM_BIN' is not an executable file"
  exit 2
fi

status=0
output=$("$VENOM_BIN" run "$example" 2>&1) || status=$?
echo "$output"

# venom colours the status word even when its output is not a terminal
esc=$(printf '\033')
plain=$(sed "s/${esc}\[[0-9;]*m//g" <<<"$output")
if [ "$status" -eq 0 ] && grep -q 'final status: PASS' <<<"$plain"; then
  echo "check-site-example: PASS"
  exit 0
fi
echo "check-site-example: FAIL (venom exit code $status)"
exit 1
