#!/usr/bin/env python3
"""Storm watch: the Sense HAT's pressure sensor (an LPS25H, read straight over
I2C), logged every SAMPLE_S, and the way it's heading.

Pressure isn't fooled by the Pi's heat the way the HAT's thermometers are. A
fall of 3.6 hPa or more in three hours (the Met Office's "falling quickly")
usually means wind and rain are coming; watch() has her say so, once in a
while, when she's awake.

The sensor reads the pressure where she is; forecasts quote it at sea level.
calibrate() takes the difference from Open-Meteo's sea-level pressure for her
place (weather.py) every CALIBRATE_S, which also takes out the sensor's own
error (it's good to about 1 hPa).

    python3 barometer.py            # read it now, and what the log says
"""
import json
import os
import sys
import time

ADDR = 0x5C
BUS = 1
LOG = os.path.expanduser("~/.local/share/robotpattinson/pressure.json")
SAMPLE_S = 5 * 60
KEEP_S = 7 * 24 * 3600          # a week of samples (about 2000)
CALIBRATE_S = 6 * 3600
WARN_EVERY_S = 6 * 3600         # say it at most this often
# the Met Office's words for a change over 3 hours, in hPa
TENDENCY = ((0.1, "steady"), (1.6, "slowly"), (3.6, ""), (6.1, "quickly"), (1e9, "very rapidly"))


class Barometer:
    def __init__(self):
        self.bus = None
        self.error = ""
        self.offset = None              # sea level minus ours, hPa (from calibrate())
        self.calibrated = 0.0
        self.warned = 0.0
        self.samples = []               # [(time.time(), hPa here)]
        try:
            with open(LOG) as f:
                d = json.load(f)
            self.samples = [tuple(s) for s in d.get("samples", [])]
            self.offset = d.get("offset")
            self.calibrated = d.get("calibrated", 0.0)
            self.warned = d.get("warned", 0.0)
        except (OSError, ValueError):
            pass
        try:
            import smbus
            self.bus = smbus.SMBus(BUS)
            if self.bus.read_byte_data(ADDR, 0x0F) != 0xBD:       # WHO_AM_I
                raise OSError("no LPS25H at 0x%02x" % ADDR)
            self.bus.write_byte_data(ADDR, 0x20, 0x90)           # on, a reading a second
        except (ImportError, OSError) as e:
            self.bus, self.error = None, str(e)
            print("barometer: off (%s)" % e, file=sys.stderr)

    def read(self):
        """The pressure here, hPa (None if it can't)."""
        if not self.bus:
            return None
        try:
            d = self.bus.read_i2c_block_data(ADDR, 0x28 | 0x80, 3)
        except OSError as e:
            self.error = str(e)
            return None
        return (d[0] | d[1] << 8 | d[2] << 16) / 4096

    def sample(self):
        p = self.read()
        if p is None:
            return None
        now = time.time()
        self.samples.append((now, round(p, 2)))
        self.samples = [s for s in self.samples if now - s[0] < KEEP_S]
        self.save()
        return p

    def save(self):
        try:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG + ".tmp", "w") as f:
                json.dump({"offset": self.offset, "calibrated": self.calibrated, "warned": self.warned,
                           "samples": self.samples}, f)
            os.replace(LOG + ".tmp", LOG)
        except OSError as e:
            print("barometer: couldn't save (%s)" % e, file=sys.stderr)

    def calibrate(self):
        """Open-Meteo's sea-level pressure now, against ours. Quietly does
        nothing without a place or the internet."""
        import weather
        p = weather.place()
        here = self.read()
        if not p or here is None:
            return
        try:
            w = weather.get(weather.FORECAST, {"latitude": p["lat"], "longitude": p["lon"],
                                               "current": "pressure_msl"})
            msl = float(w["current"]["pressure_msl"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        off = msl - here
        # smooth it: one calibration shouldn't swing the readings about
        self.offset = off if self.offset is None else self.offset + (off - self.offset) * 0.3
        self.calibrated = time.time()
        self.save()
        print("barometer: sea level %.1f, ours %.1f, offset now %.1f hPa" % (msl, here, self.offset),
              file=sys.stderr)

    def at_sea_level(self, p):
        return p + (self.offset or 0.0)

    def now(self):
        """The latest sample at sea level (None if there isn't one)."""
        return self.at_sea_level(self.samples[-1][1]) if self.samples else None

    def change(self, hours=3.0):
        """hPa it's changed in the last `hours` (None until there's enough log)."""
        if len(self.samples) < 4:
            return None
        now = self.samples[-1][0]
        then = now - hours * 3600
        old = [p for t, p in self.samples if abs(t - then) < SAMPLE_S * 1.5]
        if not old:
            return None
        recent = [p for t, p in self.samples if now - t < SAMPLE_S * 2.5]   # a little smoothing
        return sum(recent) / len(recent) - sum(old) / len(old)

    def tendency(self):
        """("falling", "quickly", -4.1), ("steady", "", 0.0) or None."""
        c = self.change()
        if c is None:
            return None
        word = next(w for limit, w in TENDENCY if abs(c) < limit)
        if word == "steady":
            return ("steady", "", c)
        return ("rising" if c > 0 else "falling", word, c)

    def words(self):
        """"1012 hPa, falling quickly" for the screen, or None."""
        p = self.now()
        if p is None:
            return None
        t = self.tendency()
        return "%d hPa" % round(p) + (", %s" % " ".join(w for w in t[:2] if w) if t else "")

    def spoken(self):
        """What she says when asked, or None."""
        p = self.now()
        if p is None:
            return None
        t = self.tendency()
        s = "The pressure's %d hectopascals" % round(p)
        if not t:
            return s + ". I'll know which way it's heading in a few hours."
        if t[0] == "steady":
            return s + ", and steady."
        s += ", and %s %s" % (t[0], t[1]) if t[1] else ", and %s" % t[0]
        if t[0] == "falling" and abs(t[2]) >= 3.6:
            return s + ". Could be a storm coming."
        if t[1] == "slowly":
            return s + "."
        if t[0] == "falling":
            return s + ". Might turn wet."
        return s + ". Should be settling down."

    def warning(self):
        """A storm warning to give now (and not again for WARN_EVERY_S), or None."""
        t = self.tendency()
        if not t or t[0] != "falling" or abs(t[2]) < 3.6 or time.time() - self.warned < WARN_EVERY_S:
            return None
        self.warned = time.time()
        self.save()
        return "very" if abs(t[2]) >= 6.1 else "quick"


if __name__ == "__main__":
    b = Barometer()
    p = b.read()
    print("here: %s hPa" % (None if p is None else round(p, 2)), b.error)
    print("offset:", b.offset, " samples:", len(b.samples))
    print("words:", b.words())
    print("spoken:", b.spoken())
