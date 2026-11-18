"""Bring-up test for the pan/tilt gimbal. Run this before anything else.

    tools/deploy.sh --smoke

What it does, in order:
  1. Snaps both servos to centre (1500us). This first move is at full speed,
     because a hobby servo has no position feedback: there is no way to ease
     into a position we don't know we're starting from.
  2. Pans left/right by SWING_US either side of centre, slowly, then back.
  3. Tilts down/up the same way, then back.
  4. Detaches (stops pulses, cleanly -- see Servo.detach). Neither servo
     should move at this point.

SWING_US = 200 is roughly +/-18 deg on a typical MG996R: small enough not to
hit the gimbal's own end stops, big enough to see. Before running, have the
gimbal sitting roughly centred and nothing in its way. The board's power
switch is your e-stop.
"""

import sys
import time

import gimbal

SWING_US = 200
SPEED_US_S = 500
HOLD_MS = 800


def step(msg):
    print(msg)


def main():
    step("platform: %s" % (sys.implementation._machine,))
    g = gimbal.Gimbal()
    step("pan  = GP%d" % g.pan.gpio)
    step("tilt = GP%d" % g.tilt.gpio)

    try:
        step("1/4 centre (one fast move)")
        g.center()
        time.sleep_ms(2000)

        c = gimbal.CENTER_US
        step("2/4 pan: %d -> %d -> %d" % (c - SWING_US, c + SWING_US, c))
        for us in (c - SWING_US, c + SWING_US, c):
            g.glide_us(us, c, SPEED_US_S)
            time.sleep_ms(HOLD_MS)

        step("3/4 tilt: %d -> %d -> %d" % (c - SWING_US, c + SWING_US, c))
        for us in (c - SWING_US, c + SWING_US, c):
            g.glide_us(c, us, SPEED_US_S)
            time.sleep_ms(HOLD_MS)
    finally:
        step("4/4 detach")
        g.detach()

    step("done: %s" % (g.status(),))


main()
