#!/usr/bin/env python3
"""The Sense HAT's LED matrix: something to watch when she's idle, one of IDLES.

    blinkenlights   single warm white pixels (and the odd red one) blink on
                    and off at random, faster the busier her CPU is: a lazy
                    twinkle when she's idle, a frantic flicker flat out
    life            Conway's Game of Life on the 8x8, wrapping round at the
                    edges, a generation faster the busier her CPU is; newborn
                    cells are red for a generation. When it dies out, settles
                    into a loop or has run MAX_GENS, it waits a moment and
                    starts again.
    rain            the Matrix: green drops with fading tails falling down
                    random columns, thicker the busier her CPU is
    lava            a lava lamp: three soft blobs drifting and merging, deep
                    red at their edges to yellow at their hearts, drifting
                    faster the busier her CPU is
    patti           Patti's face, an emoji smiley: she blinks now and then,
                    and sometimes winks
                    (she also shows it for HELLO_S when she starts)
    shuffle         a different one of the others every SHUFFLE_S, at random

Anything more important takes the matrix over, and the idle one carries on
when it's done: first hold() and pop() (one of the little animated emojis
below), then an ear while someone listens through her or a speaker while
someone talks through her (intercom.py), then KITT's voice box while she talks
(speech), then a bouncing ellipsis while she's still on it but quiet (between
sentences, or thinking: waiting on Claude, or on friday writing down what
was said), then a kitchen timer (timers.py) draining like an hourglass.

Draws straight to the matrix's framebuffer (8x8, RGB565). It waits for
rp-bootstatus (pi/bootstatus) to finish its boot show first, and uses the
same ROTATE and BRIGHT settings (/etc/default/rp-bootstatus).

    python3 hat.py --demo      # each emoji for a few seconds, then exit
    python3 hat.py --idle life # any of IDLES until Ctrl-C
    python3 hat.py --ascii lava  # one in the terminal, no Sense HAT needed
"""
import asyncio
import collections
import contextlib
import glob
import math
import os
import random
import subprocess
import sys
import time

FPS = 20
# blinkenlights
LIT = 0.3                       # about this much of the matrix is lit
RATE_IDLE = 2.0                 # blinks a second with the CPU idle...
RATE_BUSY = 60.0                # ...and flat out
RED_CHANCE = 1 / 15             # of a new light being red
WARM = (255, 190, 120)
RED = (255, 20, 0)
LEVEL = 0.5                     # their brightness (of full; not scaled by BRIGHT)
# Dimmed (frac 0) they're sparser and a little fainter. Not much fainter: the
# Sense HAT's driver turns its lowest levels off, and warm white's weak blue
# and green go first, leaving it red.
LEVEL_DIM = 0.4
LIT_DIM = 0.08
LOAD_S = 0.5                    # how often to read the CPU load
IDLES = ("blinkenlights", "life", "rain", "lava", "patti", "shuffle")
SHUFFLE_S = 60
HELLO_S = 5                     # Patti herself, when she starts
# the Game of Life
GEN_IDLE = 2.0                  # generations a second with the CPU idle...
GEN_BUSY = 10.0                 # ...and flat out
SEED = 0.35                     # how much of the board a new start fills
SETTLED_S = 3.0                 # show a dead or looping board this long before starting again
LOOP = 16                       # a board seen in the last LOOP generations means it's looping
MAX_GENS = 300                  # and start again after this many anyway (a glider never loops in 16)
# Matrix rain
DROPS_IDLE = 1.5                # new drops a second with the CPU idle...
DROPS_BUSY = 8.0                # ...and flat out
DROP_SPEED = (5.0, 10.0)        # rows a second, each drop its own
TRAIL = 4                       # its tail, in pixels
RAIN_HEAD = (190, 255, 190)
# the lava lamp
BLOBS = 3
BLOB_R = 1.7                    # how big each blob is, in pixels
LAVA_EDGE = 0.8                 # field strength where a blob starts...
LAVA_CORE = 2.2                 # ...and where it's at its hottest
LAVA = ((110, 0, 0), (255, 60, 0), (255, 170, 20))     # edge, middle, heart
LAVA_IDLE = 0.6                 # drift speed with the CPU idle...
LAVA_BUSY = 2.0                 # ...and flat out


