"""The two eye panels on the gimbal Pico (Kitronik board), for keyctl.py: the
Pi draws the eyes and sends the changed rectangles over USB; this puts them
on the panels.

    p = eyepanels.Panels()     # claims the pins and the frame buffer
    p.init()                   # reset, initialise, blank, backlight on
    n = p.size(x0, y0, x1, y1) # bytes of RGB565 for that rectangle
    ... read n bytes into p.mv ...
    p.draw(eye, x0, y0, x1, y1, n)

or, run-length coded (much less to send: her eyes are mostly flat colour),
    ... read m bytes into p.rle ...
    unrle(p.rle, m, p.buf, n) == n, then p.draw(...)

Eye 0 is her left, 1 her right. Wiring: the "Kitronik Eye Wiring" chart;
the servo headers follow the datasheet (header n is GP(n+1)), checked with
eyetest.py on 2026-10-31.
"""
import time

import micropython
from machine import Pin, SPI

SCK, MOSI, DC, RST, BL = 6, 7, 9, 8, 27    # headers 5, 6, 8, 7; breakout GP27
CS = (4, 5)                                 # headers 3, 4: her left, her right
SIZE = 240
HZ = 50_000_000     # (the RP2350 gives 50 for this; 30 was gentle for a first try)
RLE_MAX = 32768     # the most run-length data one rectangle can come as
DRAW_CHUNK = 8192   # bytes of pixels per SPI write when drawing in pieces (~1.3 ms at HZ)

# The panel maker's init sequence, as in eyes/gc9a01.py: (command, data, delay ms)
INIT = [
    (0xEF, b"", 0), (0xEB, b"\x14", 0), (0xFE, b"", 0), (0xEF, b"", 0),
    (0xEB, b"\x14", 0), (0x84, b"\x40", 0), (0x85, b"\xFF", 0), (0x86, b"\xFF", 0),
    (0x87, b"\xFF", 0), (0x88, b"\x0A", 0), (0x89, b"\x21", 0), (0x8A, b"\x00", 0),
    (0x8B, b"\x80", 0), (0x8C, b"\x01", 0), (0x8D, b"\x01", 0), (0x8E, b"\xFF", 0),
    (0x8F, b"\xFF", 0), (0xB6, b"\x00\x20", 0),
    (0x36, b"\x08", 0), (0x3A, b"\x05", 0),
    (0x90, b"\x08\x08\x08\x08", 0), (0xBD, b"\x06", 0), (0xBC, b"\x00", 0),
    (0xFF, b"\x60\x01\x04", 0), (0xC3, b"\x13", 0), (0xC4, b"\x13", 0),
    (0xC9, b"\x22", 0), (0xBE, b"\x11", 0), (0xE1, b"\x10\x0E", 0),
    (0xDF, b"\x21\x0C\x02", 0),
    (0xF0, b"\x45\x09\x08\x08\x26\x2A", 0), (0xF1, b"\x43\x70\x72\x36\x37\x6F", 0),
    (0xF2, b"\x45\x09\x08\x08\x26\x2A", 0), (0xF3, b"\x43\x70\x72\x36\x37\x6F", 0),
    (0xED, b"\x1B\x0B", 0), (0xAE, b"\x77", 0), (0xCD, b"\x63", 0),
    (0x70, b"\x07\x07\x04\x0E\x0F\x09\x07\x08\x03", 0), (0xE8, b"\x34", 0),
    (0x62, b"\x18\x0D\x71\xED\x70\x70\x18\x0F\x71\xEF\x70\x70", 0),
    (0x63, b"\x18\x11\x71\xF1\x70\x70\x18\x13\x71\xF3\x70\x70", 0),
    (0x64, b"\x28\x29\xF1\x01\xF1\x00\x07", 0),
    (0x66, b"\x3C\x00\xCD\x67\x45\x45\x10\x00\x00\x00", 0),
    (0x67, b"\x00\x3C\x00\x00\x00\x01\x54\x10\x32\x98", 0),
    (0x74, b"\x10\x85\x80\x00\x00\x4E\x00", 0), (0x98, b"\x3E\x07", 0),
    (0x35, b"", 0),                     # tearing effect line on
    (0x21, b"", 0),                     # colour inversion on (this panel needs it)
    (0x11, b"", 120),                   # sleep out
    (0x29, b"", 20),                    # display on
]


