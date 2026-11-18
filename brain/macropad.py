"""The 12-key, 3-knob macropad (USB 1189:8842, "USB Composite Device"): her
controls by hand. For the brain (server.py), which says what each does.

    pad = macropad.Pad(on_key, on_turn)   # on_key(n, pressed), n 1..12 or "k1".."k3" (the knobs' presses)
    pad.start()                           # on_turn(knob 1..3, clicks: + clockwise); both from its thread

It's read straight from its two input devices, and grabbed, so nothing else
on the Pi sees its keys. Unplugged, it's looked for again every RETRY_S.

As it comes (no reprogramming needed: each control sends something of its own):
keys F12, Up, Homepage, Left, Down, Right, Meta+C, Meta+V, Meta+Z, PageUp,
Home, PageDown; knob 1 volume up (clockwise) and down, pressed mute; knob 2
brightness up (anticlockwise) and down, pressed stop; knob 3 the scroll wheel
(-1 clockwise), pressed keypad Enter.
"""
import os
import select
import sys
import threading
import time

VENDOR, PRODUCT = 0x1189, 0x8842
RETRY_S = 5

KEYS = {"KEY_F12": 1, "KEY_UP": 2, "KEY_HOMEPAGE": 3, "KEY_LEFT": 4, "KEY_DOWN": 5, "KEY_RIGHT": 6,
        "KEY_C": 7, "KEY_V": 8, "KEY_Z": 9, "KEY_PAGEUP": 10, "KEY_HOME": 11, "KEY_PAGEDOWN": 12,
        "KEY_MUTE": "k1", "KEY_STOPCD": "k2", "KEY_KPENTER": "k3"}
TURNS = {"KEY_VOLUMEUP": (1, 1), "KEY_VOLUMEDOWN": (1, -1),
         "KEY_BRIGHTNESSUP": (2, -1), "KEY_BRIGHTNESSDOWN": (2, 1)}


class Pad:
    def __init__(self, on_key, on_turn):
        self.on_key, self.on_turn = on_key, on_turn
        self.ok = False

    def start(self):
        threading.Thread(target=self._run, name="macropad", daemon=True).start()

    def _find(self, evdev):
        return [d for d in (evdev.InputDevice(p) for p in evdev.list_devices())
                if d.info.vendor == VENDOR and d.info.product == PRODUCT]

    def _run(self):
        try:
            import evdev
        except ImportError:
            print("macropad: off (python3-evdev not installed)", file=sys.stderr)
            return
        told = False
        while True:
            devs = self._find(evdev)
            if not devs:
                if not told:
                    print("macropad: not plugged in", file=sys.stderr)
                    told = True
                time.sleep(RETRY_S)
                continue
            told = False
            try:
                for d in devs:
                    d.grab()
                self.ok = True
                print("macropad: online (%s)" % ", ".join(os.path.basename(d.path) for d in devs), file=sys.stderr)
                self._read(evdev, devs)
            except OSError as e:
                print("macropad: lost (%s)" % e, file=sys.stderr)
            finally:
                self.ok = False
                for d in devs:
                    try:
                        d.close()
                    except OSError:
                        pass
            time.sleep(1)

    def _read(self, evdev, devs):
        ec = evdev.ecodes
        while True:
            r, _, _ = select.select(devs, [], [], 5)
            for d in r:
                for e in d.read():
                    if e.type == ec.EV_REL and e.code == ec.REL_WHEEL:
                        self.on_turn(3, -e.value)
                    elif e.type == ec.EV_KEY and e.value in (0, 1):
                        names = ec.KEY.get(e.code)          # (some codes have two names: KEY_MUTE)
                        names = names if isinstance(names, list) else [names]
                        name = next((n for n in names if n in KEYS or n in TURNS), None)
                        if name in TURNS and e.value == 1:
                            self.on_turn(*TURNS[name])
                        elif name in KEYS:
                            self.on_key(KEYS[name], e.value == 1)
