#!/usr/bin/env bash
# Copy the face firmware onto the Pico with mpremote.
#
#   tools/deploy.sh                 # deploy everything, then reset
#   tools/deploy.sh --smoke         # deploy libs only and run the smoke test
#   tools/deploy.sh --diag          # run panel diagnostics (use when it hangs)
#   tools/deploy.sh --openloop      # drive the panel with BUSY ignored
#   PORT=/dev/ttyACM1 tools/deploy.sh
#
# Layout on the device is flat: shared/ and pico/*.py land at the filesystem
# root (so "import protocol" works), and pico/lib/ lands at /lib.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-}"
MPR=(mpremote)
if [[ -n "$PORT" ]]; then MPR=(mpremote connect "$PORT"); fi

if ! command -v mpremote >/dev/null; then
  echo "mpremote not found. Install it with: pip install --user mpremote" >&2
  exit 1
fi

echo "== device =="
"${MPR[@]}" exec "import sys, os; print(sys.implementation); print(os.uname().machine)"

echo "== libraries =="
"${MPR[@]}" fs mkdir :lib || true
"${MPR[@]}" fs cp "$HERE/pico/lib/epd2in13_v4.py" :lib/
"${MPR[@]}" fs cp "$HERE/shared/protocol.py" :
"${MPR[@]}" fs cp "$HERE/shared/expressions.py" :
"${MPR[@]}" fs cp "$HERE/pico/draw.py" :
"${MPR[@]}" fs cp "$HERE/pico/face.py" :

if [[ "${1:-}" == "--openloop" ]]; then
  echo "== open-loop panel test =="
  "${MPR[@]}" run "$HERE/pico/openloop.py"
  exit 0
fi

if [[ "${1:-}" == "--diag" ]]; then
  echo "== panel diagnostics =="
  "${MPR[@]}" run "$HERE/pico/diagnose.py"
  exit 0
fi

if [[ "${1:-}" == "--smoke" ]]; then
  echo "== smoke test (this takes ~15s and refreshes the panel 4 times) =="
  "${MPR[@]}" run "$HERE/pico/smoketest.py"
  exit 0
fi

echo "== firmware =="
"${MPR[@]}" fs cp "$HERE/pico/main.py" :

echo "== reset =="
"${MPR[@]}" reset
echo
echo "Deployed. main.py runs on boot and disables Ctrl-C after a 3s"
echo "grace window -- use 'facectl' to talk to it, or send REPL to get the"
echo "prompt back."
