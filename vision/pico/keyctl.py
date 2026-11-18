"""Gimbal control loop over USB serial, and her eyes. Driven by tools/keys.py
(keyboard), host/track.py (face tracking) and brain/server.py.

    import keyctl; keyctl.run()
    import keyctl; keyctl.run((12.0, -3.0))    # starting where the head already is (no snap)
    import keyctl; keyctl.run((12.0, -3.0), hold=True)  # servos off till G (eyes on meanwhile)

Reads one-byte commands from stdin and moves both axes toward a target angle,
one PWM frame per tick, without ever blocking -- so a held arrow key (which
the host sends as a stream of repeats) gives smooth continuous motion. Each
axis accelerates and brakes (see Axis) instead of starting and stopping dead,
and the target can change at any time mid-move.

    L / R / U / D   nudge the target STEP_DEG left / right / up / down
    A<pan>,<tilt>\n absolute target in degrees, e.g. "A12.5,-3\n" (for scripts;
                    no status line, so a 15Hz tracker doesn't flood the port)
    G               go: start the servos (after run(hold=True): her eyes work
                    from the start, the servos wait till the Pi has finished
                    booting, so their first pulses don't land while it's busy)
    P               report where the servos actually are right now:
                    "AT <pan> <tilt>" in degrees (the tracker uses this to
                    know where the camera was pointing when a frame was taken)
    c               target back to centre
    x               release: stop pulses (servos hold or go slack, see
                    Servo.detach); the next nudge re-attaches
    q, Ctrl-C       quit: detach and return to the REPL

The eye panels (eyepanels.py, if it's on the board and the panels answer):

    E<eye><x0><y0><x1><y1><pixels>
                    draw a rectangle, x0..x1 by y0..y1 inclusive, on eye 0
                    (her left) or 1 (her right): five single bytes, then the
                    rectangle's RGB565 big-endian, row by row
    Z<eye><x0><y0><x1><y1><m><runs>
                    the same, run-length coded: m is a big-endian 16-bit
                    byte count, then m/4 runs of (count, colour), both
                    big-endian 16-bit
    I               re-initialise the panels (blanks them, backlight on)
    B0 / B1         backlight off / on

Ctrl-C can't be a keyboard interrupt here (pixel data can contain the byte),
so it's read as a command instead: quit. Bytes that aren't commands, zeros
included, are ignored -- the host sends a run of zeros to finish a frame it
was cut off in the middle of.

Targets are clamped to the calibrated limits as they're set, so holding a key
against a limit doesn't wind up a target that then takes ages to unwind.
After every command it prints one status line: "POS <pan> <tilt>" in degrees.
"""

import micropython
import select
import sys
import time

import gimbal

try:
    import eyepanels
except ImportError:
    eyepanels = None

STEP_DEG = 3
CHUNK = 2048        # read eye frames this much at a time, keeping the servos ticking
RLE_MAX = 32768     # as eyepanels.RLE_MAX (here too for when it isn't on the board)
# Gentle on purpose: the servos share her 12V supply with the Pi, and a hard
# start draws a burst that can brown it out. (Was 900 and 3000: snappier,
# and jerkier.)
SPEED_US_S = 700    # top speed, ~63 deg/s
ACCEL_US_S2 = 1500  # ~135 deg/s^2: full speed in about half a second, so moves ease in and out
# Never slower than this while moving: an MG996R ignores changes under its
# ~5us dead band, so creeping 1-4us a frame (all of a short move, and the ends
# of a long one) made it sit, jump, sit: a twitchy stutter. 6us a frame is
# ~27 deg/s.
MIN_SPEED_US_S = 300


