#!/usr/bin/env python3
"""Her chest screen: the CYD (cyd/), over USB serial.

Cyd() opens it and keeps it open: a thread reads what it says, and if it's
unplugged (or not there at boot) it keeps trying, so it comes back by
itself. Rows sent with row() are remembered, and only changed ones go out;
they're all sent again whenever the CYD (re)boots. So are the lines set with
keep() (the clock, the weather).

Touches go to on_touch(kind, x, y), kind "touch", "move" or "release", and
the CYD's own reports (it switched screen from its icon bar, a list line was
tapped) to on_event(words), both from the reader thread. The list screens'
lines (line()) are cached and resent like the rows.

    python3 cyd.py "Hello there"     # show some words
    python3 cyd.py --touches         # print touches until Ctrl-C
    python3 cyd.py --calibrate       # touch the corners as they come up
"""
import argparse
import sys
import threading
import time

import serial

PORT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"   # its CH340
RETRY_S = 2.0
ROWS = 8
COLOURS = ("white", "grey", "green", "amber", "red", "blue")


class Cyd:
    def __init__(self, port=PORT, on_touch=None, on_event=None):
        self.port = port
        self.on_touch = on_touch
        self.on_event = on_event
        self.lines = {}                 # (screen, n) -> (colour, text) the list screens should show
        self.lines_sent = {}
        self.ser = None
        self.lock = threading.Lock()
        self.rows = [None] * ROWS       # (colour, text) the CYD should be showing
        self.sent = [None] * ROWS       # what it has been sent since it booted
        self.kept = {}                  # name -> line, or a function making it, for keep()
        self.kept_sent = {}             # name -> line, as last sent
        self.said = ""                  # its last reply, for --calibrate
        self.light = None               # its light sensor, raw (every 5 s)
        self.stopping = False
        self.thread = threading.Thread(target=self._run, name="cyd", daemon=True)
        self.thread.start()

    @property
    def ok(self):
        return self.ser is not None

    def _open(self):
        # DTR and RTS are the ESP32's reset and boot lines (its auto-program
        # circuit). Opening the port on Linux pulses them and reboots it
        # anyway (harmless: the rows go again on its "hello"); clearing them
        # first makes sure they're left released, not holding it in reset.
        ser = serial.Serial()
        ser.port, ser.baudrate, ser.timeout = self.port, 115200, 0.5
        ser.dtr = ser.rts = False
        ser.open()
        return ser

    def _run(self):
        while not self.stopping:
            try:
                ser = self._open()
            except (serial.SerialException, OSError):
                time.sleep(RETRY_S)
                continue
            print("cyd: online", file=sys.stderr)
            with self.lock:
                self.ser = ser
                self.sent = [None] * ROWS
                self.kept_sent = {}
                self.lines_sent = {}
            self.flush()
            try:
                self._read(ser)
            except (serial.SerialException, OSError) as e:
                if not self.stopping:
                    print("cyd: offline (%s)" % e, file=sys.stderr)
            with self.lock:
                self.ser = None
            ser.close()

    def _read(self, ser):
        buf = b""
        while not self.stopping:
            buf += ser.read(256)
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                line = raw.strip().decode(errors="replace")
                if not line.startswith("cyd: "):
                    continue            # its boot chatter
                words = line[5:].split()
                if words and words[0] in ("touch", "move", "release") and len(words) == 3:
                    if self.on_touch:
                        self.on_touch(words[0], int(words[1]), int(words[2]))
                elif words and words[0] == "light" and len(words) == 2:
                    self.light = int(words[1])
                elif words and words[0] in ("screen", "tap"):
                    if self.on_event:
                        self.on_event(words)
                elif words == ["hello"]:
                    # Just (re)booted: it's showing "Hello, world" again.
                    with self.lock:
                        self.sent = [None] * ROWS
                        self.kept_sent = {}
                        self.lines_sent = {}
                    if any(self.rows):
                        self.send("clear")
                    self.flush()
                    if self.on_event:
                        self.on_event(["hello"])
                else:
                    self.said = line[5:]

    def send(self, line):
        with self.lock:
            if not self.ser:
                return False
            try:
                self.ser.write(line.encode("ascii", "replace") + b"\n")
                return True
            except (serial.SerialException, OSError) as e:
                print("cyd: write failed (%s)" % e, file=sys.stderr)
                return False

    def send_raw(self, data):
        """A line as bytes (the terminal's runs use codes above 127)."""
        with self.lock:
            if not self.ser:
                return False
            try:
                self.ser.write(data + b"\n")
                return True
            except (serial.SerialException, OSError) as e:
                print("cyd: write failed (%s)" % e, file=sys.stderr)
                return False

    def text(self, words):
        """Big words in the middle (and forget the rows)."""
        self.rows = [None] * ROWS
        with self.lock:
            self.sent = [None] * ROWS
        self.send("text " + words.replace("\n", " "))

    def row(self, n, colour, words):
        """Set status row n (0 at the top); sent by flush()."""
        self.rows[n] = (colour if colour in COLOURS else "white", words.replace("\n", " "))

    def keep(self, name, line):
        """A command the CYD should always have had (e.g. "weather ..."): sent
        now if it's changed, and again whenever the CYD reboots. line can be
        a function, called each time it's sent (for the clock)."""
        self.kept[name] = line
        if callable(line):
            self.kept_sent.pop(name, None)      # always send it again now
        self.flush()

    def line(self, screen, n, colour, words):
        """Set line n of a list screen; sent by flush()."""
        self.lines[(screen, n)] = (colour if colour in COLOURS else "white", words.replace("\n", " "))

    def flush(self):
        """Send the kept lines, list lines and rows that have changed."""
        for key, r in list(self.lines.items()):
            if self.lines_sent.get(key) != r and self.send("line %s %d %s %s" % (key[0], key[1], r[0], r[1])):
                self.lines_sent[key] = r
        for name, line in list(self.kept.items()):
            if name in self.kept_sent and (callable(line) or self.kept_sent[name] == line):
                continue
            if self.send(line() if callable(line) else line):
                self.kept_sent[name] = line
        for n, r in enumerate(self.rows):
            if r is not None and r != self.sent[n] and self.send("row %d %s %s" % (n, r[0], r[1])):
                self.sent[n] = r

    def close(self):
        self.stopping = True
        self.thread.join(timeout=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("words", nargs="?", help="show these, big and centred")
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--touches", action="store_true", help="print touches until Ctrl-C")
    ap.add_argument("--calibrate", action="store_true", help="calibrate the touchscreen")
    ap.add_argument("--send", metavar="LINE", help="send one command line (e.g. fontdemo)")
    args = ap.parse_args()

    cyd = Cyd(args.port, on_touch=lambda kind, x, y: print(kind, x, y, flush=True))
    for _ in range(20):
        if cyd.ok:
            break
        time.sleep(0.1)
    if not cyd.ok:
        sys.exit("No CYD on %s" % args.port)
    try:
        if args.words:
            cyd.text(args.words)
        if args.send:
            time.sleep(2.5)         # opening the port rebooted it
            cyd.send(args.send)
        if args.calibrate:
            cyd.said = ""
            cyd.send("calibrate")
            print("Touch each corner of the screen as it lights up.")
            while not cyd.said.startswith("calibrated"):
                time.sleep(0.2)
            print(cyd.said)
        if args.touches:
            print("Touch the screen (Ctrl-C to stop).")
            while True:
                time.sleep(1)
        time.sleep(0.3)         # let the last write go out
    except KeyboardInterrupt:
        pass
    finally:
        cyd.close()


if __name__ == "__main__":
    main()
