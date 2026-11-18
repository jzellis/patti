#!/usr/bin/env python3
"""Shut the Pi down when the Sense HAT joystick is pushed in and held.

Hold the middle button for HOLD_S seconds: the LED matrix turns red and the
Pi runs `shutdown -h now`. A shorter press does nothing. Runs as root from
sensehat-shutdown.service.

Reads the joystick's input device directly (no evdev package needed); it
doesn't grab it, so the sense_hat library can still read the stick too.
"""
import glob
import os
import select
import struct
import subprocess
import sys
import time

HOLD_S = 10.0                   # long: it's the emergency way; the chest screen's settings have shut down
BOOT_GRACE_S = 60               # ignore the stick this soon after boot, so a
                                # wiring slip that reads as "held" can't boot-loop her
NAME = "Sense HAT Joystick"     # "Raspberry Pi Sense HAT Joystick"
EV_KEY = 1
KEY_ENTER = 28                  # the middle press
EVENT = struct.Struct("llHHi")  # struct input_event: time, type, code, value


def find_stick():
    for path in glob.glob("/sys/class/input/event*/device/name"):
        with open(path) as f:
            if NAME in f.read():
                return "/dev/input/" + path.split("/")[4]
    return None


def uptime():
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def red():
    try:
        from sense_hat import SenseHat
        SenseHat().clear(255, 0, 0)
    except Exception as e:      # no LEDs is no reason not to shut down
        print("couldn't light the matrix: %s" % e, file=sys.stderr)


def watch(dev):
    fd = os.open(dev, os.O_RDONLY)
    print("watching %s: hold the joystick in for %g s to shut down" % (dev, HOLD_S))
    pressed_at = None
    try:
        while True:
            wait = None if pressed_at is None else max(0.0, pressed_at + HOLD_S - time.monotonic())
            if select.select([fd], [], [], wait)[0]:
                data = os.read(fd, EVENT.size * 64)
                for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, typ, code, value = EVENT.unpack_from(data, i)
                    if typ == EV_KEY and code == KEY_ENTER:
                        if value == 1:
                            pressed_at = time.monotonic()
                        elif value == 0:
                            pressed_at = None
            elif pressed_at is not None:
                if uptime() < BOOT_GRACE_S:
                    print("joystick held, but only %.0f s after boot: ignored" % uptime())
                    pressed_at = None
                    continue
                print("joystick held: shutting down")
                red()
                subprocess.run(["shutdown", "-h", "now"])
                return
    finally:
        os.close(fd)


while True:
    dev = find_stick()
    if dev:
        try:
            watch(dev)
            break
        except OSError as e:
            print("lost the joystick: %s" % e, file=sys.stderr)
    time.sleep(5)               # not there yet (early boot) or unplugged
