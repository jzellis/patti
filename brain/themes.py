"""Her chest screen's themes and fonts, for Settings (chest.py sends them to
the CYD, which keeps the last ones in its flash).

A theme is eight colours, by role:

    bg      the background
    faint   hairlines, the faintest text
    dim     secondary text, rules
    ink     the text
    bright  the clock and big numbers
    accent  titles and the icon bar
    good    good news (green lines)
    alert   bad news, and things to confirm (red lines, drawn highlighted)

A one-hue theme (mono() makes them) uses its ink for the last three, so it
stays one colour, only lighter or darker. A colourful one gives them their
own. Add one here and it's in Settings; nothing on the CYD to change.
"""

ROLES = ("bg", "faint", "dim", "ink", "bright", "accent", "good", "alert")


def mono(ink, bg=0x000000, faint=None, dim=None):
    """One hue on black: faint and dim are the ink, darker."""
    shade = lambda f: tuple(int(((ink >> s) & 0xFF) * f) for s in (16, 8, 0))
    faint = faint or shade(0.23)
    dim = dim or shade(0.54)
    rgb = lambda c: c if isinstance(c, tuple) else ((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)
    return [rgb(bg), rgb(faint), rgb(dim), rgb(ink), rgb(ink), rgb(ink), rgb(ink), rgb(ink)]


def inverse(paper, ink=0x000000):
    """Black on one hue: faint and dim are the paper, darker."""
    shade = lambda f: tuple(int(((paper >> s) & 0xFF) * f) for s in (16, 8, 0))
    i = ((ink >> 16) & 0xFF, (ink >> 8) & 0xFF, ink & 0xFF)
    return [shade(1.0), shade(0.8), shade(0.47), i, i, i, i, i]


THEMES = {
    "orange": mono(0xFF7018, faint=0x3A1905, dim=0x8A3C0C),   # Grundig orange
    "orange inverse": inverse(0xE85E10),
    "green": mono(0x33FF55),                                   # P1 phosphor
    "green inverse": inverse(0x2EE64D),
    "cyan": mono(0x40FFE0),                                    # a VFD
    "cyan inverse": inverse(0x38E6C8),
    "red": mono(0xFF2A1A),                                     # darkroom
    "red inverse": inverse(0xE62617),
    "white": mono(0xD8E4F0),                                   # P4, a little cool
    "white inverse": inverse(0xC8D4E0),
    # colourful: dusk blue, with peach text, a pink accent and a cyan clock
    "dusk": [(0x14, 0x12, 0x2E), (0x2E, 0x2A, 0x5C), (0x7A, 0x70, 0xB0), (0xFF, 0xD8, 0xB8),
             (0x5C, 0xE6, 0xFF), (0xFF, 0x5C, 0xA8), (0x6C, 0xF0, 0x8C), (0xFF, 0x50, 0x50)],
    # ...and by day: navy on a pale lavender white, the same pink, a deeper cyan
    "dusk light": [(0xF4, 0xEE, 0xF6), (0xD8, 0xCF, 0xE6), (0x8A, 0x80, 0xB0), (0x2A, 0x26, 0x50),
                   (0x00, 0x8C, 0xB4), (0xE0, 0x38, 0x8A), (0x1E, 0x9A, 0x50), (0xD8, 0x30, 0x30)],
}
# what the old settings called them
OLD = {"amber": "orange", "inverse": "orange inverse"}

# the CYD's body fonts (cyd.ino's FONTS): name in Settings -> the CYD's
FONTS = {
    "Terminus": "terminus",
    "Pixel Operator": "pixelop",
    "Pixel Operator Bold": "pixelopbold",
    "Toshiba T3100": "toshiba",
    "Silkscreen": "silkscreen",
}


def palette_command(name):
    """The CYD's "palette ..." line for a theme."""
    cols = THEMES.get(OLD.get(name, name)) or THEMES["orange inverse"]
    return "palette " + " ".join("%02X%02X%02X" % c for c in cols)


def face_colour(name):
    """Her face on the Sense HAT (hat.py's patti) in a theme's colour: its
    accent, or for an inverse one (black text) its paper."""
    cols = THEMES.get(OLD.get(name, name)) or THEMES["orange inverse"]
    accent = cols[ROLES.index("accent")]
    return cols[ROLES.index("bg")] if max(accent) < 64 else accent


def font_command(name):
    return "font " + FONTS.get(name, "terminus")


def next_in(names, current):
    names = list(names)
    return names[(names.index(current) + 1) % len(names)] if current in names else names[0]
