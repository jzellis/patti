"""Bring-up test for the motor board. WHEELS OFF THE GROUND before running.

    tools/deploy.sh --smoke

Finds the PCA9685 on I2C, then runs each motor A, B, C, D in turn: forward
for SPIN_S at SPEED, stop, reverse, stop. Every motor is stopped at the end,
and also if anything goes wrong (or you Ctrl-C) part-way through.

No encoders yet: leave the encoder wires disconnected from the Pico until
the 5V->3.3V level shifters are in.

Watch for: each motor turning both ways, in the order A, B, C, D. Note
which physical wheel each letter is, and whether "forward" is actually
forward for it -- that becomes the invert list for Motors().
"""

import time

import motors

SPEED = 0.4
SPIN_S = 1.5
PAUSE_S = 0.7


def main():
    m = motors.Motors()
    print("PCA9685 found at 0x%02X, all motors stopped" % motors.ADDR)
    try:
        for name in "ABCD":
            for label, s in (("forward", SPEED), ("reverse", -SPEED)):
                print("motor %s %s" % (name, label))
                m[name].run(s)
                time.sleep(SPIN_S)
                m[name].coast()
                time.sleep(PAUSE_S)
    finally:
        m.stop_all()
        print("all stopped")


main()
