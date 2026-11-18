#!/usr/bin/env python3
"""Cartoon eyes on the two round panels: white eyeball, black pupil with a
highlight, black lids for blinking.

    eyes = Eyes()           # opens the panels, starts the animation thread
    eyes = Eyes(picoeyes, gimbal)   # the same, panels on the gimbal Pico
    eyes.look(x, y)         # -1..1; +x her right, +y up. Idle wandering
                            # resumes IDLE_AFTER_S after the last look()
    eyes.blink()
    eyes.reset()            # re-initialise the panels (after a loose wire)
    eyes.set_state("drowsy")  # "awake", "drowsy" (lids droop), "asleep" (shut, backlight off)
    eyes.set_mood("angry")  # an expression, one of MOODS; stays until changed
    eyes.close()

On its own it's a demo: a tour of the directions (to check they're the right
way round), then idle glancing and blinking until Ctrl-C.
"""
import math
import random
import sys
import threading
import time

import numpy as np

import gc9a01        # the panels on the Pi's SPI6; picoeyes has them on the gimbal Pico

FPS = 30
RIM = 116               # eyeball radius in pixels (the panel is 120)
PUPIL = 46
GLINT = 11              # the white highlight in the pupil
REACH = RIM - PUPIL - 6  # how far the pupil centre can move from the middle
EASE = 0.45             # fraction of the way to the target per frame (a quick saccade)
IDLE_AFTER_S = 2.0
# Only the changed part of the picture is sent, so a write garbled on the way
# (loose or long wires) would stay; redraw everything this often to heal it.
REFRESH_S = 3.0
BLINK_S = (0.07, 0.04, 0.11)   # closing, closed, opening
# How far the lids are closed in each state, and how long they take to get
# there: drifting off is slow, waking up is quick (then a double blink).
STATES = {"awake": 0.0, "drowsy": 0.35, "asleep": 1.0}
FALL_S = 4.0            # time to close fully
WAKE_S = 0.25           # time to open fully
# Mounting: the panels show their picture to someone in front of her, so her
# right is the viewer's left. Flip if they end up mounted differently.
MIRROR_X = True
# Which way (screen x, as seen from in front) each eye's nose corner is, for
# the moods with slanted lids: her left eye is on the viewer's right.
NOSE = {"left": -1, "right": 1}
HAL_EYE = NOSE["right"]     # HAL has only the one

WHITE = gc9a01.rgb565(255, 255, 255)
BLACK = 0
RED = gc9a01.rgb565(230, 30, 60)

_y, _x = np.mgrid[0:gc9a01.SIZE, 0:gc9a01.SIZE] - (gc9a01.SIZE - 1) / 2
_OUTSIDE = _x ** 2 + _y ** 2 > RIM ** 2
# The spiral for each way of turning; drawing it is then one subtraction.
_SPIRAL = {n: np.hypot(_x, _y) / 26 - n * np.arctan2(_y, _x) / (2 * np.pi) for n in (1, -1)}

def _rgb565(rgb):
    """float RGB (0..255, last axis) -> RGB565 array"""
    c = np.clip(rgb, 0, 255).astype(np.uint16)
    return (c[..., 0] >> 3) << 11 | (c[..., 1] >> 2) << 5 | c[..., 2] >> 3


# Johnny 5's camera eyes: (outer radius, colour, how much the top-left light
# shades it: + for a raised ring, - for a sunken one), outside in.
JOHNNY_RINGS = [
    (999, (70, 70, 74), 0.25),      # housing
    (110, (205, 205, 210), 0.35),   # chrome rim
    (98, (35, 35, 38), 0.0),
    (92, (195, 150, 90), 0.3),      # brass
    (70, (30, 28, 26), 0.0),
    (64, (215, 215, 220), -0.35),   # chrome bevel into the lens
]
JOHNNY_LENS = 56
JOHNNY_SHIFT = 0.3          # the whole lens barrel turns, so it shifts, but less than a pupil
_johnny_barrel = None


def _disc(img, cx, cy, r, colour):
    """Fill a circle, working only on the square around it."""
    c = gc9a01.SIZE / 2
    y0, y1 = max(0, int(c + cy - r)), min(gc9a01.SIZE, int(c + cy + r) + 2)
    x0, x1 = max(0, int(c + cx - r)), min(gc9a01.SIZE, int(c + cx + r) + 2)
    sub = img[y0:y1, x0:x1]
    sub[(_x[y0:y1, x0:x1] - cx) ** 2 + (_y[y0:y1, x0:x1] - cy) ** 2 < r * r] = colour


