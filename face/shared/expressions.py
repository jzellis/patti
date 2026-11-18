"""Patti's facial expressions, as resolution-independent geometry.

Imported by BOTH the Pico (MicroPython) and the Pi (CPython), so the two
sides draw byte-identical faces and the Pi's preview is trustworthy.

An expression is a list of drawing ops. Keeping them as data rather than as
drawing code means the art lives in one place while each side strokes it
with its own primitives -- framebuf on the Pico, PIL on the Pi. Arcs are
sampled into polylines here, in shared code, so the curve geometry cannot
drift between the two renderers.

Ops:
    ("ellipse", cx, cy, rx, ry, fill)      fill is True/False
    ("line",    x0, y0, x1, y1, w)
    ("poly",    [(x, y), ...], w)          open polyline, stroked width w

All coordinates are in panel pixels: 250 wide, 122 tall, origin top-left,
y increasing downward. Everything draws black on white.
"""

import math

WIDTH = 250
HEIGHT = 122

# Eyes
EYE_Y = 46
EYE_L_X = 78
EYE_R_X = 172
EYE_R = 14

STROKE = 5          # mouth / brow stroke width
ARC_SAMPLES = 48    # points per arc; smooth at this panel's dot pitch


def arc(cx, cy, rx, ry, deg_start, deg_end, samples=ARC_SAMPLES):
    """Sample an elliptical arc into a list of points.

    Angles are in degrees, measured with y increasing DOWNWARD (screen
    convention): 0 is right of centre, 90 is below it, 180 is left. So an
    arc from 20 to 160 bulges downward (a smile) and one from 200 to 340
    bulges upward (a frown).
    """
    pts = []
    for i in range(samples + 1):
        t = math.radians(deg_start + (deg_end - deg_start) * i / samples)
        pts.append((cx + rx * math.cos(t), cy + ry * math.sin(t)))
    return pts


def _eyes(open_=True):
    if open_:
        return [
            ("ellipse", EYE_L_X, EYE_Y, EYE_R, EYE_R, True),
            ("ellipse", EYE_R_X, EYE_Y, EYE_R, EYE_R, True),
        ]
    # Half-lidded: a filled lower crescent reads as "down" on 2-bit mono
    # better than an outline does.
    return [
        ("line", EYE_L_X - EYE_R, EYE_Y, EYE_L_X + EYE_R, EYE_Y, STROKE),
        ("line", EYE_R_X - EYE_R, EYE_Y, EYE_R_X + EYE_R, EYE_Y, STROKE),
    ]


def _sad_brows():
    # Sadness raises the INNER brow ends and drops the outer ones. (Anger is
    # the opposite; getting this backwards is the classic mistake.)
    return [
        ("line", 58, 26, 96, 14, STROKE),    # left:  outer low -> inner high
        ("line", 192, 26, 154, 14, STROKE),  # right: mirrored
    ]


def _build():
    smile = _eyes() + [("poly", arc(125, 72, 48, 24, 20, 160), STROKE)]

    frown = _eyes() + [("poly", arc(125, 100, 46, 22, 200, 340), STROKE)]

    flat = _eyes() + [("line", 82, 92, 168, 92, STROKE)]

    sad = (
        _eyes()
        + _sad_brows()
        + [("poly", arc(125, 100, 40, 18, 200, 340), STROKE)]
    )

    return {"smile": smile, "frown": frown, "sad": sad, "flat": flat}


EXPRESSIONS = _build()
NAMES = tuple(sorted(EXPRESSIONS))


def ops(name):
    """Return the op list for a named expression, or raise KeyError."""
    return EXPRESSIONS[name]