def settings():
    s = {"ROTATE": "0", "BRIGHT": "0.6"}
    try:
        with open("/etc/default/rp-bootstatus") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k in s and v:
                    s[k] = v
    except OSError:
        pass
    return int(s["ROTATE"]), float(s["BRIGHT"])


def find_fb():
    for f in glob.glob("/sys/class/graphics/fb*/name"):
        with open(f) as fh:
            if fh.read().strip() == "RPi-Sense FB":
                return "/dev/" + f.split("/")[-2]
    return None


class Matrix:
    def __init__(self, dev):
        self.fd = os.open(dev, os.O_WRONLY)
        self.rotate, self.bright = settings()

    def show(self, px, bright=None):
        """px: {(row, col): (r, g, b)}; anything missing is off. bright
        overrides the BRIGHT setting."""
        bright = self.bright if bright is None else bright
        buf = bytearray(128)
        for (r, c), col in px.items():
            for _ in range(self.rotate // 90):
                r, c = c, 7 - r
            if 0 <= r < 8 and 0 <= c < 8:
                red, green, blue = (max(0, min(255, int(v * bright))) for v in col)
                v = (red >> 3) << 11 | (green >> 2) << 5 | blue >> 3
                i = (r * 8 + c) * 2
                buf[i], buf[i + 1] = v & 0xFF, v >> 8
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, bytes(buf))

    def clear(self):
        self.show({})

    def close(self):
        try:
            self.clear()
        finally:
            os.close(self.fd)


# ---------- the emojis: each takes t (seconds since it went up), returns pixels ----------
def sprite(rows, palette):
    return {(r, c): palette[ch] for r, line in enumerate(rows) for c, ch in enumerate(line) if ch in palette}


def scale(px, f):
    return {p: tuple(v * f for v in col) for p, col in px.items()}


HEART = sprite(["........",
                ".RR..RR.",
                "RRRRRRRR",
                "RRRRRRRR",
                ".RRRRRR.",
                "..RRRR..",
                "...RR...",
                "........"], {"R": (255, 0, 40)})
HEART_SMALL = sprite(["........",
                      "........",
                      "..R..R..",
                      ".RRRRRR.",
                      "..RRRR..",
                      "...RR...",
                      "........",
                      "........"], {"R": (255, 0, 40)})


def heart(t):
    """Lub-dub, rest."""
    p = t % 1.0
    return HEART_SMALL if 0.15 < p < 0.25 or 0.4 < p < 0.5 else HEART


Y, K = (255, 190, 0), (40, 20, 0)
SMILEY_ROWS = ["..YYYY..",
               ".YYYYYY.",
               "YYKYYKYY",
               "YYYYYYYY",
               "YKYYYYKY",
               "YYKKKKYY",
               ".YYYYYY.",
               "..YYYY.."]
SMILEY = sprite(SMILEY_ROWS, {"Y": Y, "K": K})
SMILEY_BLINK = sprite(SMILEY_ROWS[:2] + ["YYYYYYYY"] + SMILEY_ROWS[3:], {"Y": Y, "K": K})


def smiley(t):
    """Blinks every few seconds."""
    return SMILEY_BLINK if t % 3.2 > 3.0 else SMILEY


G, C = (150, 150, 165), (0, 200, 255)
ROBOT = sprite(["........",
                "...A....",
                ".GGGGGG.",
                "GGCGGCGG",
                ".GGGGGG.",
                ".G....G.",
                ".GGGGGG.",
                "........"], {"G": G, "C": C, "A": G})


def robot(t):
    """Antenna light blinks; a light runs to and fro along her mouth."""
    px = dict(ROBOT)
    if t % 1.0 < 0.5:
        px[(0, 3)] = (255, 30, 0)
    x = 2 + round(1.5 + 1.5 * math.sin(t * 4))      # columns 2-5
    px[(5, x)] = C
    return px


EMOJIS = {"heart": heart, "smiley": smiley, "robot": robot}


