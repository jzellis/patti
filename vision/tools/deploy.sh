#!/usr/bin/env bash
# Copy the gimbal code onto the Pico with mpremote.
#
#   tools/deploy.sh                 # copy gimbal.py + keyctl.py (+ eyepanels.py) only
#   tools/deploy.sh --smoke         # copy gimbal.py, then run the smoke test
#   tools/deploy.sh --repl          # copy gimbal.py, then open a REPL to jog by hand
#   PORT=/dev/ttyACM1 tools/deploy.sh
#
# Layout on the device is flat: pico/*.py land at the filesystem root.
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

echo "== gimbal =="
"${MPR[@]}" fs cp "$HERE/pico/gimbal.py" "$HERE/pico/keyctl.py" :
# Her eyes are on this Pico too (keyctl draws what the brain sends).
"${MPR[@]}" fs cp "$HERE/../eyes/pico/eyepanels.py" :

case "${1:-}" in
  --smoke)
    echo "== smoke test (servos WILL move; board power switch is the e-stop) =="
    "${MPR[@]}" run "$HERE/pico/smoketest.py"
    ;;
  --repl)
    echo "== REPL: from gimbal import *; g = Gimbal(); g.center() =="
    "${MPR[@]}" repl
    ;;
esac
