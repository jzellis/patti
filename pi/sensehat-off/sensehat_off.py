#!/usr/bin/env python3
"""Blank the Sense HAT LED matrix (the firmware leaves a rainbow lit at boot)."""
import sys
import time

from sense_hat import SenseHat

# The rpisense_fb framebuffer may not exist yet this early in boot; retry briefly.
for attempt in range(30):
    try:
        SenseHat().clear()
        print("Sense HAT LEDs cleared")
        sys.exit(0)
    except Exception as e:
        err = e
        time.sleep(1)
print(f"failed to clear Sense HAT: {err}", file=sys.stderr)
sys.exit(1)
