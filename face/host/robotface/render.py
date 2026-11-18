"""Render frames for the e-paper face, on the Pi, with PIL.

This is where the face gets its quality. The Pico can draw the presets and
some blocky text by itself (so the face works before the Pi has booted), but
anything with real typography is rendered here and shipped over as a packed
frame.

Output is always exactly protocol.FRAME_BYTES: PIL mode "1", row-major,
MSB-first, rows padded to a byte boundary -- which is both what
Image.tobytes() produces natively and what the Pico's framebuf wants as a
stride. No bit twiddling anywhere in the path.
"""

import os

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import expressions, protocol

W = protocol.WIDTH
H = protocol.HEIGHT

# PIL mode "1": 255 is white, 0 is black. Matches the panel's convention.
_WHITE = 255
_BLACK = 0

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/gnu-free/FreeSansBold.ttf",
)


def find_font(explicit=None):
    """Locate a usable bold sans TTF, or return None for PIL's bitmap default."""
    if explicit:
        if not os.path.exists(explicit):
            raise FileNotFoundError(explicit)
        return explicit
    env = os.environ.get("ROBOTFACE_FONT")
    if env and os.path.exists(env):
        return env
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    local = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "assets", "fonts")
    if os.path.isdir(local):
        for name in sorted(os.listdir(local)):
            if name.lower().endswith((".ttf", ".otf")):
                return os.path.join(local, name)
    return None


def blank():
    return Image.new("1", (W, H), _WHITE)


def pack(image):
    """Convert a PIL image to the wire frame format."""
    if image.mode != "1":
        image = image.convert("1")
    if image.size != (W, H):
        raise ValueError("expected %dx%d, got %dx%d" % (W, H, *image.size))
    data = image.tobytes()
    if len(data) != protocol.FRAME_BYTES:
        raise AssertionError(
            "packed %d bytes, protocol expects %d" % (len(data), protocol.FRAME_BYTES))
    return data


# --- expressions ------------------------------------------------------------

def draw_ops(d, ops, color=_BLACK):
    """Render shared-geometry ops with PIL.

    Same op list the Pico draws from, so a preview here matches the glass.
    """
    for op in ops:
        kind = op[0]
        if kind == "ellipse":
            _, cx, cy, rx, ry, fill = op
            box = (cx - rx, cy - ry, cx + rx, cy + ry)
            if fill:
                d.ellipse(box, fill=color)
            else:
                d.ellipse(box, outline=color, width=expressions.STROKE)
        elif kind == "line":
            _, x0, y0, x1, y1, w = op
            d.line((x0, y0, x1, y1), fill=color, width=w)
        elif kind == "poly":
            _, pts, w = op
            d.line([tuple(p) for p in pts], fill=color, width=w, joint="curve")
        else:
            raise ValueError("unknown op: %r" % (kind,))


def expression_image(name):
    img = blank()
    draw_ops(ImageDraw.Draw(img), expressions.ops(name))
    return img


def expression(name):
    return pack(expression_image(name))


# --- text -------------------------------------------------------------------

def _wrap(text, font, draw_ctx, max_width):
    """Greedy wrap using real glyph metrics, not a character count."""
    lines = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for word in words[1:]:
            trial = cur + " " + word
            if draw_ctx.textlength(trial, font=font) <= max_width:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def _layout(text, font_path, size, margin):
    """Wrap text at a given point size; return (lines, font, line_h, total_h)."""
    probe = ImageDraw.Draw(blank())
    if font_path:
        font = ImageFont.truetype(font_path, size)
    else:
        font = ImageFont.load_default()
    lines = _wrap(text, font, probe, W - 2 * margin)
    ascent, descent = font.getmetrics()
    line_h = int((ascent + descent) * 1.12)
    return lines, font, line_h, line_h * len(lines)


