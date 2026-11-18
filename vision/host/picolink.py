"""Talk to pico/keyctl.py over the Pico's USB serial REPL.

Shared by tools/keys.py and host/track.py. We never send Ctrl-D: a soft reset
stops the PWM wherever it is and can cut a servo pulse short (see the README,
"Stopping the servos").
"""

import os
import sys
import time

import serial

DEFAULT_PORT = os.environ.get("PORT", "/dev/ttyACM0")


def open_port(port=DEFAULT_PORT):
    try:
        return serial.Serial(port, 115200, timeout=0)
    except serial.SerialException as e:
        sys.exit("Can't open %s: %s" % (port, e))


def start(ser, at=None, hold=False):
    """Break to the REPL and start keyctl.run(). Returns any bytes the Pico
    sent after READY. Starting puts the gimbal at `at` (pan, tilt), or
    centres it: a snap, unless that's where it already is (see Servo).
    hold: no servo pulses till a "G" is sent (the eyes work meanwhile)."""
    # Two Ctrl-Cs: break a keyctl loop left running by a previous session
    # (its finally: detaches cleanly), or anything else, back to ">>>".
    ser.write(b"\r\x03\x03")
    time.sleep(0.3)
    ser.reset_input_buffer()
    args = [b"(%.1f, %.1f)" % tuple(at) if at else b"None"] + ([b"hold=True"] if hold else [])
    ser.write(b"import keyctl; keyctl.run(%s)\r" % b", ".join(args))
    deadline = time.monotonic() + 5
    seen = b""
    while time.monotonic() < deadline:
        seen += ser.read(256)
        if b"READY" in seen:
            return seen.split(b"READY", 1)[1]
        time.sleep(0.02)
    sys.exit("Pico didn't start keyctl. It said:\n%s\n"
             "Is keyctl.py on the board? Run tools/deploy.sh first."
             % seen.decode(errors="replace"))


def look(ser, pan, tilt):
    ser.write(b"A%.1f,%.1f\n" % (pan, tilt))


class Pose:
    """Where the gimbal actually is, from keyctl's "AT <pan> <tilt>" replies.

    request() asks for one; poll() reads whatever replies have arrived and
    returns a list of (time, pan, tilt). Replies come back within a frame or
    two, and are stamped with when they were read, which is close enough.
    """

    def __init__(self, ser):
        self.ser = ser
        self.buf = b""

    def request(self):
        self.ser.write(b"P")

    def poll(self):
        out = []
        if self.ser.in_waiting:
            self.buf += self.ser.read(self.ser.in_waiting)
            now = time.monotonic()
            *lines, self.buf = self.buf.split(b"\n")
            for line in lines:
                parts = line.split()
                if len(parts) == 3 and parts[0] == b"AT":
                    try:
                        out.append((now, float(parts[1]), float(parts[2])))
                    except ValueError:
                        pass
        return out


def stop(ser):
    """Quit keyctl (it detaches the servos) and close the port."""
    ser.write(b"q")
    time.sleep(0.2)  # let it detach before we drop the port
    ser.close()
