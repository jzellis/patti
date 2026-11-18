"""Read every Sense HAT sensor for `facectl sense`.

Needs the `sense_hat` package (python3-sense-hat on Raspberry Pi OS), so it
only works on the Pi with the HAT fitted. Imported lazily by facectl so the
rest of the tool still runs on a laptop.

The HAT sits right above the Pi 4's SoC and heat-soaks to nearly CPU
temperature, so its raw temperature reads ~18C high once the Pi has been up
a while (and only a few degrees high just after boot). We estimate room
temperature from how far the CPU is above the sensor -- see room_temp().
Relative humidity is then corrected to that temperature, since the HAT
measures the RH of the warm air around itself.
"""

import math
import os
import time

from . import sysinfo

# room = sensor - (cpu - sensor) / CPU_FACTOR. Fitted 2026-10-11 from ONE
# point: sensor 36.5C, CPU 52.2C, room 17.9C (household thermostat), Pi idle
# and heat-soaked after an hour. Refit it with readings at other CPU loads;
# override without editing via ROBOTFACE_TEMP_FACTOR.
CPU_FACTOR = float(os.environ.get("ROBOTFACE_TEMP_FACTOR", "0.84"))


def _settle(sense, seconds=1.0):
    """Let the IMU fusion converge (the first orientation reads are junk),
    sampling the HAT and CPU temperatures meanwhile.

    Returns (mean sensor C, mean CPU C). Averaging matters: with
    CPU_FACTOR < 1, room_temp() amplifies any CPU wobble by 1/CPU_FACTOR.
    """
    sense.set_imu_config(True, True, True)
    temps, cpus = [], []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        sense.get_orientation_degrees()
        temps.append(sense.get_temperature_from_humidity())
        cpu = sysinfo.cpu_temp_c()
        if cpu is not None:
            cpus.append(cpu)
        time.sleep(0.02)
    mean = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    return mean(temps), mean(cpus)


def _pressure(sense, tries=20):
    """The LPS25H returns 0 until its first conversion completes."""
    for _ in range(tries):
        p = sense.get_pressure()
        if p:
            return p
        time.sleep(0.05)
    return None


def room_temp(sensor_c, cpu_c, factor=CPU_FACTOR):
    """Estimate ambient temperature from the HAT and CPU temperatures."""
    if sensor_c is None or cpu_c is None:
        return None
    return sensor_c - (cpu_c - sensor_c) / factor


def _svp(t_c):
    """Saturation vapour pressure in hPa (Magnus formula)."""
    return 6.112 * math.exp(17.62 * t_c / (243.12 + t_c))


def room_humidity(rh, sensor_c, room_c):
    """Re-express RH measured at sensor_c as RH at room_c (same moisture)."""
    if rh is None or sensor_c is None or room_c is None:
        return None
    return min(100.0, rh * _svp(sensor_c) / _svp(room_c))


def _signed(deg):
    """0..360 -> -180..180, so a level robot reads ~0 rather than ~359."""
    return (deg + 180) % 360 - 180


def read(sense=None):
    """Return a dict of every reading. Values that fail come back as None."""
    if sense is None:
        from sense_hat import SenseHat
        sense = SenseHat()
    temp_h, cpu = _settle(sense)
    o = sense.get_orientation_degrees()
    humidity = sense.get_humidity()
    room = room_temp(temp_h, cpu)
    return {
        "temp_h": temp_h,
        "temp_p": sense.get_temperature_from_pressure() if _pressure(sense) else None,
        "cpu": cpu,
        "room": room,
        "humidity": humidity,
        "room_humidity": room_humidity(humidity, temp_h, room),
        "pressure": _pressure(sense),
        "pitch": _signed(o["pitch"]),
        "roll": _signed(o["roll"]),
        "yaw": o["yaw"],
        "heading": sense.get_compass(),
        "accel": sense.get_accelerometer_raw(),
        "gyro": sense.get_gyroscope_raw(),
        "mag": sense.get_compass_raw(),
    }


def _f(v, fmt):
    return "--" if v is None else fmt % v


def _vec(v, fmt):
    return "--" if v is None else " ".join(fmt % v[k] for k in ("x", "y", "z"))


def gather(sense=None):
    """(title, pairs, wide_rows) for render.sense_image.

    pairs are short (label, value) cells laid out two to a line; wide_rows
    are the x/y/z vectors, which need a whole line each.
    """
    r = read(sense)
    deg = "°"
    pairs = [
        ("Temp", _f(r["room"], "%.1f" + deg + "C")),
        ("Humid", _f(r["room_humidity"], "%.0f%%")),
        ("Press", _f(r["pressure"], "%.1f hPa")),
        ("Head", _f(r["heading"], "%.0f" + deg)),
        ("Pitch", _f(r["pitch"], "%.1f" + deg)),
        ("Roll", _f(r["roll"], "%.1f" + deg)),
    ]
    wide = [
        ("Accel g", _vec(r["accel"], "%+.2f")),
        ("Gyro r/s", _vec(r["gyro"], "%+.2f")),
        ("Mag uT", _vec(r["mag"], "%+.0f")),
    ]
    return "SENSE HAT  " + time.strftime("%H:%M:%S"), pairs, wide