class Axis:
    """One servo with velocity and acceleration limits.

    Each frame it picks the fastest speed from which it could still brake to a
    stop exactly at the target, capped at SPEED_US_S, and changes its actual
    speed toward that by at most ACCEL_US_S2. So it eases out of rest, cruises,
    and eases into the target. Position is kept as a float; the servo only
    gets whole microseconds.

    The braking speed is for whole-frame steps: braking from v by a per frame
    covers v + (v-a) + ... = v*v/(2a) + v/2, so v = (sqrt(a*a + 8*a*d) - a)/2.
    The continuous-time sqrt(2*a*d) overshoots and then hunts.

    It never goes slower than vmin per frame while moving (see
    MIN_SPEED_US_S), and lands once the target is within a step.
    """

    def __init__(self, servo):
        self.s = servo
        self.pos = float(servo.us)
        self.vel = 0.0  # us per frame

    def stop(self):
        self.pos = float(self.s.us)
        self.vel = 0.0

    def deg(self):
        return self.s.us_to_deg(self.pos)

    def tick(self, target_deg, vmax, amax, vmin):
        d = self.s.clamp(self.s.deg_to_us(target_deg)) - self.pos
        near = max(amax, vmin)
        if abs(d) <= near and abs(self.vel) <= near:
            if self.vel or d:
                self.pos, self.vel = float(self.s.clamp(self.pos + d)), 0.0
                self.s.write_us(self.pos)
            return
        want = min(vmax, ((amax * amax + 8 * amax * abs(d)) ** 0.5 - amax) / 2)
        want = want if d > 0 else -want
        self.vel += max(-amax, min(amax, want - self.vel))
        if abs(self.vel) < vmin:
            self.vel = vmin if d > 0 else -vmin
        self.pos += self.vel
        self.s.write_us(self.pos)


def _limits(s):
    a, b = s.us_to_deg(s.min_us), s.us_to_deg(s.max_us)
    return min(a, b), max(a, b)


def _open_eyes():
    if eyepanels is None:
        return None
    try:
        p = eyepanels.Panels()
        p.init()
        return p
    except Exception as e:      # the gimbal carries on without them
        print("ERR eyes: %r" % e)
        return None


FIRST_GAP_MS = 200  # between the two servos' first pulses: their bursts don't land together