def text_image(text, font_path=None, size=None, margin=6, align="center"):
    """Render text, auto-fitting the point size to fill the panel.

    e-paper this small is unforgiving: too small and it is unreadable at
    robot-face distance, too big and it clips. So unless a size is given we
    binary-search the largest size whose wrapped block still fits.
    """
    font_path = find_font(font_path)

    if size is None:
        lo, hi, best = 6, 96, None
        while lo <= hi:
            mid = (lo + hi) // 2
            lines, font, line_h, total = _layout(text, font_path, mid, margin)
            widest = max(
                (ImageDraw.Draw(blank()).textlength(l, font=font) for l in lines),
                default=0)
            if total <= H - 2 * margin and widest <= W - 2 * margin:
                best = (mid, lines, font, line_h, total)
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            best = (6,) + _layout(text, font_path, 6, margin)
        size, lines, font, line_h, total = best
    else:
        lines, font, line_h, total = _layout(text, font_path, size, margin)

    img = blank()
    d = ImageDraw.Draw(img)
    y = max(margin, (H - total) // 2)
    for line in lines:
        if align == "left":
            x = margin
        else:
            x = max(margin, (W - d.textlength(line, font=font)) / 2)
        d.text((x, y), line, font=font, fill=_BLACK)
        y += line_h
    return img


def text(value, **kwargs):
    return pack(text_image(value, **kwargs))


# --- info screen ------------------------------------------------------------

def info_image(title, rows, font_path=None, margin=4):
    """Title in an inverted bar, then (label, value) rows in two columns.

    Picks the largest point size at which every row fits, so a host with one
    IP gets big readable text and one with three still fits.
    """
    font_path = find_font(font_path)
    d0 = ImageDraw.Draw(blank())

    def fonts(size):
        if font_path:
            return ImageFont.truetype(font_path, size)
        return ImageFont.load_default()

    title_font = fonts(18)
    title_h = 24
    body_h = H - title_h - 2 * margin
    gap = 6

    best = None
    for size in range(22, 7, -1):
        f = fonts(size)
        line_h = int(size * 1.15)
        label_w = max((d0.textlength(l, font=f) for l, _ in rows), default=0)
        widest = max((d0.textlength(v, font=f) for _, v in rows), default=0)
        if (len(rows) * line_h <= body_h
                and margin + label_w + gap + widest <= W - margin):
            best = (f, line_h, label_w)
            break
    if best is None:
        f = fonts(8)
        best = (f, 9, max((d0.textlength(l, font=f) for l, _ in rows), default=0))
    f, line_h, label_w = best

    img = blank()
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W - 1, title_h - 1), fill=_BLACK)
    d.text((margin, 2), title, font=title_font, fill=_WHITE)

    y = title_h + margin + max(0, (body_h - len(rows) * line_h) // 2)
    for label, value in rows:
        d.text((margin, y), label, font=f, fill=_BLACK)
        d.text((margin + label_w + gap, y), value, font=f, fill=_BLACK)
        y += line_h
    return img


def info(**kwargs):
    from . import sysinfo
    title, rows = sysinfo.gather()
    return pack(info_image(title, rows, **kwargs))


# --- sense hat screen -------------------------------------------------------

def sense_image(title, pairs, wide_rows, font_path=None, margin=4):
    """Title bar, then (label, value) pairs two to a line, then full-width
    rows (the x/y/z vectors). Largest point size at which it all fits.
    """
    font_path = find_font(font_path)
    d0 = ImageDraw.Draw(blank())

    def font(size):
        if font_path:
            return ImageFont.truetype(font_path, size)
        return ImageFont.load_default()

    title_font = font(16)
    title_h = 20
    body_h = H - title_h - margin
    gap, col_gap = 4, 12
    cols = (pairs[0::2], pairs[1::2])
    lines = len(cols[0]) + len(wide_rows)

    def width(items, f):
        return max((d0.textlength(t, font=f) for t in items), default=0)

    def column(items, f):
        """(label width, total width) of a label/value column."""
        lw = width([l for l, _ in items], f)
        return lw, lw + gap + width([v for _, v in items], f)

    for size in range(20, 7, -1):
        f = font(size)
        line_h = int(size * 1.12)
        (l0, w0), (l1, w1), (lw, ww) = (column(c, f) for c in cols + (wide_rows,))
        if (lines * line_h <= body_h
                and w0 + col_gap + w1 <= W - 2 * margin
                and ww <= W - 2 * margin):
            break

    img = blank()
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W - 1, title_h - 1), fill=_BLACK)
    d.text((margin, 1), title, font=title_font, fill=_WHITE)

    def row(x, y, label, value, label_w):
        d.text((x, y), label, font=f, fill=_BLACK)
        d.text((x + label_w + gap, y), value, font=f, fill=_BLACK)

    y = title_h + margin + max(0, (body_h - lines * line_h) // 2)
    for i, (label, value) in enumerate(cols[0]):
        row(margin, y, label, value, l0)
        if i < len(cols[1]):
            row(margin + w0 + col_gap, y, *cols[1][i], l1)
        y += line_h
    for label, value in wide_rows:
        row(margin, y, label, value, lw)
        y += line_h
    return img


def sense(**kwargs):
    from . import sensehat
    return pack(sense_image(*sensehat.gather(), **kwargs))


# --- arbitrary images -------------------------------------------------------

def image_file(path, dither=True, invert=False, fit="contain", autocontrast=False):
    """Load any image file and fit it to the panel as 1-bit.

    Dithering is on by default because it is usually what you want for
    photos; turn it off (threshold instead) for line art and logos, where
    dither just looks like noise at this resolution.

    fit: "contain" letterboxes on white, "cover" crops to fill the panel,
    "stretch" distorts. autocontrast stretches the grey levels first, which
    rescues dim webcam frames that would otherwise dither to flat grey.
    """
    src = Image.open(path)
    if src.mode in ("RGBA", "LA", "P"):
        flat = Image.new("RGB", src.size, (255, 255, 255))
        src = src.convert("RGBA")
        flat.paste(src, mask=src.split()[-1])
        src = flat
    src = src.convert("L")

    if fit == "stretch":
        src = src.resize((W, H), Image.LANCZOS)
    elif fit == "cover":
        src = ImageOps.fit(src, (W, H), Image.LANCZOS, centering=(0.5, 0.5))
    else:
        src.thumbnail((W, H), Image.LANCZOS)
        canvas = Image.new("L", (W, H), _WHITE)
        canvas.paste(src, ((W - src.width) // 2, (H - src.height) // 2))
        src = canvas

    if autocontrast:
        src = ImageOps.autocontrast(src, cutoff=1)
    if invert:
        src = src.point(lambda v: 255 - v)

    if dither:
        out = src.convert("1")
    else:
        out = src.point(lambda v: 255 if v >= 128 else 0, mode="1")
    return out
