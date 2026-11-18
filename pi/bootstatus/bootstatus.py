#!/usr/bin/env python3
"""Boot status on the Sense HAT's LED matrix.

Starts as soon as the matrix's framebuffer appears (about 7 s after power on;
before that the HAT shows its own rainbow) and shows, in turn:

  booting      a single pixel spiralling in from the corner, round and round
  booted       once boot finishes, the pixel runs on to the centre and bursts
  network      a wifi fan building up while it waits, then for 5 s:
               green (internet), amber (wifi but no internet) or red (no wifi)
  Tailscale    its nine dots, briefly, green (connected) or red (not)

then blanks the matrix and exits. Draws straight to the framebuffer (8x8,
RGB565), so it needs nothing but Python. Runs as root from rp-bootstatus.service.

    bootstatus.py           the real thing (waits for boot, network, Tailscale)
    bootstatus.py --demo    the whole show with made-up timings, for testing
"""
import glob
import json
import os
import socket
import subprocess
import sys
import threading
import time

ROTATE = int(os.environ.get("ROTATE", "0"))     # 0/90/180/270: how the matrix is mounted
BRIGHT = float(os.environ.get("BRIGHT", "0.6")) # the LEDs are fierce; scale everything
STEP_S = 0.03                   # spiral speed: one cell per step
NET_WAIT_S = 60                 # give up on the network this long after boot
TS_WAIT_S = 20                  # ...and on Tailscale this long after the network
SHOW_S = 5                      # how long the network result stays up
TS_SHOW_S = 2

CYAN = (0, 200, 255)
GREEN = (0, 255, 40)
AMBER = (255, 140, 0)
RED = (255, 0, 0)
WHITE = (255, 255, 255)
BLUE = (40, 90, 255)


# ---------- the matrix ----------
def find_fb():
    for f in glob.glob("/sys/class/graphics/fb*/name"):
        with open(f) as fh:
            if fh.read().strip() == "RPi-Sense FB":
                return "/dev/" + f.split("/")[-2]
    return None


class Matrix:
    def __init__(self, dev):
        self.fd = os.open(dev, os.O_WRONLY)

    def show(self, px):
        """px: dict {(row, col): (r, g, b)}; anything missing is off."""
        buf = bytearray(128)
        for (r, c), (red, green, blue) in px.items():
            for _ in range(ROTATE // 90):
                r, c = c, 7 - r
            if 0 <= r < 8 and 0 <= c < 8:
                red, green, blue = (int(v * BRIGHT) for v in (red, green, blue))
                v = (red >> 3) << 11 | (green >> 2) << 5 | blue >> 3
                i = (r * 8 + c) * 2
                buf[i], buf[i + 1] = v & 0xFF, v >> 8
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, bytes(buf))

    def clear(self):
        self.show({})


def dim(col, f):
    return tuple(int(v * f) for v in col)


# ---------- pictures ----------
def spiral():
    """The 64 cells from the top-left corner, clockwise, inward."""
    cells, top, left, bottom, right = [], 0, 0, 7, 7
    while top <= bottom and left <= right:
        cells += [(top, c) for c in range(left, right + 1)]
        cells += [(r, right) for r in range(top + 1, bottom + 1)]
        if top < bottom:
            cells += [(bottom, c) for c in range(right - 1, left - 1, -1)]
        if left < right:
            cells += [(r, left) for r in range(bottom - 1, top, -1)]
        top, left, bottom, right = top + 1, left + 1, bottom - 1, right - 1
    return cells


SPIRAL = spiral()
TRAIL = 6                       # cells of fading tail behind the pixel

WIFI_DOT = [(5, 3), (5, 4), (6, 3), (6, 4)]
WIFI_MID = [(2, 3), (2, 4), (3, 2), (3, 5)]
WIFI_OUT = [(0, 2), (0, 3), (0, 4), (0, 5), (1, 1), (1, 6), (2, 0), (2, 7)]
SLASH = [(i, i) for i in range(8)]

# Tailscale's nine dots as 2x2 blocks; its logo has the middle row and the
# bottom middle bright, the rest dim
TS_BRIGHT = {(1, 0), (1, 1), (1, 2), (2, 1)}


def tailscale_icon(col):
    px = {}
    for gr in range(3):
        for gc in range(3):
            c = col if (gr, gc) in TS_BRIGHT else dim(col, 0.25)
            for dr in range(2):
                for dc in range(2):
                    px[(gr * 3 + dr, gc * 3 + dc)] = c
    return px