# ---------- Patti's face: an emoji smiley ----------
# One strong shape and a few dark marks (LEDs left off) are all that reads on
# 8x8 LEDs with gaps between them; a portrait of her namesake (Patti Smith on
# Horses) didn't: skin came out nearly white, and the face never came together.
PS = (255, 190, 0)
PATTI = sprite(["..YYYY..",
                ".YYYYYY.",
                "YY.YY.YY",
                "YYYYYYYY",
                "Y.YYYY.Y",
                "YY....YY",
                ".YYYYYY.",
                "..YYYY.."], {"Y": PS})
PATTI_EYES = ((2, 2), (2, 5))


class PattiFace:
    colour = PS                     # (the chest sets it to its theme's: Hat.face)

    """Patti's face on her own Sense HAT: she blinks now and then, and every
    so often winks. frame(dt, load) as the other idles; patti(t) as an emoji."""

    def __init__(self):
        self.t = 0.0
        self.blink_at = 3.0
        self.wink = None            # which eye winks this time (None: a blink)

    def frame(self, dt, load=0.0):
        self.t += dt
        px = {p: self.colour for p in PATTI}
        if self.t >= self.blink_at:
            shut = PATTI_EYES if self.wink is None else PATTI_EYES[self.wink:self.wink + 1]
            for p in shut:
                px[p] = self.colour     # lid down: the eye goes face-coloured
            if self.t >= self.blink_at + (0.15 if self.wink is None else 0.5):
                self.blink_at = self.t + random.uniform(2.5, 6.0)
                self.wink = random.choice([0, 1]) if random.random() < 0.15 else None
        return px


_patti = PattiFace()


def patti(t):
    return _patti.frame(1 / FPS)


EMOJIS["patti"] = patti


# ---------- the intercom (intercom.py): someone listening, or talking, through her
E = (255, 140, 40)
EAR = sprite(["...EEE..",
              "..E...E.",
              ".E..E..E",
              ".E.E.E.E",
              ".E...E..",
              "..E.E...",
              "..E.E...",
              "...E...."], {"E": E})
S, W = (60, 160, 255), (200, 230, 255)
SPEAKER = sprite(["........",
                  "...S....",
                  "..SS..W.",
                  "SSSS.W..",
                  "SSSS.W..",
                  "..SS..W.",
                  "...S....",
                  "........"], {"S": S, "W": W})


def ear(t):
    """Someone's listening: an ear, breathing slowly so it catches the eye."""
    k = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(t * 2.5))
    return {p: tuple(int(v * k) for v in c) for p, c in EAR.items()}


def speaker(t):
    """Someone's talking through her: a speaker, its sound waves flickering."""
    px = dict(SPEAKER)
    if t % 0.5 < 0.25:
        for p in [(2, 6), (3, 5), (4, 5), (5, 6)]:
            px.pop(p, None)
    return px


# ---------- while she talks, and the timer ----------
KITT_ON, KITT_OFF = (255, 0, 0), (70, 0, 0)


def kitt(level):
    """KITT's voice box: three red bars, the middle one tallest, lit outwards
    from the middle row as loud as she is (0-1)."""
    px = {}
    for cols, reach in (((0, 1), 0.6), ((3, 4), 1.0), ((6, 7), 0.6)):
        half = level * reach * 4                # rows lit each way from the middle
        for r in range(8):
            k = abs(r - 3.5) - 0.5              # 0 by the middle, 3 at the ends
            b = max(0.0, min(1.0, half - k))
            col = tuple(int(off + (on - off) * b) for on, off in zip(KITT_ON, KITT_OFF))
            for c in cols:
                px[(r, c)] = col
    return px


GAP_S = 0.25            # quiet this long mid-reply (the next sentence not ready yet): the ellipsis


def ellipsis(t, colour):
    """Still going: three 2x2 dots, bouncing a row up and down one after
    another, like a chat app's "typing"."""
    px = {}
    for i, col in enumerate((0, 3, 6)):
        lift = max(0.0, math.sin(t * 2 * math.pi * 1.2 - i * 0.9))     # each a beat behind the last
        top = 3 - round(lift * 2)
        k = 0.55 + 0.45 * lift
        c = tuple(int(v * k) for v in colour)
        for r in (top, top + 1):
            for cc in (col, col + 1):
                px[(r, cc)] = c
    return px