class Panels:
    def __init__(self):
        self.spi = SPI(0, baudrate=HZ, polarity=0, phase=0, sck=Pin(SCK), mosi=Pin(MOSI))
        self.dc = Pin(DC, Pin.OUT, value=0)
        self.rst = Pin(RST, Pin.OUT, value=1)
        self.bl = Pin(BL, Pin.OUT, value=0)
        self.cs = [Pin(p, Pin.OUT, value=1) for p in CS]
        # One whole panel's worth, made once (a frame read straight into it).
        self.buf = bytearray(SIZE * SIZE * 2)
        self.mv = memoryview(self.buf)
        self.rle = bytearray(RLE_MAX)
        self.rmv = memoryview(self.rle)

    def _cmd(self, eye, c, data=b""):
        cs = self.cs[eye]
        cs(0)
        self.dc(0)
        self.spi.write(bytes((c,)))
        if data:
            self.dc(1)
            self.spi.write(data)
        cs(1)

    @staticmethod
    def size(x0, y0, x1, y1):
        return (x1 - x0 + 1) * (y1 - y0 + 1) * 2

    def draw(self, eye, x0, y0, x1, y1, n, between=None):
        """Send the first n bytes of the buffer (RGB565, big-endian) to the
        rectangle x0..x1, y0..y1 (inclusive) of one eye. between() is called
        every DRAW_CHUNK bytes: a whole panel takes ~18 ms, longer than a
        servo frame, so keyctl ticks the servos there (or they judder)."""
        self._cmd(eye, 0x2A, bytes((0, x0, 0, x1)))
        self._cmd(eye, 0x2B, bytes((0, y0, 0, y1)))
        cs = self.cs[eye]
        cs(0)
        self.dc(0)
        self.spi.write(b"\x2C")
        self.dc(1)
        if between is None:
            self.spi.write(self.mv[:n])
        else:
            for i in range(0, n, DRAW_CHUNK):
                self.spi.write(self.mv[i:min(n, i + DRAW_CHUNK)])
                between()
        cs(1)

    def init(self):
        """Reset (the line is shared) and initialise both panels, blank them,
        backlight on. Also brings back a panel that lost its settings."""
        self.rst(0)
        time.sleep_ms(20)
        self.rst(1)
        time.sleep_ms(120)
        n = self.size(0, 0, SIZE - 1, SIZE - 1)
        row = bytes(SIZE * 2)
        for y in range(SIZE):
            self.mv[y * SIZE * 2:(y + 1) * SIZE * 2] = row
        for eye in range(len(CS)):
            for c, data, ms in INIT:
                self._cmd(eye, c, data)
                if ms:
                    time.sleep_ms(ms)
            self.draw(eye, 0, 0, SIZE - 1, SIZE - 1, n)
        self.bl(1)

    def backlight(self, on):
        self.bl(1 if on else 0)


@micropython.viper
def unrle(src, m: int, dst, limit: int) -> int:
    """Expand m bytes of runs (each: count, then colour; big-endian 16-bit
    both) into dst. Returns the bytes written, or -1 if they'd pass limit."""
    s = ptr8(src)
    d = ptr8(dst)
    i = 0
    o = 0
    while i < m:
        end = o + ((s[i] << 8) | s[i + 1]) * 2
        if end > limit:
            return -1
        hi = s[i + 2]
        lo = s[i + 3]
        while o < end:
            d[o] = hi
            d[o + 1] = lo
            o += 2
        i += 4
    return o
