#!/usr/bin/env python3
"""Fill each GC9A01 eye with a solid colour, to check the wiring.

    python3 eyetest.py                  left red, right blue, then both green
    python3 eyetest.py left white       one eye (left/right/both), one colour;
                                        an eye not named goes black
    python3 eyetest.py left red right cyan

Left and right are hers, as seen from behind her. Every run resets both eyes
(the reset line is shared). Wiring: README.md.
"""
import sys
import time

import gc9a01

COLOURS = {"red": 0xF800, "green": 0x07E0, "blue": 0x001F, "white": 0xFFFF,
           "black": 0x0000, "yellow": 0xFFE0, "cyan": 0x07FF, "magenta": 0xF81F}


def main():
    gpio, eyes = gc9a01.open_eyes()
    if len(sys.argv) > 2:
        want = {"left": "black", "right": "black"}
        args = sys.argv[1:]
        for which, colour in zip(args[::2], args[1::2]):
            for name in want:
                if which in (name, "both"):
                    want[name] = colour
        for name, colour in want.items():
            eyes[name].fill(COLOURS[colour])
            print("%s: %s" % (name, colour))
        return
    eyes["left"].fill(COLOURS["red"])
    eyes["right"].fill(COLOURS["blue"])
    print("left should be RED, right BLUE (5 s)")
    time.sleep(5)
    for e in eyes.values():
        e.fill(COLOURS["green"])
    print("both GREEN")


if __name__ == "__main__":
    main()
