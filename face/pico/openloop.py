"""Drive the panel with BUSY ignored, and watch for any sign of life.

Run when diagnose.py reports BUSY stuck high.

    mpremote connect /dev/ttyACM0 run pico/openloop.py

Why: "BUSY is driven high" and "BUSY is disconnected but the HAT has its own
pull-up resistor" look identical from the Pico. So stop inferring from that
line. Instead we run the real vendor init sequence with every BUSY wait
replaced by a generous fixed delay, push an unmistakable test pattern, and
wait longer than any refresh could take.

Then the answer comes from your eyes, not the pin:

  * Something appears on the glass  -> the panel works and BUSY is the only
    broken thing (wrong polarity for this revision, or a bad BUSY line).
  * Nothing at all appears          -> the panel is not receiving commands;
    it is a wiring, seating or power fault.

This does exactly one refresh, which is well within spec.
"""

import utime

import lib.epd2in13_v4 as drv
from lib.epd2in13_v4 import EPD_2in13_V4_Landscape

WAIT_MS = 2500          # longer than a full refresh (~2s), per vendor spec

transitions = []


class OpenLoop(EPD_2in13_V4_Landscape):
    """The vendor driver with its BUSY handshake replaced by a timed wait.

    We still sample BUSY throughout, because a pin that MOVES tells us the
    panel is alive even if we have its polarity backwards.
    """

    def ReadBusy(self):
        t0 = utime.ticks_ms()
        last = self.digital_read(self.busy_pin)
        transitions.append(("start", last, 0))
        while utime.ticks_diff(utime.ticks_ms(), t0) < WAIT_MS:
            v = self.digital_read(self.busy_pin)
            if v != last:
                transitions.append(
                    ("change", v, utime.ticks_diff(utime.ticks_ms(), t0)))
                last = v
            utime.sleep_ms(2)


def main():
    print("=== open-loop panel test (BUSY ignored) ===\n")
    print("init (this takes ~%ds, BUSY waits are fixed delays)..." % (WAIT_MS * 3 // 1000))

    epd = OpenLoop()

    print("drawing test pattern: four thick black bars + a solid block")
    epd.fill(1)                                  # white
    for i in range(4):                           # vertical bars
        epd.fill_rect(20 + i * 56, 10, 24, 100, 0)
    epd.fill_rect(0, 0, 250, 8, 0)               # solid bar along the top

    print("pushing frame and refreshing...")
    epd.display(epd.buffer)

    print("waiting 5s for the refresh to settle...")
    utime.sleep(5)

    epd.sleep()

    print("\n=== BUSY activity during the run ===")
    if not transitions:
        print("  (none recorded)")
    changes = [t for t in transitions if t[0] == "change"]
    for kind, value, ms in transitions:
        print("  %-7s value=%d  t+%dms" % (kind, value, ms))

    print("\n=== now look at the display ===")
    print("""If you see black bars: the panel and the driver revision are FINE.
The only broken thing is the BUSY handshake, and we can work around it.

If the glass is still blank: the panel is not receiving commands at all.
That is wiring, seating or power -- not a software problem.

BUSY transitions recorded: %d""" % len(changes))
    if changes:
        print("BUSY moved, so the panel IS responding to commands.")
    else:
        print("BUSY never moved, so it is either stuck or not connected.")


main()
