#!/usr/bin/env python3
"""Drive the gimbal from the keyboard.

    tools/keys.py                  # PORT=/dev/ttyACM1 tools/keys.py to override

    arrows   pan / tilt (hold to keep moving)
    c        centre
    x        release (stop pulses; the next arrow re-attaches)
    q        quit (Ctrl-C works too)

Needs pico/keyctl.py and pico/gimbal.py on the Pico (tools/deploy.sh). This
talks to the normal MicroPython REPL: it interrupts whatever is running with
Ctrl-C (never Ctrl-D, which would soft-reset and could cut a servo pulse
short), starts keyctl.run(), then forwards arrow keys as single letters.

Nothing else may have the serial port open (mpremote, another keys.py).
"""

import os
import select
import sys
import termios
import tty

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host"))
import picolink  # noqa: E402

ARROWS = {b"A": b"U", b"B": b"D", b"C": b"R", b"D": b"L"}
PLAIN = {b"c": b"c", b"x": b"x"}
QUIT = (b"q", b"Q", b"\x03")


def keys(buf):
    """Turn raw terminal bytes into Pico commands. Yields None for quit."""
    i = 0
    while i < len(buf):
        if buf[i:i + 2] in (b"\x1b[", b"\x1bO") and i + 2 < len(buf):
            cmd = ARROWS.get(buf[i + 2:i + 3])
            i += 3
            if cmd:
                yield cmd
            continue
        b = buf[i:i + 1]
        i += 1
        if b in QUIT:
            yield None
            return
        if b in PLAIN:
            yield PLAIN[b]


def main():
    ser = picolink.open_port()
    pending = picolink.start(ser)
    print("arrows: move   c: centre   x: release   q: quit")

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    status = ""
    try:
        while True:
            for line in pending.split(b"\n")[:-1]:
                line = line.strip().decode(errors="replace")
                if line.startswith("POS "):
                    p, t = line.split()[1:3]
                    status = "pan %4s deg   tilt %4s deg" % (p, t)
                elif line:
                    status = line
                sys.stdout.write("\r\x1b[K" + status)
                sys.stdout.flush()
            pending = pending.rsplit(b"\n", 1)[-1]

            r, _, _ = select.select([fd, ser.fileno()], [], [], 0.1)
            if ser.fileno() in r:
                pending += ser.read(1024)
            if fd in r:
                for cmd in keys(os.read(fd, 64)):
                    if cmd is None:
                        return
                    ser.write(cmd)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        picolink.stop(ser)
        print("\nreleased, bye")


if __name__ == "__main__":
    main()
