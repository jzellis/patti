#!/usr/bin/env python3
"""Turn a pixel font (TTF) into an Adafruit GFX font header for the CYD.

Renders each printable ASCII character at the font's own pixel size, keeps
the pixels over half lit (so no anti-aliasing), and packs them the GFX way.
LovyanGFX draws these, at any whole-number size.

    python3 fontconvert.py Silkscreen-Regular.ttf 8 Silkscreen > silkscreen.h
    (name, pixel size, C name; several fonts: run it once each and concatenate)

Needs Pillow.
"""
import sys

from PIL import Image, ImageDraw, ImageFont


def convert(path, size, name):
    font = ImageFont.truetype(path, size)
    ascent, descent = font.getmetrics()
    bits, glyphs = [], []
    for code in range(0x20, 0x7F):
        ch = chr(code)
        im = Image.new("L", (size * 4, size * 4), 0)
        ox, base = size, size * 2              # where the pen starts, and the baseline
        ImageDraw.Draw(im).text((ox, base), ch, font=font, fill=255, anchor="ls")
        px = im.load()
        on = [(x, y) for y in range(im.height) for x in range(im.width) if px[x, y] > 127]
        adv = round(font.getlength(ch))
        if not on:
            glyphs.append((len(bits) // 8, 0, 0, adv, 0, 0))
            continue
        x0, x1 = min(x for x, _ in on), max(x for x, _ in on)
        y0, y1 = min(y for _, y in on), max(y for _, y in on)
        start = len(bits)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                bits.append(1 if px[x, y] > 127 else 0)
        while len(bits) % 8:                    # each glyph starts on a byte
            bits.append(0)
        glyphs.append((start // 8, x1 - x0 + 1, y1 - y0 + 1, adv, x0 - ox, y0 - base))
    data = [sum(b << (7 - i) for i, b in enumerate(bits[k:k + 8])) for k in range(0, len(bits), 8)]
    out = ["// %s, %d px, from %s by cyd/fontconvert.py" % (name, size, path.split("/")[-1]),
           "static const uint8_t %s_bitmap[] = {" % name]
    for k in range(0, len(data), 16):
        out.append("  " + ", ".join("0x%02x" % v for v in data[k:k + 16]) + ",")
    out.append("};")
    out.append("static const lgfx::GFXglyph %s_glyphs[] = {" % name)
    for code, g in zip(range(0x20, 0x7F), glyphs):
        out.append("  {%d, %d, %d, %d, %d, %d},  // %r" % (g + (chr(code),)))
    out.append("};")
    out.append("static const lgfx::GFXfont %s((uint8_t *)%s_bitmap, (lgfx::GFXglyph *)%s_glyphs, 0x20, 0x7e, %d);"
               % (name, name, name, ascent + descent))
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    print(convert(sys.argv[1], int(sys.argv[2]), sys.argv[3]))