def _johnny(px, py, nose, t):
    global _johnny_barrel
    m = int(REACH * JOHNNY_SHIFT) + 2
    if _johnny_barrel is None:
        # Drawn once, a little bigger than the panel; each frame is a crop of it.
        n = gc9a01.SIZE + 2 * m
        dy, dx = np.mgrid[0:n, 0:n] - (n - 1) / 2
        r = np.hypot(dx, dy)
        light = (-dx - dy) / (np.sqrt(2) * np.maximum(r, 1))     # 1 facing the top-left light
        rgb = np.zeros(r.shape + (3,), np.float32)
        for outer, colour, shade in JOHNNY_RINGS:
            ring = r < outer
            rgb[ring] = np.array(colour, np.float32) * (1 + shade * light[ring])[:, None]
        lens = r < JOHNNY_LENS
        glass = (1 - 0.45 * (r[lens] / JOHNNY_LENS) ** 2) * (1 - 0.15 * light[lens])
        rgb[lens] = np.array((125, 140, 155), np.float32) * glass[:, None]
        _johnny_barrel = _rgb565(rgb)
    cx, cy = round(px * JOHNNY_SHIFT), round(py * JOHNNY_SHIFT)
    img = _johnny_barrel[m - cy:m - cy + gc9a01.SIZE, m - cx:m - cx + gc9a01.SIZE].copy()
    # The pupil sits a little further toward where she's looking; the glints
    # are reflections, so they stay put on the lens.
    _disc(img, cx + px * 0.15, cy + py * 0.15, 17, _rgb565(np.array((15, 15, 20))))
    _disc(img, cx - 20, cy - 22, 8, WHITE)
    _disc(img, cx + 15, cy + 17, 3, _rgb565(np.array((190, 190, 190))))
    return img


_hal_lens = None


def _hal(px, py, nose, t):
    """HAL 9000's lens: chrome ring, black glass, red glow with a yellow-white
    core, and the reflections of the ceiling lights. It never moves."""
    global _hal_lens
    if _hal_lens is None:
        r = np.hypot(_x, _y)
        a = np.degrees(np.arctan2(_y, _x))          # -90 is straight up
        light = np.cos(np.radians(a + 135))         # top-left light
        rgb = np.zeros(_x.shape + (3,), np.float32)
        ring = (r >= 100) & (r < 116)
        bevel = 1 - np.abs((r[ring] - 108) / 8) ** 2
        grey = (55 + 55 * bevel) * (1 + 0.4 * light[ring])
        rgb[ring] = grey[:, None] * np.array((1, 1, 1.05), np.float32)
        lens = r < 100
        rr = r[lens]
        # (The panels show colours brighter and warmer than a monitor, so the
        # glow has little green, or it goes orange.)
        rgb[lens, 0] = 255 * np.clip(1.1 - (rr / 64) ** 2.5, 0.03, 1)
        rgb[lens, 1] = 200 * np.exp(-(rr / 6) ** 2) + 8 * np.exp(-(rr / 20) ** 2)
        rgb[lens, 2] = 90 * np.exp(-(rr / 4) ** 2) + 6
        # Reflections: (inner r, outer r, from angle, to angle, brightness).
        for r0, r1, a0, a1, b in ((84, 91, -112, -68, 170), (74, 79, -150, -136, 130),
                                  (74, 79, -44, -30, 130), (62, 67, -98, -82, 150),
                                  (55, 58, -132, -122, 100), (55, 58, -58, -48, 100),
                                  (34, 37, -106, -74, 90), (26, 29, -100, -80, 75)):
            m = (r >= r0) & (r < r1) & (a >= a0) & (a <= a1)
            rgb[m] = np.maximum(rgb[m], b)
        _hal_lens = _rgb565(rgb)
    if nose != HAL_EYE:
        return np.zeros(_x.shape, np.uint16)
    return _hal_lens.copy()


def _canvas(margin):
    """Coordinate grids (dx, dy, r, light) for a picture margin pixels bigger
    than the panel on each side, centred on it; light is 1 where a surface
    faces the top-left light, -1 facing away."""
    n = gc9a01.SIZE + 2 * margin
    dy, dx = np.mgrid[0:n, 0:n] - (n - 1) / 2
    r = np.hypot(dx, dy)
    return dx, dy, r, (-dx - dy) / (np.sqrt(2) * np.maximum(r, 1))


def _crop(canvas, margin, cx, cy):
    """The panel's view of a _canvas picture shifted by (cx, cy)."""
    return canvas[margin - cy:margin - cy + gc9a01.SIZE,
                  margin - cx:margin - cx + gc9a01.SIZE].copy()