def wifi_icon(col, slash=False):
    px = {p: col for p in WIFI_DOT + WIFI_MID + WIFI_OUT}
    if slash:
        px.update({p: WHITE for p in SLASH})
    return px


# ---------- what's going on ----------
class Booted:
    """Set once systemd says boot has finished (running or degraded)."""
    def __init__(self, demo):
        self.event = threading.Event()
        if demo:
            threading.Timer(4.0, self.event.set).start()
        else:
            threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self):
        subprocess.run(["systemctl", "is-system-running", "--wait"], capture_output=True)
        self.event.set()


def network_up():
    """Is any real interface connected, according to NetworkManager?"""
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "TYPE,STATE", "device"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split(":")[1:2] == ["connected"] and line.split(":")[0] in ("wifi", "ethernet")
               for line in out.splitlines())


def internet_up():
    """Can we look a name up and reach it?"""
    try:
        socket.create_connection(("github.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


def tailscale_up():
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5).stdout
        st = json.loads(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return st.get("BackendState") == "Running" and bool(st.get("Self", {}).get("Online"))


# ---------- the show ----------
def spin_until(m, booted):
    """Spiral until booted, then finish the lap to the centre."""
    i = 0
    while True:
        k = i % len(SPIRAL)
        px = {}
        for t in range(TRAIL, -1, -1):
            if i - t >= 0:
                px[SPIRAL[(i - t) % len(SPIRAL)]] = dim(CYAN, (TRAIL + 1 - t) / (TRAIL + 1)) if t else WHITE
        m.show(px)
        if k == len(SPIRAL) - 1 and booted.is_set():
            return
        i += 1
        time.sleep(STEP_S)


def starburst(m):
    """Rings racing out from the centre, then fading."""
    cols = [WHITE, (255, 230, 120), (255, 170, 40), (255, 90, 0), (120, 30, 0)]
    for frame in range(9):
        px = {}
        for r in range(8):
            for c in range(8):
                d = max(abs(r - 3.5), abs(c - 3.5)) - 0.5      # 0 at the middle four, 3 at the edge
                age = frame - d
                if 0 <= age < len(cols):
                    px[(r, c)] = cols[int(age)]
        m.show(px)
        time.sleep(0.06)
    m.clear()


def wait_network(m, demo):
    """Build the wifi fan up, over and over, until there's a network (or we give up)."""
    stages = [WIFI_DOT, WIFI_DOT + WIFI_MID, WIFI_DOT + WIFI_MID + WIFI_OUT, []]
    end = time.monotonic() + (3 if demo else NET_WAIT_S)
    result = [None]

    def check():
        if demo:
            time.sleep(3)
            result[0] = True
            return
        while time.monotonic() < end:
            if network_up():
                result[0] = True
                return
            time.sleep(1)
        result[0] = False
    threading.Thread(target=check, daemon=True).start()
    i = 0
    while result[0] is None:
        m.show({p: BLUE for p in stages[i % len(stages)]})
        i += 1
        time.sleep(0.35)
    return result[0]


def main():
    demo = "--demo" in sys.argv
    booted = Booted(demo)
    dev = None
    while not dev:                       # the driver loads a few seconds into boot
        dev = find_fb()
        if not dev:
            time.sleep(0.05)
    m = Matrix(dev)

    spin_until(m, booted.event)
    starburst(m)
    time.sleep(0.3)

    net = wait_network(m, demo)
    inet = net and (demo or internet_up())
    if not net:
        m.show(wifi_icon(RED, slash=True))
    elif not inet:
        m.show(wifi_icon(AMBER))
    else:
        m.show(wifi_icon(GREEN))
    print("network: %s, internet: %s" % (net, inet), flush=True)
    time.sleep(SHOW_S)

    ts = False
    if inet:
        end = time.monotonic() + (0 if demo else TS_WAIT_S)
        ts = demo or tailscale_up()
        while not ts and time.monotonic() < end:
            time.sleep(1)
            ts = tailscale_up()
    m.show(tailscale_icon(GREEN if ts else RED))
    print("tailscale: %s" % ts, flush=True)
    time.sleep(TS_SHOW_S)
    m.clear()


if __name__ == "__main__":
    main()
