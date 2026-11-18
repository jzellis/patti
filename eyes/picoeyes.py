"""The eye panels on the gimbal Pico, with the same interface as gc9a01 (which
drove them from the Pi's own SPI6): eyes.Eyes(picoeyes, link) draws exactly as
before, and this sends the changed rectangles over the Pico's USB serial to
keyctl.py, which puts them on the panels (eyes/pico/eyepanels.py).

    bus, panels = picoeyes.open_eyes(link)
    panels["left"].show(img)        # 240x240 uint16 RGB565 numpy array
    picoeyes.close(bus, panels)

link is brain/server.py's Gimbal: link.send(bytes) queues bytes for the Pico
and waits till they're written (so the eyes go at the speed of the link),
False when the Pico's offline; link.gen goes up each time keyctl (re)starts,
which re-initialises and blanks the panels.
"""
import numpy as np

SIZE = 240
EYES = {"left": 0, "right": 1}      # keyctl's eye numbers
# Over USB nothing gets garbled on the way to the Pico (only maybe on its
# short leads to the panels), so a full redraw now and then can be rarer.
REFRESH_S = 10.0
RLE_MAX = 32768         # keyctl's limit (eyepanels.RLE_MAX)
# The Pico has to draw every pixel of a rectangle it's sent, and that's the
# slow part, so changed rows further apart than this go as separate
# rectangles (a blink moves a band at the top and one at the bottom).
GAP = 6


def rgb565(r, g, b):
    return (r & 0xF8) << 8 | (g & 0xFC) << 3 | b >> 3


class Bus:
    def __init__(self, link):
        self.link = link
        self.gen = None         # the keyctl run the panels were last drawn in
        self.lit = True

    def check(self, panels):
        """After keyctl restarts the panels are blank with the backlight on:
        redraw everything, and put the backlight back as it was."""
        if self.gen != self.link.gen:
            self.gen = self.link.gen
            for p in panels.values():
                p.shown = None
            self.link.send(b"B1" if self.lit else b"B0")


class Panel:
    def __init__(self, bus, eye):
        self.bus = bus
        self.eye = eye
        self.panels = None
        self.shown = None       # what the panel holds now, to send only changes

    def show(self, img):
        """Send a 240x240 uint16 RGB565 image, or just the parts of it that
        differ from what's on the panel: a rectangle round each band of
        changed rows."""
        self.bus.check(self.panels)
        if self.shown is None:
            rects = [(0, SIZE - 1, 0, SIZE - 1)]
        else:
            diff = img != self.shown
            rows = np.flatnonzero(diff.any(1))
            if not len(rows):
                return
            rects = []
            for band in np.split(rows, np.flatnonzero(np.diff(rows) > GAP) + 1):
                y0, y1 = band[0], band[-1]
                cols = np.flatnonzero(diff[y0:y1 + 1].any(0))
                rects.append((y0, y1, cols[0], cols[-1]))
        data = b"".join(_frame(bytes((self.eye, int(x0), int(y0), int(x1), int(y1))),
                               img[y0:y1 + 1, x0:x1 + 1]) for y0, y1, x0, x1 in rects)
        if self.bus.link.send(data):
            self.shown = img.copy()
        else:
            self.shown = None       # offline: everything again when it's back


def _frame(rect, sub):
    """keyctl's E (raw) or Z (run-length) command for a rectangle of
    pixels, whichever is shorter."""
    flat = sub.ravel()
    starts = np.flatnonzero(np.r_[True, flat[1:] != flat[:-1]])
    if len(starts) * 4 < min(flat.size * 2, RLE_MAX + 1):
        runs = np.empty(2 * len(starts), ">u2")
        runs[0::2] = np.diff(np.r_[starts, flat.size])
        runs[1::2] = flat[starts]
        return b"Z" + rect + (runs.nbytes).to_bytes(2, "big") + runs.tobytes()
    return b"E" + rect + flat.astype(">u2").tobytes()


def open_eyes(link):
    bus = Bus(link)
    panels = {name: Panel(bus, n) for name, n in EYES.items()}
    for p in panels.values():
        p.panels = panels
    return bus, panels


def reset(bus, panels):
    """Re-initialise both panels (blank, backlight on)."""
    bus.link.send(b"I")
    bus.lit = True
    for p in panels.values():
        p.shown = None


def backlight(bus, on):
    bus.lit = bool(on)
    bus.link.send(b"B1" if on else b"B0")


def close(bus, panels):
    for p in panels.values():
        p.show(np.zeros((SIZE, SIZE), np.uint16))
    backlight(bus, False)
