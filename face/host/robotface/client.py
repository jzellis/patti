"""Serial client for the face. Runs on the Pi, talks to the Pico over USB CDC."""

import glob
import time

import serial

from . import protocol


class FaceError(RuntimeError):
    """The Pico replied with ERR, or did not reply at all."""


def find_port(explicit=None):
    if explicit:
        return explicit
    # MicroPython on an rp2 shows up as a CDC ACM device. If more than one is
    # present we cannot guess, so make the caller choose rather than picking
    # the wrong board once the motor Picos are also plugged in.
    candidates = sorted(glob.glob("/dev/ttyACM*"))
    if not candidates:
        raise FaceError("no /dev/ttyACM* found -- is the Pico plugged in?")
    if len(candidates) > 1:
        raise FaceError(
            "several serial devices present (%s); pass --port to pick one"
            % ", ".join(candidates))
    return candidates[0]


class FaceClient:
    def __init__(self, port=None, timeout=10.0, settle=0.3):
        self.port = find_port(port)
        self.ser = serial.Serial(self.port, protocol.DEFAULT_BAUD, timeout=timeout)
        # Opening the port can assert DTR and reset some boards; give it a
        # moment, then throw away anything already buffered.
        time.sleep(settle)
        self.ser.reset_input_buffer()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- plumbing ---------------------------------------------------------

    def _readline(self):
        raw = self.ser.readline()
        if not raw:
            raise FaceError("timed out waiting for a reply from %s" % self.port)
        return raw.decode("utf-8", "replace").strip()

    def command(self, line):
        """Send one command and return its reply, raising on ERR."""
        self.ser.write((line + "\n").encode())
        self.ser.flush()
        reply = self._readline()
        if reply.startswith("ERR"):
            raise FaceError(reply[4:].strip() or "unspecified error")
        if not reply.startswith("OK"):
            # Most likely the firmware is not running and we are talking to a
            # bare REPL, which echoes instead of replying.
            raise FaceError(
                "unexpected reply %r -- is main.py running on the Pico?" % reply)
        return reply[3:].strip()

    # --- commands ---------------------------------------------------------

    def hello(self):
        return self.command("HELLO")

    def ping(self):
        return self.command("PING")

    def status(self):
        out = {}
        for pair in self.command("STATUS").split():
            if "=" in pair:
                k, v = pair.split("=", 1)
                out[k] = v
        return out

    def preset(self, name):
        return self.command("PRESET " + name)

    def pico_text(self, text, scale=3):
        """Draw text using the Pico's own blocky font (no Pi rendering)."""
        return self.command("TEXT %d %s" % (scale, text.replace("\n", " ")))

    def clear(self):
        return self.command("CLEAR")

    def refresh(self):
        return self.command("REFRESH")

    def frame(self, data):
        """Send a packed frame rendered on this side."""
        if len(data) != protocol.FRAME_BYTES:
            raise ValueError(
                "frame must be %d bytes, got %d" % (protocol.FRAME_BYTES, len(data)))
        self.ser.write(
            ("FRAME %d %s\n" % (len(data), protocol.checksum(data))).encode())
        self.ser.flush()
        ack = self._readline()
        if not ack.startswith("OK ready"):
            raise FaceError("Pico refused the frame: %s" % ack)
        self.ser.write(data)
        self.ser.flush()
        reply = self._readline()
        if reply.startswith("ERR"):
            raise FaceError(reply[4:].strip())
        return reply[3:].strip()
