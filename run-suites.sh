#!/usr/bin/env bash
# Run every test suite; print a failing suite's tail and exit non-zero.
# deploy-vps.sh runs this here, ships it, and runs it again on the box.
set -euo pipefail
cd "$(dirname "$0")"
for t in test_*.py; do
  log="$(mktemp)"
  python3 "$t" -q >"$log" 2>&1 || { tail -20 "$log"; rm -f "$log"; echo "  $t FAILED" >&2; exit 1; }
  rm -f "$log"
  echo "  $t ok"
done
