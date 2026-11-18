"""Pan/tilt gimbal driver: two MG996Rs on a Kitronik Simply Servos board.

Uses hardware PWM (machine.PWM) rather than Kitronik's SimplyServos.py. Their
module claims PIO state machines 0-7 and drives every one of the 8 headers to
1500us the moment you construct it, and on a Pico W the CYW43 radio wants PIO
too (their README calls out the resulting "StateMachine claimed" error). We
only need two channels, and the RP2350's PWM slices do 50Hz with sub-microsecond
resolution, so plain PWM is simpler and leaves the PIO alone.

Servo channels are GP2-GP9. Servos are named by GPIO, not header number: the
datasheet says header 1 = GP2, but on our board the header labelled 1 is GP3
and 2 is GP2 (verified by driving each pin). GP2/GP3 share PWM
slice 1, which is fine: both run at the same 50Hz.

Everything below is in microseconds of pulse width, the one unit the servo
actually understands. Angles are a convenience on top, and only as good as
US_PER_DEG, which is a guess until calibrated (see README).
"""

import time

from machine import PWM, Pin

# GPIOs wired to the Simply Servos servo headers
SERVO_GPIOS = (2, 3, 4, 5, 6, 7, 8, 9)

PAN_GPIO = 2   # board header labelled 2
TILT_GPIO = 3  # board header labelled 1

FRAME_HZ = 50
FRAME_MS = 1000 // FRAME_HZ

# MG996R datasheet: 1000-2000us, 1500 centre. Most real units travel further
# (roughly 500-2500us for ~180 deg), but past their end stops they stall and
# pull ~2.5A each, so we only open the range up after measuring it.
SAFE_MIN_US = 1000
SAFE_MAX_US = 2000
CENTER_US = 1500

# ~2000us / 180deg. Checked on both of ours: +/-1000us from centre is ~90 deg.
US_PER_DEG = 2000 / 180

# Default glide speed: ~500us/s is ~45 deg/s.
DEFAULT_SPEED_US_S = 500

# Calibrated limits for our gimbal, by stepping each axis and watching. Angle
# convention: +pan = camera's right, +tilt = up.
#
# Pan (2026-10-08): full 500-2500us (+/-90 deg), nothing in the way; mounted
# so a *lower* pulse is +, hence invert=True.
# Tilt (2026-10-31, after the head went on and the gimbal was rebuilt level):
# now a *higher* pulse is up, so invert=False. Down, the tilt servo hits the
# gimbal at -23 deg; up, the head nearly touches at +45. The limits leave
# 4-5 deg of margin: -19 to +40.
PAN_LIMITS = dict(min_us=500, max_us=2500, center_us=1500, invert=True)
TILT_LIMITS = dict(min_us=1289, max_us=1944, center_us=1500, invert=False)


class Servo:
    def __init__(self, gpio, min_us=SAFE_MIN_US, max_us=SAFE_MAX_US,
                 center_us=CENTER_US, invert=False):
        if gpio not in SERVO_GPIOS:
            raise ValueError("gpio must be one of %r" % (SERVO_GPIOS,))
        if not (min_us <= center_us <= max_us):
            raise ValueError("need min_us <= center_us <= max_us")
        self.gpio = gpio
        self.min_us = min_us
        self.max_us = max_us
        self.center_us = center_us
        self.invert = invert
        self._pwm = None
        self._us = None  # last commanded pulse; None = never driven / unknown

    @property
    def attached(self):
        return self._pwm is not None

    @property
    def us(self):
        return self._us

    def clamp(self, us):
        return max(self.min_us, min(self.max_us, int(round(us))))

    def write_us(self, us):
        """Jump straight to a pulse width (clamped). Attaches if needed."""
        us = self.clamp(us)
        if self._pwm is None:
            self._pwm = PWM(Pin(self.gpio), freq=FRAME_HZ, duty_ns=us * 1000)
        else:
            self._pwm.duty_ns(us * 1000)
        self._us = us
        return us

    def detach(self):
        """Stop sending pulses.

        Must not cut a pulse short: MG996Rs are commonly digital servos that
        latch the last pulse they saw and keep driving to it, so a runt pulse
        sends them hard to an end and holds them there. Setting duty to 0 is
        double-buffered (takes effect at the next wrap), so the in-flight
        pulse finishes; one frame later the line is idle low and we hand the
        pin back to plain GPIO. We deliberately never deinit() the PWM: GP2
        and GP3 share a slice, and stopping it freezes the *other* servo's
        line wherever it happens to be, possibly mid-pulse.

        Whether the servo then goes limp or holds its last position depends
        on the servo; a digital one may keep holding."""
        if self._pwm is not None:
            self._pwm.duty_ns(0)
            time.sleep_ms(FRAME_MS + 5)
            Pin(self.gpio, Pin.OUT, value=0)
            self._pwm = None

    # -- angles, relative to centre. Positive = increasing pulse unless inverted.

    def deg_to_us(self, deg):
        if self.invert:
            deg = -deg
        return self.center_us + deg * US_PER_DEG

    def us_to_deg(self, us):
        deg = (us - self.center_us) / US_PER_DEG
        return -deg if self.invert else deg


class Gimbal:
    def __init__(self, pan=None, tilt=None):
        self.pan = pan or Servo(PAN_GPIO, **PAN_LIMITS)
        self.tilt = tilt or Servo(TILT_GPIO, **TILT_LIMITS)

    def center(self, speed=DEFAULT_SPEED_US_S):
        self.glide_us(self.pan.center_us, self.tilt.center_us, speed)

    def look(self, pan_deg, tilt_deg, speed=DEFAULT_SPEED_US_S):
        self.glide_us(self.pan.deg_to_us(pan_deg),
                      self.tilt.deg_to_us(tilt_deg), speed)

    def glide_us(self, pan_us, tilt_us, speed=DEFAULT_SPEED_US_S):
        """Move both axes together at `speed` us/s, one PWM frame per step.

        Blocking. A servo we have never driven has no known position: there is
        no feedback wire, so its first pulse snaps it at full speed to wherever
        that pulse says. Call center() once at start-up and accept that one
        uncontrolled move, ideally with the gimbal already near centre.
        """
        targets = [(self.pan, self.pan.clamp(pan_us)),
                   (self.tilt, self.tilt.clamp(tilt_us))]
        for s, t in targets:
            if s.us is None:
                s.write_us(t)
        step = max(1, speed * FRAME_MS // 1000)
        while True:
            moving = False
            for s, t in targets:
                d = t - s.us
                if d:
                    s.write_us(s.us + max(-step, min(step, d)))
                    moving = True
            if not moving:
                return
            time.sleep_ms(FRAME_MS)

    def detach(self):
        self.pan.detach()
        self.tilt.detach()

    def status(self):
        return {
            "pan_us": self.pan.us, "tilt_us": self.tilt.us,
            "pan_attached": self.pan.attached, "tilt_attached": self.tilt.attached,
        }
