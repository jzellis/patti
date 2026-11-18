# Arm bench module for the Pico 2 W on the Kitronik Simply Servos board. Lives beside main.py
# (which is still the old motor board's boot file - it only touches GP20/21, harmless here).
# Import from the REPL:  import arm   - nothing is driven at import; no pulses until go().
#
# Servos (user, 2026-11-14): 1 base rotation, 2+3 shoulder (one each side, mirrored), 4 elbow, 5 wrist.
# Kitronik: servo header 1-8 = GP2-GP9.
#
#   arm.go(n, deg)     move servo n slowly to deg (clamped to LIMITS[n])
#   arm.shoulder(deg)  the shoulder: servo 2 alone (SHOULDER_PAIR False), or 2 and 3 mirrored (180 - deg + SH_TRIM)
#   arm.off(n) / arm.off()   stop pulses (limp)
#   arm.where()        last commanded angles
# A servo's FIRST pulse jumps at full speed from wherever it really is - always set the joint by hand
# near the first target before it, or support it.
import time
from machine import Pin, PWM

PINS = [2, 3, 4, 5, 6, 7, 8, 9]
MIN_US, MAX_US = 500, 2500       # 0..180 deg
SPEED = 15                        # deg/s - slow and low
# conservative limits until each joint is calibrated by eye (widen as we learn the real range)
LIMITS = {1: (0, 180), 2: (30, 170), 3: (10, 150), 4: (5, 175), 5: (1, 179)}
# wrist pitch 5 (belt MG996, 1:1): 90 = wrist flat in line with the forearm (belt re-indexed 2026-11-17); the sim clears
# +/-100, the tensioner clamp is hit at ~+/-120. 1..179 tested 2026-11-17: no strain, no skips at either end;
# lower numbers = wrist UP.
# shoulder: 2 = 180 - 3 (re-splined 2026-11-14, trim 0); 100 = straight up, 30 = ~70 deg forward (reaches its own
# base; ~10 more is possible); 170 = ~70 deg back (reached, but shudders a bit there - prefer <= 160 in use).
# elbow 4: horn re-fitted 2026-11-14 with 90 = forearm ~30-35 deg above horizontal (fwd); higher = down/forward;
# 5 = forearm in line with the upper arm (photo; the joint could go ~30-45 further back, servo can't);
# 175 = forward end tested OK (no hard stop hit). Range 5..175.
# base 1 (new wheel base, servo gear re-fitted 2026-11-15): 90 = arm straight away from the clamp tab; lower = left
# (counterclockwise from above), higher = right; 0 and 180 quiet, each about 80-85 deg from straight ahead.
# NEVER mpremote cp while servos hold: it soft-resets the Pico and every joint goes limp.
SH_TRIM = 0                       # servo 3 = 180 - servo 2 + SH_TRIM
# 2026-11-17: the buzz isn't the pair fighting: 2 alone AND 3 alone both buzz at shoulder 60 and go quiet when the
# arm's weight is lifted = load hunting (fix: a spring counterbalance). So the pair stays (half the load each).
# False = servo 2 alone, 3 never driven (unplug it).
SHOULDER_PAIR = True

pwm = [None] * 9
pos = [None] * 9

def _write(n, deg):
    if pwm[n] is None:
        pwm[n] = PWM(Pin(PINS[n - 1])); pwm[n].freq(50)
    pwm[n].duty_ns(int((MIN_US + (MAX_US - MIN_US) * deg / 180) * 1000))
    pos[n] = deg

def _clamp(n, deg):
    lo, hi = LIMITS.get(n, (0, 180))
    if deg < lo or deg > hi:
        print("servo", n, "limited to", lo, "-", hi)
    return max(lo, min(hi, deg))

def go(n, deg):
    deg = _clamp(n, deg)
    if pos[n] is None:
        _write(n, deg); return pos[n]
    d = pos[n]
    while abs(deg - d) >= 1:
        d += 1 if deg > d else -1
        _write(n, d); time.sleep(1 / SPEED)
    _write(n, deg)
    return pos[n]

def shoulder(deg):
    if not SHOULDER_PAIR:
        return go(2, deg)
    deg = _clamp(2, deg)
    m = lambda a: 180 - a + SH_TRIM
    if pos[2] is None or pos[3] is None:
        _write(2, deg); _write(3, m(deg)); return
    d = pos[2]
    while abs(deg - d) >= 1:
        d += 1 if deg > d else -1
        _write(2, d); _write(3, m(d)); time.sleep(1 / SPEED)
    _write(2, deg); _write(3, m(deg))

def off(n=None):
    # the signal is held LOW, not left floating: a floating line picks up the neighbours' pulses and the "limp" servo
    # twitches (2026-11-16: the released base kicked whenever other joints moved)
    for i in ([n] if n else range(1, 9)):
        if pwm[i] is not None:
            pwm[i].deinit(); pwm[i] = None
        Pin(PINS[i - 1], Pin.OUT, value=0)


for _p in PINS:                     # at import: every servo line low = quiet, until commanded
    Pin(_p, Pin.OUT, value=0)

def where():
    return {i: pos[i] for i in range(1, 9) if pos[i] is not None}
