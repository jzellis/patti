"""Drive DC motors through a Waveshare Pico-Motor-Driver (SKU 19764).

The board is a PCA9685 (I2C PWM chip) feeding two TB6612FNG dual H-bridges,
four motors in all. The Pico never touches the motors directly: it tells the
PCA9685, over I2C, what to put on each H-bridge input. Per Waveshare's demo:

    port    PWM  IN1  IN2      (PCA9685 channels; CHANNELS below says
                                which wheel is on which port)
      A      0    1    2
      B      3    4    5
      C      6    7    8
      D      9   10   11

    I2C0, SDA = GP20, SCL = GP21, address 0x40 (5 address jumpers on board)

Board pins NOT to reuse for anything else (from the schematic): GP20/GP21
(I2C), GP6/GP7 (alternate I2C routing), GP14 (power-enable circuit).

Safety: the PCA9685's OE pin is hard-wired to GND, so there is no hardware
"all off". The chip also latches its outputs -- if the Pico crashes or hangs
mid-move, the motor keeps running. So:
  * Motors() stops everything the moment it's constructed (call it first
    thing at boot),
  * anything long-running must wrap motion in try/finally: stop_all(),
  * the command firmware adds a watchdog and a dead-man timeout.

Speeds are -1.0 .. +1.0. MAX_DUTY caps the top end because the FIT0450 TT
motors are rated 6V (7.5V max) and the board passes VIN straight through to
them. VIN is a buck converter set to 7.5V (fed from a 9V USB-PD trigger).
"""

import time

from machine import I2C, Pin

ADDR = 0x40
PWM_HZ = 1000          # quiet-ish and smooth; the demo's 50Hz makes motors judder

# Fraction of VIN the motors ever see. VIN is a regulated 7.5V, so
# 0.8 -> 6.0V, the motors' rated voltage. Up to ~0.93 (7V) is safe if you
# want more speed; 7.5V is their absolute max. Re-check this if VIN changes.
MAX_DUTY = 0.8

# Duty below which a stopped motor just hums: the gearbox friction wins. Any
# non-zero speed starts here and scales up to MAX_DUTY, so small speeds still
# move. Set a little above the stickiest motor: in the 2026-10-15 step test, B
# (back right) needed 0.2 of the old range (0.16 duty), the rest 0.1 or less.
MIN_DUTY = 0.18

# Letters name wheels, not board ports. Since the 2026-10-20 rebuild the back
# wheels are on each other's ports: back left (A) is on the board's motor B
# terminals, back right (B) on motor A's.
CHANNELS = {"A": (3, 4, 5), "B": (0, 1, 2), "C": (6, 7, 8), "D": (9, 10, 11)}

# Motors whose leads are wired the other way round, so "forward" is reversed
# in software. Since the 2026-10-20 rebuild that's the two front wheels.
INVERT = "CD"

_MODE1 = 0x00
_MODE2 = 0x01
_LED0 = 0x06
_ALL_LED = 0xFA
_PRESCALE = 0xFE
_SLEEP = 0x10
_AI = 0x20             # register auto-increment, so one write sets a channel
_RESTART = 0x80
_FULL = 0x1000         # bit 4 of ON_H / OFF_H: channel fully on / fully off


class PCA9685:
    def __init__(self, i2c, addr=ADDR, freq=PWM_HZ):
        self.i2c = i2c
        self.addr = addr
        # Auto-increment first: every multi-byte write below depends on it,
        # and it's off at power-up. Stay asleep (outputs idle) meanwhile.
        self._w(_MODE1, _AI | _SLEEP)
        self.all_off()
        self._w(_MODE2, 0x04)               # totem-pole outputs (the default)
        self.set_freq(freq)                 # wakes the chip

    def _w(self, reg, val):
        self.i2c.writeto_mem(self.addr, reg, bytes([val]))

    def _r(self, reg):
        return self.i2c.readfrom_mem(self.addr, reg, 1)[0]

    def set_freq(self, freq):
        prescale = round(25_000_000 / (4096 * freq)) - 1
        prescale = max(3, min(255, prescale))
        old = self._r(_MODE1) & ~_RESTART
        self._w(_MODE1, (old & 0x7F) | _SLEEP)   # prescale only writable asleep
        self._w(_PRESCALE, prescale)
        self._w(_MODE1, old & ~_SLEEP | _AI)
        time.sleep_us(600)                       # oscillator settle
        self._w(_MODE1, (old & ~_SLEEP) | _AI | _RESTART)

    def _set(self, base, on, off):
        self.i2c.writeto_mem(self.addr, base,
                             bytes([on & 0xFF, on >> 8, off & 0xFF, off >> 8]))

    def duty(self, ch, frac):
        """0.0 .. 1.0. Exact 0 and 1 use the chip's full-off/full-on bits,
        not 0/4095 counts, so "off" really is off."""
        if frac <= 0:
            self._set(_LED0 + 4 * ch, 0, _FULL)
        elif frac >= 1:
            self._set(_LED0 + 4 * ch, _FULL, 0)
        else:
            self._set(_LED0 + 4 * ch, 0, int(frac * 4095))

    def level(self, ch, high):
        self.duty(ch, 1 if high else 0)

    def all_off(self):
        self._set(_ALL_LED, 0, _FULL)


class Motor:
    def __init__(self, pca, name, invert=False):
        self.pca = pca
        self.name = name
        self.pwm, self.in1, self.in2 = CHANNELS[name]
        self.invert = invert
        self.speed = 0.0
        self._fwd = None    # direction the IN pins are set for; None = not driving

    def run(self, speed):
        """-1.0 (full reverse) .. +1.0 (full forward). Non-zero speeds map onto
        MIN_DUTY .. MAX_DUTY."""
        speed = max(-1.0, min(1.0, speed))
        self.speed = speed
        if self.invert:
            speed = -speed
        if speed == 0:
            self.coast()
            return
        fwd = speed > 0
        if fwd != self._fwd:
            # Direction pins first, with PWM at 0, so we never briefly drive
            # the old direction at the new speed. Skipped when only the speed
            # changes, so a control loop updating 50x a second doesn't blip.
            self.pca.duty(self.pwm, 0)
            self.pca.level(self.in1, fwd)
            self.pca.level(self.in2, not fwd)
            self._fwd = fwd
        self.pca.duty(self.pwm, MIN_DUTY + abs(speed) * (MAX_DUTY - MIN_DUTY))

    def coast(self):
        """Both inputs low: H-bridge off, motor spins down freely."""
        self.pca.duty(self.pwm, 0)
        self.pca.level(self.in1, 0)
        self.pca.level(self.in2, 0)
        self.speed = 0.0
        self._fwd = None

    def brake(self):
        """Both inputs high: motor terminals shorted, stops quickly."""
        self.pca.level(self.in1, 1)
        self.pca.level(self.in2, 1)
        self.pca.duty(self.pwm, 1)
        self.speed = 0.0
        self._fwd = None


class Motors:
    """All four channels. Constructing this stops every motor."""

    def __init__(self, invert=INVERT):
        i2c = I2C(0, sda=Pin(20), scl=Pin(21), freq=400_000)
        if ADDR not in i2c.scan():
            raise OSError("PCA9685 not found at 0x%02X -- is the Pico seated "
                          "on the motor board, and the board powered?" % ADDR)
        self.pca = PCA9685(i2c)
        self.m = {n: Motor(self.pca, n, n in invert) for n in CHANNELS}
        self.stop_all()

    def __getitem__(self, name):
        return self.m[name]

    def stop_all(self):
        self.pca.all_off()
        for m in self.m.values():
            m.speed = 0.0
            m._fwd = None
