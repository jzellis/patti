"""Driver for the two GC9A01 eye panels on SPI6 (see README.md for wiring).

    gpio, eyes = gc9a01.open_eyes()      # resets and initialises both
    eyes["left"].show(img)               # 240x240 uint16 RGB565 numpy array
    gc9a01.close(gpio, eyes)             # blank, backlight off

Left and right are hers, as seen from behind her.
"""
import os
import time

# lgpio makes little notification pipes in its working directory, which for a
# systemd service is / (not writable). Point it somewhere that is.
os.environ.setdefault("LG_WD", "/tmp")
import lgpio  # noqa: E402
import numpy as np  # noqa: E402
import spidev  # noqa: E402

DC, RST, BL = 22, 26, 13
# spidev6.<n>. The CS wires went on crossed: GPIO18 (CE0) is her right eye.
EYES = {"left": 1, "right": 0}
SIZE = 240
HZ = 30_000_000     # the long spliced Dupont leads garbled some writes at 40 MHz; try higher once they're tidied

# The panel maker's init sequence (as in Waveshare's and Adafruit's drivers):
# (command, data bytes, delay ms after).
INIT = [
    (0xEF, b"", 0), (0xEB, b"\x14", 0), (0xFE, b"", 0), (0xEF, b"", 0),
    (0xEB, b"\x14", 0), (0x84, b"\x40", 0), (0x85, b"\xFF", 0), (0x86, b"\xFF", 0),
    (0x87, b"\xFF", 0), (0x88, b"\x0A", 0), (0x89, b"\x21", 0), (0x8A, b"\x00", 0),
    (0x8B, b"\x80", 0), (0x8C, b"\x01", 0), (0x8D, b"\x01", 0), (0x8E, b"\xFF", 0),
    (0x8F, b"\xFF", 0), (0xB6, b"\x00\x20", 0),
    (0x36, b"\x08", 0),                 # memory access: BGR order
    (0x3A, b"\x05", 0),                 # 16 bits per pixel (RGB565)
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


def rgb565(r, g, b):
    return (r & 0xF8) << 8 | (g & 0xFC) << 3 | b >> 3


class Eye:
    def __init__(self, gpio, cs):
        self.gpio = gpio
        self.spi = spidev.SpiDev()
        self.spi.open(6, cs)
        self.spi.max_speed_hz = HZ
        self.spi.mode = 0
        self.shown = None           # what the panel holds now, to send only changes

    def cmd(self, c, data=b""):
        lgpio.gpio_write(self.gpio, DC, 0)
        self.spi.writebytes2([c])
        if data:
            lgpio.gpio_write(self.gpio, DC, 1)
            self.spi.writebytes2(data)

    def init(self):
        for c, data, ms in INIT:
            self.cmd(c, data)
            if ms:
                time.sleep(ms / 1000)
        self.shown = None

    def window(self, x0, y0, x1, y1):
        self.cmd(0x2A, bytes([x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF]))
        self.cmd(0x2B, bytes([y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF]))

    def fill(self, colour):
        self.show(np.full((SIZE, SIZE), colour, np.uint16))

    def show(self, img):
        """Send a 240x240 uint16 RGB565 image, or just the rectangle of it
        that differs from what's on the panel."""
        if self.shown is None:
            y0, y1, x0, x1 = 0, SIZE - 1, 0, SIZE - 1
        else:
            diff = img != self.shown
            rows, cols = np.flatnonzero(diff.any(1)), np.flatnonzero(diff.any(0))
            if not len(rows):
                return
            y0, y1, x0, x1 = rows[0], rows[-1], cols[0], cols[-1]
        self.window(int(x0), int(y0), int(x1), int(y1))
        self.cmd(0x2C, img[y0:y1 + 1, x0:x1 + 1].astype(">u2").tobytes())
        self.shown = img.copy()


def open_eyes():
    """Claim the GPIOs, reset both panels (the reset line is shared) and
    initialise them, then turn the backlight on. Returns (gpio, eyes)."""
    gpio = lgpio.gpiochip_open(0)
    for pin, level in ((DC, 0), (RST, 1), (BL, 0)):
        lgpio.gpio_claim_output(gpio, pin, level)
    eyes = {name: Eye(gpio, cs) for name, cs in EYES.items()}
    reset(gpio, eyes)
    return gpio, eyes


def reset(gpio, eyes):
    """Reset and initialise both panels (the reset line is shared), blank
    them and turn the backlight on. Also brings back a panel that lost its
    settings to a loose wire."""
    lgpio.gpio_write(gpio, RST, 0)
    time.sleep(0.02)
    lgpio.gpio_write(gpio, RST, 1)
    time.sleep(0.12)
    for e in eyes.values():
        e.init()
        e.fill(0)
    lgpio.gpio_write(gpio, BL, 1)


def backlight(gpio, on):
    lgpio.gpio_write(gpio, BL, 1 if on else 0)


def close(gpio, eyes):
    try:
        for e in eyes.values():
            e.fill(0)
            e.spi.close()
        lgpio.gpio_write(gpio, BL, 0)
    finally:
        lgpio.gpiochip_close(gpio)
