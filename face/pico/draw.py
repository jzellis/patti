"""Draw shared expression geometry, and text, into a MicroPython framebuf.

The Pico only needs this so the face works standalone -- at boot, or any
time the Pi is down. For good typography the Pi renders a full frame and
sends it over; see host/robotface/render.py.
"""

import framebuf

import expressions
import protocol

BLACK = protocol.BLACK
WHITE = protocol.WHITE


def _stroke_segment(fb, x0, y0, x1, y1, w, color):
    """Draw a line of width w by sweeping a square kernel along it.

    Crude, but every offset line is a C-speed framebuf.line call, joins come
    out clean with no mitring maths, and at w=5 on a 0.194mm dot pitch the
    square cap is invisible.
    """
    r = w // 2
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            fb.line(x0 + dx, y0 + dy, x1 + dx, y1 + dy, color)


def _stroke_poly(fb, pts, w, color):
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        _stroke_segment(fb, x0, y0, x1, y1, w, color)


def draw_ops(fb, ops, color=BLACK):
    """Render a shared-geometry op list into fb."""
    for op in ops:
        kind = op[0]
        if kind == "ellipse":
            _, cx, cy, rx, ry, fill = op
            fb.ellipse(int(cx), int(cy), int(rx), int(ry), color, fill)
        elif kind == "line":
            _, x0, y0, x1, y1, w = op
            _stroke_segment(fb, x0, y0, x1, y1, w, color)
        elif kind == "poly":
            _, pts, w = op
            _stroke_poly(fb, pts, w, color)
        else:
            raise ValueError("unknown op: " + str(kind))


def expression(fb, name):
    """Clear fb to white and draw a named preset expression on it."""
    fb.fill(WHITE)
    draw_ops(fb, expressions.ops(name))


# --- text -------------------------------------------------------------------
#
# framebuf's built-in font is a fixed 8x8. At 1x that is 31 columns across a
# 250px panel and reads as tiny. We scale it with nearest-neighbour: render
# into a scratch buffer, then stamp each set pixel as a scale x scale block.

GLYPH_W = 8
GLYPH_H = 8


def _draw_scaled_line(fb, text, x, y, scale, color):
    if scale == 1:
        fb.text(text, x, y, color)
        return
    w = GLYPH_W * len(text)
    stride = (w + 7) // 8 * 8
    scratch = bytearray(stride * GLYPH_H // 8)
    sfb = framebuf.FrameBuffer(scratch, w, GLYPH_H, framebuf.MONO_HLSB, stride)
    sfb.fill(0)
    sfb.text(text, 0, 0, 1)
    for sy in range(GLYPH_H):
        for sx in range(w):
            if sfb.pixel(sx, sy):
                fb.fill_rect(x + sx * scale, y + sy * scale, scale, scale, color)


def wrap(text, scale, width=protocol.WIDTH):
    """Greedy word wrap to whatever fits the panel at this scale."""
    cols = max(1, width // (GLYPH_W * scale))
    lines = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for word in words[1:]:
            if len(cur) + 1 + len(word) <= cols:
                cur += " " + word
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def text_block(fb, text, scale=3, color=BLACK):
    """Clear fb to white and draw centred, wrapped text on it."""
    fb.fill(WHITE)
    lines = wrap(text, scale)
    line_h = GLYPH_H * scale + max(2, scale)
    total = line_h * len(lines) - max(2, scale)
    y = max(0, (protocol.HEIGHT - total) // 2)
    for line in lines:
        x = max(0, (protocol.WIDTH - GLYPH_W * scale * len(line)) // 2)
        _draw_scaled_line(fb, line, x, y, scale, color)
        y += line_h
    return len(lines)


def blit_host_frame(fb, data):
    """Blit a host-rendered frame (PIL mode "1" bytes) into the panel buffer.

    The host sends row-major MSB-first pixels; the landscape panel buffer is
    column-packed. Rather than transpose 30,500 pixels in Python, we wrap the
    received bytes in a framebuf of the host's layout and let framebuf.blit
    convert formats in C.
    """
    src = framebuf.FrameBuffer(
        data, protocol.WIDTH, protocol.HEIGHT,
        framebuf.MONO_HLSB, protocol.STRIDE_PX,
    )
    fb.blit(src, 0, 0)