def _blur(a, k):
    """Box blur, k pixels, both ways (for smooth noise)."""
    for axis in (0, 1):
        c = np.cumsum(np.pad(a, [(k, k) if i == axis else (0, 0) for i in (0, 1)], mode="edge"), axis)
        a = (np.take(c, range(2 * k, c.shape[axis]), axis) - np.take(c, range(0, c.shape[axis] - 2 * k), axis)) / (2 * k)
    return a


SHIFT = 0.3                 # the characters' eyes turn as a whole, so move less than a pupil
SHIFT_M = int(REACH * SHIFT) + 2
_cache = {}


def _shift(px, py):
    return round(px * SHIFT), round(py * SHIFT)


# Arnie: a red lens in a mechanical iris of red blades that keeps opening
# and closing, in a dark socket. Flat colours and black outlines, cartoon style.
ARNIE_OPEN = (30, 58)       # iris radius, closed and open
ARNIE_HOLD_S = (0.4, 3.5)   # how long the iris rests before it moves again
ARNIE_BLADES = 8


def _arnie_layers():
    dx, dy, r, light = _canvas(SHIFT_M)
    rgb = np.zeros(r.shape + (3,), np.float32)
    # Socket, in black.
    rgb[...] = (40, 12, 14)
    rgb[r >= 95] = 0
    rgb[(r >= ARNIE_OPEN[1] + 6) & (r < ARNIE_OPEN[1] + 10)] = 0   # round the iris
    base = _rgb565(rgb)
    # The lens, revealed as far as the iris is open.
    lens = np.zeros(r.shape + (3,), np.float32)
    lens[..., 0] = 255 * (0.5 + 0.5 * np.exp(-(r / 28) ** 2))
    lens[..., 1] = 210 * np.exp(-(r / 5) ** 2) + 25 * np.exp(-(r / 14) ** 2)
    lens[..., 2] = 170 * np.exp(-(r / 5) ** 2) + 15 * np.exp(-(r / 14) ** 2)
    for k in (11, 19, 28, 38, 49):
        lens[np.abs(r - k) < 1.3] *= 0.5
    lens[np.hypot(dx + 15, dy + 17) < 4] = 255
    # Just the middle (where the iris works), each frame, as panel-sized windows.
    w = ARNIE_OPEN[1] + 6
    c = r.shape[0] // 2
    win = (slice(c - w, c + w), slice(c - w, c + w))
    return {"base": base, "r": r[win], "ang": np.arctan2(dy, dx)[win] / (2 * np.pi),
            "light": light[win], "lens": _rgb565(lens)[win], "under": base[win], "w": w}


def _arnie(px, py, nose, t):
    a = _cache.get("arnie") or _cache.setdefault("arnie", _arnie_layers())
    cx, cy = _shift(px, py)
    img = _crop(a["base"], SHIFT_M, cx, cy)
    closed, opened = ARNIE_OPEN
    # Now and then a new size: sometimes a snap, sometimes a slow drift.
    # (Both eyes are drawn for the same t, so they move together.)
    s = a.setdefault("iris", {"ap": opened, "to": opened, "rate": 3.0, "next": t, "t": t})
    if t != s["t"]:
        s["ap"] += (s["to"] - s["ap"]) * min(1.0, (t - s["t"]) * s["rate"])
        s["t"] = t
        if t >= s["next"]:
            s["to"] = random.uniform(closed, opened)
            s["rate"] = random.choice((2.0, 4.0, 15.0))
            s["next"] = t + random.uniform(*ARNIE_HOLD_S)
    ap = s["ap"]
    r = a["r"]
    # Aperture blades from the lens out: curved seams that turn as the iris
    # closes. Red, a little darker toward the seam under the next blade, with
    # black outlines.
    phase = (a["ang"] * ARNIE_BLADES + (r - ap) / 22 - ap / 40) % 1
    red = 215 - 70 * phase
    outline = (phase > 1 - 3.5 * ARNIE_BLADES / (2 * np.pi * np.maximum(r, 1))) | (r < ap + 3)
    red[outline] = 0
    rr = red.astype(np.uint16)
    g = (rr * 0.12).astype(np.uint16)
    blades = (rr >> 3) << 11 | (g >> 2) << 5 | (g >> 3)
    win = np.where(r < ap, a["lens"], np.where(r < opened + 6, blades, a["under"]))
    c, w = gc9a01.SIZE // 2, a["w"]
    img[c + cy - w:c + cy + w, c + cx - w:c + cx + w] = win
    return img


