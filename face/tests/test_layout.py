"""Verify the host -> panel byte path without any hardware.

Three things in this project are pure index arithmetic, are easy to get
subtly wrong, and are horrible to debug by squinting at an e-paper panel:

  1. What PIL's mode "1" tobytes() actually emits (row padding, bit order).
  2. Whether a MONO_HLSB framebuf over that data, blitted into the panel's
     MONO_VLSB buffer, lands the pixels where we think.
  3. Whether face.Face._push()'s slice-based reorder emits byte-for-byte
     what the vendor driver's per-byte display() loop emits.

So we model framebuf in plain Python here and assert all three. Run with
    python3 tests/test_layout.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "host"))

import protocol                      # noqa: E402
from robotface import render         # noqa: E402

# The vendored driver's landscape class does:
#     self.width  = 128   (122 rounded up to a byte multiple)
#     self.height = 250
#     super().__init__(buffer, self.height, self.width, framebuf.MONO_VLSB)
# i.e. a framebuf 250 px wide and 128 px tall, column-packed.
FB_W = 250
FB_H = 128
BANDS = FB_H // 8
BUF_LEN = FB_W * FB_H // 8          # 4000


class VLSB:
    """Minimal MONO_VLSB framebuf: byte (y//8)*width + x, bit y%8."""

    def __init__(self, w=FB_W, h=FB_H):
        self.w, self.h = w, h
        self.buf = bytearray(w * h // 8)

    def pixel(self, x, y, v=None):
        idx = (y // 8) * self.w + x
        bit = 1 << (y & 7)
        if v is None:
            return 1 if self.buf[idx] & bit else 0
        if v:
            self.buf[idx] |= bit
        else:
            self.buf[idx] &= ~bit & 0xFF

    def fill(self, v):
        self.buf[:] = b"\xff" * len(self.buf) if v else bytes(len(self.buf))


class HLSB:
    """Minimal MONO_HLSB framebuf with an explicit stride, in pixels."""

    def __init__(self, data, w, h, stride):
        self.data, self.w, self.h, self.stride = data, w, h, stride

    def pixel(self, x, y):
        idx = (y * self.stride + x) >> 3
        return 1 if self.data[idx] & (0x80 >> (x & 7)) else 0


def blit(dst, src):
    """What framebuf.blit does across differing formats: getpixel/setpixel."""
    for y in range(src.h):
        for x in range(src.w):
            dst.pixel(x, y, src.pixel(x, y))


def vendor_stream(buf):
    """Byte order the vendor landscape display() sends, byte by byte."""
    width, height = FB_H, FB_W      # driver's own naming: 128, 250
    out = bytearray()
    for j in range(width // 8 - 1, -1, -1):
        for i in range(0, height):
            out.append(buf[i + j * height])
    return bytes(out)


def fast_stream(buf):
    """The slice-based reorder used by face.Face._push()."""
    out = bytearray(BUF_LEN)
    pos = 0
    for band in range(BANDS - 1, -1, -1):
        out[pos:pos + FB_W] = buf[band * FB_W:(band + 1) * FB_W]
        pos += FB_W
    return bytes(out)


def check(label, cond):
    print("%-58s %s" % (label, "ok" if cond else "FAIL"))
    if not cond:
        raise SystemExit(1)


def main():
    # 1. PIL's packing, verified against a single known pixel.
    from PIL import Image
    img = Image.new("1", (protocol.WIDTH, protocol.HEIGHT), 255)
    img.putpixel((9, 3), 0)                       # one black pixel
    data = img.tobytes()
    check("PIL mode '1' emits %d bytes" % protocol.FRAME_BYTES,
          len(data) == protocol.FRAME_BYTES)
    check("PIL rows pad to %d bytes" % protocol.ROW_BYTES,
          len(data) // protocol.HEIGHT == protocol.ROW_BYTES)
    byte_i = 3 * protocol.ROW_BYTES + (9 >> 3)
    check("black pixel (9,3) clears the right bit, MSB-first",
          data[byte_i] == (0xFF & ~(0x80 >> (9 & 7))))

    # 2. HLSB -> VLSB blit preserves every pixel of a real expression.
    packed = render.expression("smile")
    src = HLSB(packed, protocol.WIDTH, protocol.HEIGHT, protocol.STRIDE_PX)
    dst = VLSB()
    dst.fill(1)
    blit(dst, src)
    mismatches = sum(
        1
        for y in range(protocol.HEIGHT)
        for x in range(protocol.WIDTH)
        if dst.pixel(x, y) != src.pixel(x, y)
    )
    check("blit preserves all %d px of 'smile'" % (protocol.WIDTH * protocol.HEIGHT),
          mismatches == 0)

    # The 6 padding rows below the visible 122 must stay white, or they show
    # up as a black band along one edge of the panel.
    pad_black = sum(
        1 for y in range(protocol.HEIGHT, FB_H) for x in range(FB_W)
        if dst.pixel(x, y) == 0
    )
    check("padding rows %d..%d left white" % (protocol.HEIGHT, FB_H - 1),
          pad_black == 0)

    # 3. The fast reorder must be byte-identical to the vendor loop.
    check("fast reorder == vendor display() stream",
          fast_stream(dst.buf) == vendor_stream(dst.buf))
    check("stream is %d bytes" % BUF_LEN, len(fast_stream(dst.buf)) == BUF_LEN)

    # 4. Checksum catches a desynced payload.
    good = render.expression("flat")
    shifted = good[1:] + b"\x00"
    check("checksum differs on a 1-byte framing slip",
          protocol.checksum(good) != protocol.checksum(shifted))

    print("\nall layout checks passed")


if __name__ == "__main__":
    main()