SAND = (255, 150, 30)


def hourglass(frac, t):
    """What's left of a timer (1 to 0), as sand: it goes from the top row
    down, a pixel at a time; the last one to go pulses, and in the last tenth
    they all do."""
    n = min(64, max(1, int(math.ceil(frac * 64))))
    pulse = 0.55 + 0.45 * math.cos(t * 2 * math.pi)
    px = {}
    for i in range(64 - n, 64):
        b = pulse if i == 64 - n or frac < 0.1 else 1.0
        px[(i // 8, i % 8)] = tuple(v * b for v in SAND)
    return px


def ringing(t):
    """Time's up: the whole matrix flashes."""
    on = (t * 3) % 1.0 < 0.5
    return {(r, c): (255, 60, 0) if on else (60, 0, 0) for r in range(8) for c in range(8)}


class CpuLoad:
    """Fraction of all CPUs busy since the last read, from /proc/stat."""
    def __init__(self):
        self.last = self._read()

    @staticmethod
    def _read():
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        idle = v[3] + v[4]                  # idle + iowait
        return sum(v), idle

    def read(self):
        total, idle = self._read()
        dt, di = total - self.last[0], idle - self.last[1]
        self.last = (total, idle)
        return 0.0 if dt <= 0 else max(0.0, min(1.0, 1 - di / dt))


class Life:
    """Conway's Game of Life on an 8x8 torus."""

    def __init__(self):
        self.seed()

    def seed(self):
        self.cells = {(r, c) for r in range(8) for c in range(8) if random.random() < SEED}
        self.born = set(self.cells)
        self.seen = collections.deque([frozenset(self.cells)], maxlen=LOOP)
        self.settled = None             # time.monotonic() it died out or started looping
        self.gen = 0

    def step(self, now):
        if self.settled is not None:
            if now - self.settled >= SETTLED_S:
                self.seed()
            return
        counts = collections.Counter(((r + dr) % 8, (c + dc) % 8) for r, c in self.cells
                                     for dr in (-1, 0, 1) for dc in (-1, 0, 1) if dr or dc)
        new = {p for p, n in counts.items() if n == 3 or (n == 2 and p in self.cells)}
        self.born = new - self.cells
        self.cells = new
        self.gen += 1
        if not new or frozenset(new) in self.seen or self.gen >= MAX_GENS:
            self.settled = now
        self.seen.append(frozenset(new))

    def pixels(self):
        return {p: RED if p in self.born else WARM for p in self.cells}


class Rain:
    """The Matrix, on the 8x8."""

    def __init__(self):
        self.drops = []                 # [column, head's row (float), rows a second]

    def frame(self, dt, load):
        if random.random() < (DROPS_IDLE + (DROPS_BUSY - DROPS_IDLE) * load) * dt:
            busy = {d[0] for d in self.drops if d[1] < TRAIL}       # a column's last drop still near the top
            free = [c for c in range(8) if c not in busy]
            if free:
                self.drops.append([random.choice(free), 0.0, random.uniform(*DROP_SPEED)])
        for d in self.drops:
            d[1] += d[2] * dt
        self.drops = [d for d in self.drops if d[1] - TRAIL < 8]
        px = {}
        for c, row, _ in self.drops:
            head = int(row)
            for k in range(TRAIL + 1):
                r = head - k
                if 0 <= r < 8:
                    g = int(255 * (1 - k / (TRAIL + 1)) ** 1.5)
                    col = RAIN_HEAD if k == 0 else (0, g, 0)
                    if col[1] > px.get((r, c), (0, 0, 0))[1]:
                        px[(r, c)] = col
        return px


class Lava:
    """A lava lamp on the 8x8: metaballs."""

    def __init__(self):
        self.t = random.uniform(0, 1000)
        # each blob wanders on two slow sine waves, its own speeds and phases
        self.blobs = [(random.uniform(0.13, 0.3), random.uniform(0.1, 0.25),
                       random.uniform(0, 2 * math.pi), random.uniform(0, 2 * math.pi)) for _ in range(BLOBS)]

    def frame(self, dt, load):
        self.t += dt * (LAVA_IDLE + (LAVA_BUSY - LAVA_IDLE) * load)
        centres = [(3.5 + 3.2 * math.sin(self.t * wy + py), 3.5 + 3.2 * math.sin(self.t * wx + px))
                   for wx, wy, px, py in self.blobs]
        out = {}
        for r in range(8):
            for c in range(8):
                f = sum(BLOB_R ** 2 / ((r - y) ** 2 + (c - x) ** 2 + 0.4) for y, x in centres)
                if f < LAVA_EDGE:
                    continue
                v = min(1.0, (f - LAVA_EDGE) / (LAVA_CORE - LAVA_EDGE)) * 2      # 0-2 along the three colours
                a, b = LAVA[int(min(v, 1.999))], LAVA[int(min(v, 1.999)) + 1]
                k = v - int(min(v, 1.999))
                out[(r, c)] = tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))
        return out