# Wall-E: big dark binocular lenses with purple glass, in a scuffed grey
# housing whose top edge slopes down at the outer corner (the sad tilt).
def _walle_layer(nose):
    dx, dy, r, light = _canvas(SHIFT_M)
    rng = np.random.default_rng(7)
    noise = rng.random((r.shape[0] // 6 + 1,) * 2)
    noise = _blur(np.kron(noise, np.ones((6, 6)))[:r.shape[0], :r.shape[1]], 4)
    rgb = np.zeros(r.shape + (3,), np.float32)
    housing = np.array((200, 194, 186), np.float32)
    shade = (0.75 + 0.25 * noise) * (1 - 0.35 * (r / 130) ** 3) * (1 + 0.1 * light)
    rgb[...] = housing * shade[..., None]
    # Lens, a bit below the middle.
    ly = dy - 10
    lr = np.hypot(dx, ly)
    llight = (-dx - ly) / (np.sqrt(2) * np.maximum(lr, 1))
    rgb[lr < 76] = (50, 48, 50)
    edge = (lr >= 70) & (lr < 76)
    rgb[edge] = ((110 + 50 * llight[edge]) * 1)[:, None]
    rgb[lr < 64] = (22, 18, 22)
    glass = lr < 58
    f = 0.25 + 0.75 * np.exp(-((lr[glass] - 36) / 15) ** 2)
    rgb[glass] = np.array((120, 35, 85), np.float32) * f[:, None]
    rgb[lr < 21] = (14, 5, 12)
    rgb[(lr >= 21) & (lr < 23)] = (45, 18, 35)
    ang = np.degrees(np.arctan2(ly, dx))
    arc = (lr > 42) & (lr < 49) & (ang > -165) & (ang < -115)
    rgb[arc] = (215, 150, 195)
    for gx, gy, gr in ((-14, -14, 5), (8, -26, 2.5), (22, 28, 3)):
        rgb[np.hypot(dx - gx, ly - gy) < gr] = 245
    # The top edge of the housing, with a dark seam below it.
    u = dx * nose / RIM
    top = RIM * (-0.74 - 0.16 * u)
    rgb[dy < top + 5] = (70, 64, 58)
    rgb[dy < top] = 0
    return _rgb565(rgb)


def _walle(px, py, nose, t):
    key = ("walle", nose)
    layer = _cache.get(key)
    if layer is None:
        layer = _cache[key] = _walle_layer(nose)
    return _crop(layer, SHIFT_M, *_shift(px, py))


# Uwu: big vertical anime irises, navy going to blue at the bottom, two big
# glints, thick lashes flicking out at the outer corner.
UWU_IRIS = (60, 86)         # half width, half height
UWU_MOVE = 0.4


def _uwu_layers(nose):
    x, y = _x, _y
    u = x * nose            # + toward the nose
    # Upper lash: an arch, thicker toward the outer corner, with flicks.
    def arch(x):
        return -70 + 0.0042 * x ** 2
    line = arch(x) + 0.05 * u
    thick = 11 + 9 * np.clip(-u / 95, 0, 1)
    lash = (y < line) & (y > line - thick) & (np.abs(x) < 104)
    for x0, deg, ln in ((70, 70, 20), (84, 45, 26), (98, 20, 26)):
        a = math.radians(deg)
        sx, sy = -nose * x0, arch(x0) - 4
        vx, vy = -nose * math.cos(a) * ln, -math.sin(a) * ln
        # Distance to the segment, tapering.
        tt = np.clip(((x - sx) * vx + (y - sy) * vy) / (vx * vx + vy * vy), 0, 1)
        d = np.hypot(x - sx - tt * vx, y - sy - tt * vy)
        lash |= d < 6 * (1 - tt) + 1
    # A short lower lash at the outer corner.
    lower = 108 - 0.004 * x ** 2
    lash |= (y > lower - 4) & (y < lower + 1) & (u < -25) & (u > -85)
    base = np.full(x.shape, WHITE, np.uint16)
    opening = y > line                          # the iris shows only below the lash
    # The iris, as a sprite.
    a, b = UWU_IRIS
    sy_, sx_ = np.mgrid[-b - 2:b + 3, -a - 2:a + 3].astype(np.float32)
    e = (sx_ / a) ** 2 + (sy_ / b) ** 2
    t = np.clip((sy_ + b) / (2 * b), 0, 1) ** 1.4
    rgb = np.array((15, 25, 75), np.float32) * (1 - t)[..., None] + np.array((50, 105, 200), np.float32) * t[..., None]
    inner = ((sx_ / 36) ** 2 + ((sy_ + 8) / 54) ** 2) < 1
    rgb[inner] *= 0.45
    band = (e < 0.9) & ((sx_ / (a - 6)) ** 2 + ((sy_ + 14) / (b - 4)) ** 2 > 1)
    rgb[band] = (80, 140, 225)
    rgb[e > (1 - 10 / a)] = 0                   # outline
    for gx, gy, gr in ((-20, -40, 18), (22, 46, 8)):
        rgb[np.hypot(sx_ - gx, sy_ - gy) < gr] = 255
    return {"base": base, "lash": lash, "opening": opening,
            "iris": _rgb565(rgb), "mask": e < 1, "h": b + 2, "w": a + 2}


def _uwu(px, py, nose, t):
    key = ("uwu", nose)
    L = _cache.get(key)
    if L is None:
        L = _cache[key] = _uwu_layers(nose)
    img = L["base"].copy()
    c, h, w = gc9a01.SIZE // 2, L["h"], L["w"]
    cx, cy = round(px * UWU_MOVE), round(py * UWU_MOVE) + 10
    ys, xs = slice(c + cy - h, c + cy + h + 1), slice(c + cx - w, c + cx + w + 1)
    mask = L["mask"] & L["opening"][ys, xs]
    img[ys, xs][mask] = L["iris"][mask]
    img[L["lash"]] = BLACK
    return img


# Old-school VU meters: her left eye shows the Pi's CPU, her right its RAM.
# A backlit cream face, a scale with a red zone, a black needle with a VU
# meter's ballistics (a quick rise with a little overshoot).
VU_PIVOT = (0, 78)          # needle pivot, screen pixels from the middle (y down)
VU_R = (118, 136)           # scale arc: tick inner / outer radius from the pivot
VU_SWING = 48               # degrees either side of straight up
VU_RED = 0.8                # red zone from here to full scale
VU_NEEDLE = 146
VU_SPRING = (90.0, 13.0)    # stiffness, damping: settles in ~0.3 s, ~2% overshoot
_vu_faces = {}
_vu_needle = {}             # nose -> [position, speed, last t]
_vu_stat = {"at": -1.0, "cpu": 0.0, "ram": 0.0, "prev": None}


def _vu_read(t):
    """CPU (since the last read) and RAM in use, 0..1; read at most 5 times a second."""
    st = _vu_stat
    if t - st["at"] < 0.2:
        return st
    st["at"] = t
    try:
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        idle, total = v[3] + v[4], sum(v[:8])
        if st["prev"]:
            dt, di = total - st["prev"][1], idle - st["prev"][0]
            if dt > 0:
                st["cpu"] = 1 - di / dt
        st["prev"] = (idle, total)
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, val = line.split(":")
                mem[k] = int(val.split()[0])
        st["ram"] = 1 - mem["MemAvailable"] / mem["MemTotal"]
    except (OSError, ValueError, KeyError):
        pass
    return st


def _vu_angle(v):
    return math.radians(-VU_SWING + 2 * VU_SWING * v)       # 0 = straight up


def _vu_face(label):
    """The meter face, drawn once (with PIL, for the lettering)."""
    from PIL import Image, ImageDraw, ImageFont
    n = gc9a01.SIZE
    r = np.hypot(_x, _y)
    # Warm backlit paper, brightest above the pivot, darker toward the rim.
    glow = np.hypot(_x, _y + 10) / 120
    rgb = np.zeros((n, n, 3), np.float32)
    rgb[:] = np.array((245, 228, 160), np.float32)
    rgb *= np.clip(1.05 - 0.35 * glow ** 2, 0.55, 1)[..., None]
    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    cx, cy = n / 2 + VU_PIVOT[0], n / 2 + VU_PIVOT[1]

    def font(size):
        for name in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                pass
        return ImageFont.load_default()

    def at(v, rad):
        a = _vu_angle(v)
        return cx + rad * math.sin(a), cy - rad * math.cos(a)

    # the red zone: a thick band along the outside of the scale
    steps = 40
    for i in range(steps):
        v0 = VU_RED + (1 - VU_RED) * i / steps
        v1 = VU_RED + (1 - VU_RED) * (i + 1) / steps
        d.polygon([at(v0, VU_R[1] - 6), at(v0, VU_R[1] + 2), at(v1, VU_R[1] + 2), at(v1, VU_R[1] - 6)],
                  fill=(200, 25, 20))
    # the scale's arc
    d.line([at(i / 200, VU_R[1] - 6) for i in range(201)], fill=(20, 18, 15), width=2)
    # ticks: long and numbered every 20 %, short every 5 %
    f = font(13)
    for i in range(21):
        v = i / 20
        major = i % 4 == 0
        inner = VU_R[0] if major else VU_R[1] - 14
        colour = (180, 20, 15) if v > VU_RED + 1e-6 else (20, 18, 15)
        d.line([at(v, inner), at(v, VU_R[1] - 6)], fill=colour, width=3 if major else 2)
        if major:
            x, y = at(v, VU_R[0] - 12)
            d.text((x, y), str(i * 5), font=f, fill=colour, anchor="mm")
    d.text((n / 2, cy - 64), label, font=font(26), fill=(20, 18, 15), anchor="mm")
    d.text((n / 2, cy - 40), "%", font=font(14), fill=(90, 80, 60), anchor="mm")
    # the pivot's black cover along the bottom
    d.ellipse([cx - 34, cy - 20, cx + 34, cy + 48], fill=(25, 23, 22))
    out = np.asarray(img, np.float32).copy()
    # dark bezel round the edge, lit from the top left
    ring = (r >= 108) & (r < 116)
    light = (-_x - _y) / (np.sqrt(2) * np.maximum(r, 1))
    out[ring] = (40 + 30 * light[ring])[:, None] * np.array((1, 1, 1.05), np.float32)
    return _rgb565(out)


def _vu(px, py, nose, t):
    label, key = ("CPU", "cpu") if nose == NOSE["left"] else ("RAM", "ram")
    if label not in _vu_faces:
        _vu_faces[label] = _vu_face(label)
    target = min(1.0, max(0.0, _vu_read(t)[key]))
    s = _vu_needle.setdefault(nose, [0.0, 0.0, t])
    dt = min(0.1, max(0.0, t - s[2]))
    for _ in range(4):                      # a few small steps keep the spring steady
        h = dt / 4
        s[1] += (VU_SPRING[0] * (target - s[0]) - VU_SPRING[1] * s[1]) * h
        s[0] += s[1] * h
    s[2] = t
    img = _vu_faces[label].copy()
    # the needle: a thin black line from the pivot (only its square is worked out)
    a = _vu_angle(min(1.04, max(-0.04, s[0])))
    c = gc9a01.SIZE / 2
    x0, y0 = VU_PIVOT
    x1, y1 = x0 + VU_NEEDLE * math.sin(a), y0 - VU_NEEDLE * math.cos(a)
    xa, xb = int(max(0, c + min(x0, x1) - 3)), int(min(gc9a01.SIZE, c + max(x0, x1) + 4))
    ya, yb = int(max(0, c + min(y0, y1) - 3)), int(min(gc9a01.SIZE, c + max(y0, y1) + 4))
    gx, gy = _x[ya:yb, xa:xb], _y[ya:yb, xa:xb]
    dx, dy = x1 - x0, y1 - y0
    u = np.clip(((gx - x0) * dx + (gy - y0) * dy) / (dx * dx + dy * dy), 0, 1)
    dist = np.hypot(gx - x0 - u * dx, gy - y0 - u * dy)
    sub = img[ya:yb, xa:xb]
    sub[(dist < 1.6) & ~_OUTSIDE[ya:yb, xa:xb] & (np.hypot(gx - x0, gy - y0) > 26)] = _rgb565(np.array((15, 12, 10)))
    img[_OUTSIDE] = BLACK
    return img


# Expressions. Lid edges are functions of u, across the eye from -1 (the
# outer corner) to 1 (the corner by her nose), giving the y in pixels (down
# is +) that the upper lid comes down to / the lower lid comes up to.
# pupil: size; heart: a beating red heart for a pupil; spiral: a spinning
# spiral instead of a pupil; blink: False for none; draw: a function (px, py, nose, t) -> image
# that draws the whole eye instead; anim: redraw every frame; glance: (mean y,
# spread x, spread y) for idle wandering.
MOODS = {
    "normal": {},
    "happy": {"bottom": lambda u: RIM * (0.5 - 0.4 * (1 - u * u))},
    "angry": {"top": lambda u: RIM * (-0.45 + 0.4 * u)},
    "sad": {"top": lambda u: RIM * (-0.45 - 0.35 * u), "glance": (-0.4, 0.3, 0.15)},
    "surprised": {"pupil": 0.55},
    "suspicious": {"top": lambda u: -RIM * 0.3 + 0 * u, "bottom": lambda u: RIM * 0.3 + 0 * u,
                   "glance": (0.0, 0.8, 0.05)},
    "love": {"heart": True, "anim": True},
    "dizzy": {"spiral": True, "anim": True},
    "johnny": {"draw": _johnny},
    "hal": {"draw": _hal, "blink": False},
    "arnie": {"draw": _arnie, "anim": True, "blink": False},
    "walle": {"draw": _walle},
    "uwu": {"draw": _uwu},
    "vu": {"draw": _vu, "anim": True, "blink": False},
}


def render(px, py, lid, mood="normal", nose=1, t=0.0):
    """One eye: pupil at (px, py) in pixels from the middle (screen axes, y
    down), lids closed by the fraction lid (0 open, 1 shut), with the
    expression mood. nose is the screen-x direction (1 or -1) of the corner
    by her nose; t is the time, for the animated moods."""
    m = MOODS[mood]
    img = np.full(_x.shape, WHITE, np.uint16)
    if m.get("draw"):
        img = m["draw"](px, py, nose, t)
    elif m.get("spiral"):
        # One arm, turning; the two eyes turn opposite ways.
        img[(_SPIRAL[nose] - t * 1.2) % 1 < 0.5] = BLACK
    elif m.get("heart"):
        size = PUPIL * 0.9 * (1 + 0.12 * max(0.0, math.sin(t * 2 * math.pi * 1.2)) ** 4)
        # Only work out the square around the pupil.
        c, h = gc9a01.SIZE // 2, int(size * 1.3) + 2
        ys = slice(max(0, c + py - h), c + py + h)
        xs = slice(max(0, c + px - h), c + px + h)
        hx, hy = (_x[ys, xs] - px) / size, -(_y[ys, xs] - py) / size + 0.15
        sub = img[ys, xs]
        sub[(hx * hx + hy * hy - 1) ** 3 - hx * hx * hy ** 3 <= 0] = RED
    else:
        r = PUPIL * m.get("pupil", 1.0)
        img[(_x - px) ** 2 + (_y - py) ** 2 < r ** 2] = BLACK
        gx, gy = px - r * 0.35, py - r * 0.35
        img[(_x - gx) ** 2 + (_y - gy) ** 2 < (GLINT * r / PUPIL) ** 2] = WHITE
    img[_OUTSIDE] = BLACK
    if lid > 0 or "top" in m or "bottom" in m:
        # Upper lid comes down most of the way, lower lid up to meet it,
        # each with a slight curve.
        top = -RIM + lid * RIM * 1.25 + 10 * lid * (_x / RIM) ** 2
        bottom = RIM - lid * RIM * 0.75 - 8 * lid * (_x / RIM) ** 2
        u = _x[0] * nose / RIM
        if "top" in m:
            top = np.maximum(top, m["top"](u))
        if "bottom" in m:
            bottom = np.minimum(bottom, m["bottom"](u))
        img[(_y < top) | (_y > bottom)] = BLACK
    return img


class Eyes:
    def __init__(self, hw=gc9a01, *args):
        """hw is the panels' driver, gc9a01 or picoeyes; args go to its open_eyes()."""
        self.hw = hw
        self.gpio, self.panels = hw.open_eyes(*args)
        self.pos = [0.0, 0.0]           # where the pupils are now (-1..1)
        self.target = [0.0, 0.0]
        self.told_at = -1e9             # last look() from outside
        self.next_glance = time.monotonic() + 1
        self.next_blink = time.monotonic() + random.uniform(2, 5)
        self.blink_at = None
        self.stopping = False
        self.resetting = False
        self.sleepy = 0.0               # how far the lids are closed by sleepiness now
        self.sleepy_target = 0.0
        self.state = "awake"
        self.mood = "normal"
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def look(self, x, y, hold=0.0):
        """Pupils toward (x, y), -1..1 (+y up). They stay there for hold
        seconds, then IDLE_AFTER_S more before glancing about again."""
        d = math.hypot(x, y)
        if d > 1:
            x, y = x / d, y / d
        self.target = [x, y]
        self.told_at = time.monotonic() + max(0.0, hold)

    def blink(self):
        self.blink_at = time.monotonic()

    def reset(self):
        self.resetting = True       # done by the drawing thread, between frames

    def set_state(self, state):
        if state == self.state:
            return
        target = STATES[state]
        if target < self.sleepy_target:
            # Waking up: once the lids are open, blink twice.
            now = time.monotonic()
            self.blink_at = now + WAKE_S * self.sleepy
            self.next_blink = self.blink_at + 0.35
            self.next_glance = now
        self.state, self.sleepy_target = state, target
        print("eyes: %s" % state, file=sys.stderr)

    def set_mood(self, mood):
        if mood not in MOODS:
            raise ValueError("no mood %r" % mood)
        if mood != self.mood:
            self.mood = mood
            self.next_glance = time.monotonic()
            print("eyes: mood %s" % mood, file=sys.stderr)

    def _lid(self, now):
        if not MOODS[self.mood].get("blink", True):
            self.blink_at = None
            return 0.0
        if self.blink_at is None:
            if now < self.next_blink:
                return 0.0
            if self.sleepy_target >= 1:         # asleep: no blinking
                self.next_blink = now + 1
                return 0.0
            self.blink_at = now
            # Now and then a double blink; sleepy eyes blink less often.
            self.next_blink = now + (0.35 if random.random() < 0.15
                                     else random.uniform(2.5, 6) * (1 + 2 * self.sleepy))
        t = now - self.blink_at
        if t < 0:
            return 0.0
        close, hold, open_ = (s * (1 + 2 * self.sleepy) for s in BLINK_S)   # and slowly
        if t < close:
            return t / close
        if t < close + hold:
            return 1.0
        if t < close + hold + open_:
            return 1 - (t - close - hold) / open_
        self.blink_at = None
        return 0.0

    def _idle(self, now):
        if (now - self.told_at < IDLE_AFTER_S or now < self.next_glance
                or self.sleepy_target >= 1):
            return
        # Mostly near the middle, now and then further out; a sleepy gaze
        # sinks and wanders less.
        y, sx, sy = MOODS[self.mood].get("glance", (0.0, 0.4, 0.3))
        self.target = [max(-1, min(1, random.gauss(0, sx))),
                       max(-1, min(1, random.gauss(y - 0.6 * self.sleepy, sy)))]
        self.next_glance = now + random.uniform(1.0, 4.0) * (1 + 3 * self.sleepy)

    def _doze(self, dt):
        """Move the lids toward the state's closedness; backlight off once shut."""
        if self.sleepy < self.sleepy_target:
            self.sleepy = min(self.sleepy_target, self.sleepy + dt / FALL_S)
        elif self.sleepy > self.sleepy_target:
            self.sleepy = max(self.sleepy_target, self.sleepy - dt / WAKE_S)
        lit = self.sleepy < 1
        if lit != self.lit:
            if lit:
                self.hw.backlight(self.gpio, True)
            self.lit = lit
            return not lit      # turn it off after drawing the shut eye
        return False

    def _run(self):
        drawn = None
        self.lit = True
        last = refreshed = time.monotonic()
        try:
            while not self.stopping:
                if self.resetting:
                    self.resetting = False
                    self.hw.reset(self.gpio, self.panels)   # (backlight back on)
                    drawn, self.lit = None, True
                    print("eyes: reset", file=sys.stderr)
                now = time.monotonic()
                if now - refreshed > getattr(self.hw, "REFRESH_S", REFRESH_S):
                    refreshed, drawn = now, None
                    for p in self.panels.values():
                        p.shown = None
                dark = self._doze(now - last)
                last = now
                self._idle(now)
                for i in range(2):
                    self.pos[i] += (self.target[i] - self.pos[i]) * EASE
                lid = max(self.sleepy, self._lid(now))
                px = round(self.pos[0] * REACH * (-1 if MIRROR_X else 1))
                py = round(-self.pos[1] * REACH)
                mood = self.mood
                anim = now if MOODS[mood].get("anim") else 0
                if (px, py, lid, mood, anim) != drawn:      # still: no drawing, no SPI
                    drawn = (px, py, lid, mood, anim)
                    for name, p in self.panels.items():
                        p.show(render(px, py, lid, mood, NOSE[name], anim))
                if dark:
                    self.hw.backlight(self.gpio, False)
                time.sleep(max(0, 1 / FPS - (time.monotonic() - now)))
        finally:
            self.hw.close(self.gpio, self.panels)

    def close(self):
        self.stopping = True
        self.thread.join(timeout=2)


def main():
    eyes = Eyes()
    try:
        for name, x, y in (("her left", -1, 0), ("her right", 1, 0),
                           ("up", 0, 1), ("down", 0, -1), ("ahead", 0, 0)):
            print("looking", name)
            eyes.look(x, y)
            time.sleep(1.5)
        eyes.blink()
        print("blink, then idle (Ctrl-C to stop)")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        eyes.close()


if __name__ == "__main__":
    main()
