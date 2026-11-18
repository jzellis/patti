"""Eye test on the gimbal Pico (Kitronik board): fills each eye a colour.

    mpremote run eyes/pico/eyetest.py    # runs from RAM; nothing is copied

Her left eye red, her right eye blue, then both green, then both off.
Wiring: the "Kitronik Eye Wiring" chart. The servo headers follow the datasheet
(header n is GP(n+1)), checked 2026-10-31: yellow on header 5 (GP6, SPI0 clock),
green on 6 (GP7, data), purple on 7 (GP8, reset), blue on 8 (GP9, DC), and the
reds on 3 and 4 (GP4 is her left eye, GP5 her right). LED on the breakout's GP27.
"""
import time
from machine import Pin, SPI

SCK, MOSI, DC, RST, BL = 6, 7, 9, 8, 27
CS = {"left": 4, "right": 5}
SIZE = 240
HZ = 20_000_000     # gentle for a first test on Dupont leads

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
    (0x35, b"", 0), (0x21, b"", 0), (0x11, b"", 120), (0x29, b"", 20),
]

spi = SPI(0, baudrate=HZ, polarity=0, phase=0, sck=Pin(SCK), mosi=Pin(MOSI))
dc, rst, bl = Pin(DC, Pin.OUT, value=0), Pin(RST, Pin.OUT, value=1), Pin(BL, Pin.OUT, value=0)
cs = {name: Pin(p, Pin.OUT, value=1) for name, p in CS.items()}


def cmd(eye, c, data=b""):
    cs[eye](0)
    dc(0)
    spi.write(bytes([c]))
    if data:
        dc(1)
        spi.write(data)
    cs[eye](1)


def rgb565(r, g, b):
    return (r & 0xF8) << 8 | (g & 0xFC) << 3 | b >> 3


def fill(eye, colour):
    cmd(eye, 0x2A, b"\x00\x00\x00\xEF")
    cmd(eye, 0x2B, b"\x00\x00\x00\xEF")
    rows = 16
    buf = bytes([colour >> 8, colour & 0xFF]) * (SIZE * rows)
    cs[eye](0)
    dc(0)
    spi.write(b"\x2C")
    dc(1)
    for _ in range(SIZE // rows):
        spi.write(buf)
    cs[eye](1)


rst(0)
time.sleep_ms(20)
rst(1)
time.sleep_ms(120)
for eye in cs:
    for c, data, ms in INIT:
        cmd(eye, c, data)
        if ms:
            time.sleep_ms(ms)
    fill(eye, 0)
bl(1)

RED, BLUE, GREEN = rgb565(255, 0, 0), rgb565(0, 0, 255), rgb565(0, 255, 0)
print("her left eye red, her right eye blue")
fill("left", RED)
fill("right", BLUE)
time.sleep(4)
print("both green")
fill("left", GREEN)
fill("right", GREEN)
time.sleep(4)
for eye in cs:
    fill(eye, 0)
bl(0)
print("done")
