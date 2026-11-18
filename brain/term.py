#!/usr/bin/env python3
"""A terminal for her chest screen. The Pi does the terminal itself; the CYD
only draws the grid of coloured characters it's sent (cyd.ino's "T" runs).
Two kinds of source:

    console     the Linux console, tty1 (read from /dev/vcsa1 and vcsu1): a
                USB keyboard plugged into the Pi types into it. Needs the
                user in the "tty" group; it's resized to fit (sudo stty).
    a command   htop, her logs, anything: run in a pseudo-terminal of the
                right size and followed with pyte (pip install pyte)

    t = Term("htop")                # or "console", or "logs"
    rows, cols, cells, cursor = t.screen()
    # cells[r][c] = (character code for the CYD, colour byte fg | bg << 4)
    t.close()

and diff(old, new) gives the runs that changed, for the CYD.

    python3 term.py htop            # print what the CYD would get
"""
import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import threading

ROWS, COLS = 20, 53             # what fits the CYD in its 6x12 font
COMMANDS = {
    "htop": ["htop"],
    "logs": ["journalctl", "-f", "-n", "40", "-o", "short", "-u", "rp-brain"],
}

# Unicode line drawing and blocks -> the CYD's codes for them (0x80-0x93);
# double, heavy and rounded lines all draw as thin ones.
BOX = {}
for chars, code in (("─━═╌╍┄┅┈┉", 0x80), ("│┃║╎╏┆┇┊┋", 0x81), ("┌┏╔╓╒╭", 0x82), ("┐┓╗╖╕╮", 0x83),
                    ("└┗╚╙╘╰", 0x84), ("┘┛╝╜╛╯", 0x85), ("├┣╠╟╞", 0x86), ("┤┫╣╢╡", 0x87),
                    ("┬┳╦╥╤", 0x88), ("┴┻╩╨╧", 0x89), ("┼╋╬╫╪", 0x8A), ("█", 0x8B), ("▀", 0x8C),
                    ("▄", 0x8D), ("░", 0x8E), ("▒", 0x8F), ("▓", 0x90), ("▌", 0x91), ("▐", 0x92),
                    ("·•∙", 0x93)):
    for ch in chars:
        BOX[ch] = code
# ...and a few others to plain ASCII
PLAIN = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": ".", "«": "<", "»": ">",
         "▲": "^", "▼": "v", "△": "^", "▽": "v", "►": ">", "◄": "<", "→": ">", "←": "<", "↑": "^", "↓": "v", "°": "o"}

# pyte's colour names -> ANSI numbers
NAMES = ["black", "red", "green", "brown", "blue", "magenta", "cyan", "white"]
# the Linux console keeps VGA order (blue and red swapped, and so on) -> ANSI
VGA = [0, 4, 2, 6, 1, 5, 3, 7]


def code(ch):
    """A character as the CYD's byte."""
    if " " <= ch <= "~":
        return ord(ch)
    if ch in BOX:
        return BOX[ch]
    ch = PLAIN.get(ch, ch)
    return ord(ch) if " " <= ch <= "~" else ord("?")


def ansi(colour, default, bright=False):
    """pyte's colour (a name, or hex for 256-colour) -> 0-15."""
    if colour == "default":
        return default
    if colour.startswith("bright") and colour[6:] in NAMES:
        return NAMES.index(colour[6:]) + 8
    if colour in NAMES:
        return NAMES.index(colour) + (8 if bright else 0)
    if colour == "brightblack":
        return 8
    try:                                    # "ff8700": the nearest of the 16
        r, g, b = (int(colour[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default
    hi = max(r, g, b) > 200
    n = (r > 100) * 1 + (g > 100) * 2 + (b > 100) * 4
    return n + (8 if hi and n else 0) if n else (8 if max(r, g, b) > 60 else 0)


class Console:
    """tty1, read from the kernel's copy of it."""

    def __init__(self, rows=ROWS, cols=COLS):
        subprocess.run(["sudo", "-n", "stty", "-F", "/dev/tty1", "rows", str(rows), "cols", str(cols)],
                       capture_output=True, timeout=5)

    def screen(self):
        with open("/dev/vcsa1", "rb") as f:
            a = f.read()
        with open("/dev/vcsu1", "rb") as f:
            u = f.read()
        rows, cols, x, y = a[0], a[1], a[2], a[3]
        cells = []
        for r in range(rows):
            row = []
            for c in range(cols):
                i = r * cols + c
                at = a[4 + 2 * i + 1]
                ch = chr(struct.unpack_from("<I", u, 4 * i)[0] or 32)
                fg, bg = VGA[at & 7] + (at & 8), VGA[(at >> 4) & 7]
                row.append((code(ch), fg | bg << 4))
            cells.append(row)
        return rows, cols, cells, (y, x)

    def close(self):
        pass


class Program:
    """A command in a pseudo-terminal, followed with pyte."""

    def __init__(self, argv, rows=ROWS, cols=COLS):
        import pyte
        self.screen_ = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen_)
        self.lock = threading.Lock()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:                                       # the child
            os.environ.update(TERM="xterm", LINES=str(rows), COLUMNS=str(cols))
            os.execvp(argv[0], argv)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.kill(self.pid, signal.SIGWINCH)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                data = os.read(self.fd, 4096)
            except OSError:
                return
            if not data:
                return
            with self.lock:
                self.stream.feed(data)

    def screen(self):
        s = self.screen_
        with self.lock:
            cells = []
            for r in range(s.lines):
                line = s.buffer[r]
                row = []
                for c in range(s.columns):
                    ch = line[c]
                    fg, bg = ansi(ch.fg, 7, ch.bold), ansi(ch.bg, 0)
                    if ch.reverse:
                        fg, bg = bg, fg
                    row.append((code(ch.data[:1] or " "), fg | bg << 4))
                cells.append(row)
            cursor = None if s.cursor.hidden else (s.cursor.y, s.cursor.x)
        return s.lines, s.columns, cells, cursor

    def close(self):
        try:
            os.kill(self.pid, signal.SIGTERM)
            os.waitpid(self.pid, 0)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


def Term(what):
    """console, htop or logs."""
    return Console() if what == "console" else Program(COMMANDS[what])


def diff(old, new, gap=3):
    """The runs to send: [(row, col, colour byte, bytes)], each one colour,
    taking in unchanged cells between changed ones if fewer than gap (fewer,
    longer lines)."""
    out = []
    for r, row in enumerate(new):
        prev = old[r] if old and r < len(old) else None
        same = lambda c: prev is not None and c < len(prev) and prev[c] == row[c]
        c, n = 0, len(row)
        while c < n:
            if same(c):
                c += 1
                continue
            start, at, end = c, row[c][1], c + 1
            c += 1
            while c < n and row[c][1] == at:
                if not same(c):
                    end = c + 1
                elif c - end + 1 >= gap:
                    break
                c += 1
            out.append((r, start, at, bytes(ch for ch, _ in row[start:end])))
            c = end
    return out


if __name__ == "__main__":
    import time
    t = Term(sys.argv[1] if len(sys.argv) > 1 else "htop")
    time.sleep(2)
    rows, cols, cells, cursor = t.screen()
    print("%dx%d, cursor %s" % (rows, cols, cursor))
    for row in cells:
        print("".join(chr(ch) if ch < 128 else "#" for ch, _ in row))
    print(len(diff(None, cells)), "runs for the whole screen")
    t.close()
