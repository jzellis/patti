#!/usr/bin/env bash
# Copy the motor code onto the motor Pico with mpremote.
#
#   PORT=/dev/ttyACMx tools/deploy.sh           # copy motors.py, drive.py, main.py
#   PORT=/dev/ttyACMx tools/deploy.sh --smoke   # copy, then run the smoke test
#
# PORT is required: with several Picos plugged in (face, gimbal, motors) the
# wrong one is one default away. Find it with: mpremote connect list
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${PORT:-}" ]]; then
  echo "Set PORT, e.g. PORT=/dev/ttyACM1 $0   (see: mpremote connect list)" >&2
  exit 1
fi
# "resume": no soft reset. After drive.run() has armed the watchdog, a soft
# reset kills the timer feeding it and the Pico hard-reboots mid-copy.
MPR=(mpremote connect "$PORT" resume)

echo "== device =="
"${MPR[@]}" exec "import sys; print(sys.implementation._machine)"

echo "== motors =="
"${MPR[@]}" fs cp "$HERE/pico/motors.py" "$HERE/pico/drive.py" "$HERE/pico/main.py" :

if [[ "${1:-}" == "--smoke" ]]; then
  echo "== smoke test: WHEELS OFF THE GROUND; motors A-D run in turn =="
  "${MPR[@]}" run "$HERE/pico/smoketest.py"
fi