class Hat:
    """The idle display (IDLES; set .idle). run() it as a task; hold() to
    take the matrix for a while, or pop() an emoji."""

    def __init__(self):
        dev = find_fb()
        self.m = Matrix(dev) if dev else None
        self.held_until = 0.0
        self.load = 0.0                 # smoothed CPU load, 0-1
        self.frac = 1.0                 # how bright, 0 (dimmed) to 1 (the chest follows its screen)
        self.lights = {}                # (row, col) -> colour
        self.speech = None              # speech.py's Speech: KITT's voice box while she talks
        self.timer = None               # timers.py's Timer
        self.intercom = None            # intercom.py's Intercom: an ear or a speaker, over everything
        self.mouth = 0.0                # the voice box's level, smoothed
        self.thinking_n = 0             # thinking() blocks going on: the ellipsis
        self.idle = IDLES[0]            # which idle display (the chest's Settings)
        self.shuffled = (IDLES[0], 0.0)     # "shuffle": showing which, till when
        self.life = Life()
        self.anims = {"rain": Rain(), "lava": Lava(), "patti": PattiFace()}

    @property
    def face(self):
        return self.anims["patti"].colour

    @face.setter
    def face(self, colour):
        """Her face's colour (the chest's theme), idle or popped up."""
        self.anims["patti"].colour = _patti.colour = tuple(colour)

    @contextlib.contextmanager
    def thinking(self):
        """with hat.thinking(): ... shows the ellipsis meanwhile (waiting on
        Claude, or on Whisper)."""
        self.thinking_n += 1
        try:
            yield
        finally:
            self.thinking_n -= 1

    def hold(self, seconds):
        """Keep the blinkenlights off the matrix for `seconds` (someone else is using it)."""
        self.held_until = max(self.held_until, time.monotonic() + seconds)

    async def pop(self, name, seconds):
        """Show one of the EMOJIS for a while, then go back to the blinkenlights."""
        self.hold(seconds)
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            self.m.show(EMOJIS[name](time.monotonic() - start))
            await asyncio.sleep(1 / FPS)

    async def boot_show_done(self):
        while True:
            p = await asyncio.create_subprocess_exec("systemctl", "is-active", "--quiet", "rp-bootstatus",
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if await p.wait() != 0:
                return
            await asyncio.sleep(1)

    def overlay(self, now):
        """The intercom (someone listening or talking through her: the room
        should always see that), else the voice box while she talks, else the
        ellipsis (quiet mid-reply, or thinking), else the timer, else None (the
        blinkenlights)."""
        ic = self.intercom.state() if self.intercom else None
        if ic == "talk":
            return speaker(now)
        if ic == "listen":
            return ear(now)
        sp = self.speech
        if sp and sp.speaking and not (sp.pending and now > sp.quiet_at + GAP_S):
            self.mouth = max(sp.level(), self.mouth * 0.75)     # up at once, down gently
            return kitt(self.mouth)
        self.mouth = 0.0
        if self.thinking_n or (sp and sp.speaking):     # (between sentences, or thinking)
            return ellipsis(now, self.face)
        t = self.timer
        if t and t.ringing:
            return ringing(now)
        f = t.fraction() if t else None
        if f is not None:
            return hourglass(f, now)
        return None

    def lit(self):
        return LIT_DIM + (LIT - LIT_DIM) * self.frac

    def blink(self):
        """One blink: a random pixel goes off, or (if it was off) maybe on."""
        p = (random.randrange(8), random.randrange(8))
        if p in self.lights:
            del self.lights[p]
        elif random.random() < self.lit() / (1 - self.lit()):   # settles at about lit() lit
            self.lights[p] = RED if random.random() < RED_CHANCE else WARM

    async def run(self, verbose=False):
        if not self.m:
            print("hat: no Sense HAT matrix", file=sys.stderr)
            return
        await self.boot_show_done()
        await self.pop("patti", HELLO_S)            # hello: it's her
        print("hat: idle display on (%s)" % self.idle, file=sys.stderr)
        cpu = CpuLoad()
        next_load = 0.0
        owed = 0.0                      # blinks due, carried between frames
        gens = 0.0                      # generations due
        try:
            while True:
                now = time.monotonic()
                if now >= next_load:
                    self.load += (cpu.read() - self.load) * 0.5
                    next_load = now + LOAD_S
                    if verbose:
                        print("load %3.0f%%" % (self.load * 100), flush=True)
                owed += (RATE_IDLE + (RATE_BUSY - RATE_IDLE) * self.load) / FPS
                while owed >= 1:
                    self.blink()
                    owed -= 1
                # When the density changes (the screen woke or dimmed), catch
                # up quickly rather than waiting on the blinks: a light a frame
                # on the way up, one every few frames on the way down.
                want = round(64 * self.lit())
                if len(self.lights) < want - 2:
                    free = [(r, c) for r in range(8) for c in range(8) if (r, c) not in self.lights]
                    self.lights[random.choice(free)] = RED if random.random() < RED_CHANCE else WARM
                elif len(self.lights) > want + 2 and random.random() < 0.3:
                    del self.lights[random.choice(list(self.lights))]
                mode = self.idle
                if mode == "shuffle":
                    if now >= self.shuffled[1]:
                        others = [i for i in IDLES if i not in ("shuffle", self.shuffled[0])]
                        self.shuffled = (random.choice(others), now + SHUFFLE_S)
                    mode = self.shuffled[0]
                anim = self.anims.get(mode)
                if anim:
                    px_idle = anim.frame(1 / FPS, self.load)
                if mode == "life":
                    gens += (GEN_IDLE + (GEN_BUSY - GEN_IDLE) * self.load) / FPS
                    while gens >= 1:
                        self.life.step(now)
                        gens -= 1
                if now >= self.held_until:
                    px = self.overlay(now)
                    if px is None:
                        idle = px_idle if anim else self.life.pixels() if mode == "life" else self.lights
                        # (Patti's a picture, not twinkles: full brightness, or she vanishes.)
                        self.m.show(idle, None if mode == "patti" else LEVEL_DIM + (LEVEL - LEVEL_DIM) * self.frac)
                    else:
                        self.m.show(px)
                await asyncio.sleep(1 / FPS)
        finally:
            self.m.clear()

    def close(self):
        if self.m:
            self.m.close()


if __name__ == "__main__":
    if "--demo" in sys.argv:
        h = Hat()

        async def demo():
            for name in EMOJIS:
                print(name)
                await h.pop(name, 4)
            h.m.clear()
        asyncio.run(demo())
    elif "--idle" in sys.argv:
        h = Hat()
        h.idle = sys.argv[sys.argv.index("--idle") + 1]
        try:
            asyncio.run(h.run(verbose=True))
        except KeyboardInterrupt:
            pass
    elif "--ascii" in sys.argv:
        # a few frames of one in the terminal, by brightness
        what = sys.argv[sys.argv.index("--ascii") + 1]
        a = {"rain": Rain, "lava": Lava, "patti": PattiFace}[what]()
        for n in range(6):
            for _ in range(FPS // 2):
                px = a.frame(1 / FPS, 0.0)
            for r in range(8):
                print(" ".join(" .:-=+*#@"[min(8, int(max(px.get((r, c), (0, 0, 0))) / 32))] for c in range(8)))
            print()
    else:
        print(__doc__)