def run(start=None, hold=False):
    """start: (pan, tilt) in degrees, where the head already is (the brain
    remembers it), so the first pulse doesn't move it; None: centre. hold:
    no servo pulses at all till G (targets set meanwhile are kept)."""
    g = gimbal.Gimbal()
    pan_lim, tilt_lim = _limits(g.pan), _limits(g.tilt)
    start = start or (0.0, 0.0)
    tp, tt = start
    axes = None         # (Axis, Axis) once the servos have had their first pulse

    def wake():
        # The first pulse is a snap: there's no position feedback, so a
        # servo goes at full speed to wherever it says. From where the head
        # already is, it hardly moves; and one servo at a time, so a wrong
        # guess draws one servo's burst, not two at once. Then it eases on
        # to the target.
        nonlocal axes
        g.pan.write_us(g.pan.deg_to_us(start[0]))
        time.sleep_ms(FIRST_GAP_MS)
        g.tilt.write_us(g.tilt.deg_to_us(start[1]))
        axes = (Axis(g.pan), Axis(g.tilt))

    if not hold:
        wake()
    dt = gimbal.FRAME_MS / 1000
    vmax = SPEED_US_S * dt          # us per frame
    amax = ACCEL_US_S2 * dt * dt    # us per frame, per frame
    vmin = MIN_SPEED_US_S * dt      # us per frame
    eyes = _open_eyes()
    scratch = memoryview(bytearray(CHUNK))     # where a frame goes with no panels
    next_tick = time.ticks_ms()

    def tick():
        # One PWM frame of servo motion, if one is due.
        nonlocal next_tick
        now = time.ticks_ms()
        if time.ticks_diff(now, next_tick) < 0:
            return
        next_tick = time.ticks_add(next_tick, gimbal.FRAME_MS)
        if time.ticks_diff(now, next_tick) > 0:     # fell behind: don't catch up in a rush
            next_tick = time.ticks_add(now, gimbal.FRAME_MS)
        if not axes:        # (held: no pulses yet)
            return
        for a, deg in ((axes[0], tp), (axes[1], tt)):
            a.tick(deg, vmax, amax, vmin)

    def read_into(stdin, mv, n):
        # n bytes into mv (or thrown away, mv None), keeping the servos going,
        # also while waiting for the Pi to send more (a busy Pi pauses mid-frame).
        got = 0
        while got < n:
            k = min(CHUNK, n - got)
            while not poll.poll(0):
                tick()
                time.sleep_us(200)
            stdin.readinto(mv[got:got + k] if mv else scratch[:k])
            got += k
            tick()

    def frame(stdin, rle):
        eye, x0, y0, x1, y1 = stdin.read(5)
        m = 0
        if rle:
            hi, lo = stdin.read(2)
            m = hi << 8 | lo
        if eye > 1 or x0 > x1 or y0 > y1 or x1 >= 240 or y1 >= 240 or m > RLE_MAX:
            print("ERR bad frame header")
            return
        n = (x1 - x0 + 1) * (y1 - y0 + 1) * 2
        if not rle:
            read_into(stdin, eyes.mv if eyes else None, n)
        else:
            read_into(stdin, eyes.rmv if eyes else None, m)
            if eyes and eyepanels.unrle(eyes.rle, m, eyes.buf, n) != n:
                print("ERR bad runs")
                return
        if eyes:
            eyes.draw(eye, x0, y0, x1, y1, n, tick)

    stdin = sys.stdin.buffer
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    micropython.kbd_intr(-1)
    print("READY")
    print("POS %+.0f %+.0f" % (tp, tt))
    try:
        while True:
            dirty = False
            while poll.poll(0):
                ch = stdin.read(1)
                if ch in (b"q", b"\x03"):
                    return
                if ch == b"L":
                    tp -= STEP_DEG
                elif ch == b"R":
                    tp += STEP_DEG
                elif ch == b"U":
                    tt += STEP_DEG
                elif ch == b"D":
                    tt -= STEP_DEG
                elif ch == b"A":
                    line = b""
                    while not line.endswith(b"\n") and len(line) < 32:
                        line += stdin.read(1)
                    try:
                        p, t = line.split(b",")
                        tp, tt = float(p), float(t)
                    except ValueError:
                        print("ERR bad A command")
                        continue
                    tp = max(pan_lim[0], min(pan_lim[1], tp))
                    tt = max(tilt_lim[0], min(tilt_lim[1], tt))
                    continue
                elif ch == b"P":
                    at = (axes[0].deg(), axes[1].deg()) if axes else start
                    print("AT %+.1f %+.1f" % at)
                    continue
                elif ch == b"G":
                    if not axes:
                        wake()
                    continue
                elif ch in (b"E", b"Z"):
                    frame(stdin, ch == b"Z")
                    continue
                elif ch == b"I":
                    if eyes:
                        eyes.init()
                    else:
                        eyes = _open_eyes()
                    continue
                elif ch == b"B":
                    on = stdin.read(1) == b"1"
                    if eyes:
                        eyes.backlight(on)
                    continue
                elif ch == b"c":
                    tp = tt = 0.0
                elif ch == b"x":
                    # Stop where we are, so nothing re-attaches to finish a glide.
                    if axes:
                        for a in axes:
                            a.stop()
                        tp, tt = axes[0].deg(), axes[1].deg()
                    g.detach()
                else:
                    continue
                tp = max(pan_lim[0], min(pan_lim[1], tp))
                tt = max(tilt_lim[0], min(tilt_lim[1], tt))
                dirty = True
            if dirty:
                print("POS %+.0f %+.0f" % (tp, tt))
            tick()
            time.sleep_ms(1)
    finally:
        micropython.kbd_intr(3)
        g.detach()
        if eyes:
            eyes.backlight(False)
        print("BYE")
