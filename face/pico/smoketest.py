"""Bring-up test. Run this BEFORE deploying main.py.

Its whole job is to separate "the panel and driver revision are right" from
"my code is right". If this draws a clean face, the hardware, the wiring,
the MicroPython build and the driver revision are all good, and anything
that breaks afterwards is ours.

    mpremote connect /dev/ttyACM0 run pico/smoketest.py

Note this does several refreshes back to back, which is below the 5s floor
the firmware normally enforces. That is what the vendor demos do too and is
fine as a one-off -- just don't sit here running it in a loop.
"""

import time

import draw
import protocol
from lib.epd2in13_v4 import EPD_2in13_V4_Landscape


def main():
    print("init panel...")
    epd = EPD_2in13_V4_Landscape()

    print("clear to white")
    epd.Clear()

    print("draw 'smile'")
    draw.expression(epd, "smile")
    epd.display(epd.buffer)
    time.sleep(3)

    print("draw text")
    draw.text_block(epd, "PATTI ONLINE", 2)
    epd.display(epd.buffer)
    time.sleep(3)

    print("sleep panel")
    epd.sleep()
    print("smoketest done -- if you saw a face and then text, we are good")


main()
