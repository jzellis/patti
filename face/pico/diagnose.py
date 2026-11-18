"""Panel diagnostics. Run this when the smoke test hangs.

    mpremote connect /dev/ttyACM0 run pico/diagnose.py

Nothing here can block: every wait has a timeout. It answers one question
first -- is the panel electrically present at all? -- because a hang in
ReadBusy() looks identical whether the panel is unplugged, unpowered, or
genuinely stuck, and the fix is completely different in each case.
"""

import time

from machine import Pin, SPI

RST_PIN = 12
DC_PIN = 8
CS_PIN = 9
BUSY_PIN = 13
SCK_PIN = 10
MOSI_PIN = 11


CONTROL_PINS = (2, 3, 22)       # brought out to the header, unused by the HAT


def _pull_read(gp):
    """Read a pin with the pull-up on, then the pull-down on."""
    up = Pin(gp, Pin.IN, Pin.PULL_UP)
    time.sleep_ms(30)
    v_up = up.value()
    down = Pin(gp, Pin.IN, Pin.PULL_DOWN)
    time.sleep_ms(30)
    v_down = down.value()
    return v_up, v_down


def probe_busy():
    """Is anything actually driving BUSY?

    The obvious test -- read against the pull-up, then the pull-down, and see
    whether the pin follows them -- is NOT trustworthy on RP2350. That part
    has an erratum where a floating input latches high even with the internal
    pull-down enabled, so an unconnected pin reads 1/1 exactly like a driven
    one. Reading BUSY alone on a Pico 2 produces a confident wrong answer.

    So we read known-floating control pins alongside it. If BUSY matches the
    controls, the test tells us nothing and we say so, rather than inventing
    a verdict.
    """
    controls = [_pull_read(gp) for gp in CONTROL_PINS]
    for gp, (vu, vd) in zip(CONTROL_PINS, controls):
        print("  GP%-2d (floating control) pull-up:%d pull-down:%d" % (gp, vu, vd))

    v_up, v_down = _pull_read(BUSY_PIN)
    print("  GP%-2d BUSY               pull-up:%d pull-down:%d" % (
        BUSY_PIN, v_up, v_down))

    pull_test_works = any(vu != vd for vu, vd in controls)
    if not pull_test_works:
        print("  !! floating controls do not follow their pulls on this chip,")
        print("     so the pull test cannot distinguish floating from driven.")
        return "indeterminate"

    if v_up == 1 and v_down == 0:
        return "floating"
    if v_up == 1 and v_down == 1:
        return "driven-high"
    if v_up == 0 and v_down == 0:
        return "driven-low"
    return "weird"


def wait_busy_low(timeout_ms=5000):
    pin = Pin(BUSY_PIN, Pin.IN, Pin.PULL_UP)
    start = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
        if pin.value() == 0:
            return time.ticks_diff(time.ticks_ms(), start)
        time.sleep_ms(10)
    return None


def main():
    print("=== Patti face: panel diagnostics ===\n")

    print("1. Is the BUSY line driven?")
    state = probe_busy()
    print("  -> %s\n" % state)

    print("2. Hardware reset, then watch BUSY")
    rst = Pin(RST_PIN, Pin.OUT)
    rst.value(1)
    time.sleep_ms(20)
    rst.value(0)
    time.sleep_ms(5)
    rst.value(1)
    time.sleep_ms(20)
    took = wait_busy_low()
    if took is None:
        print("  BUSY never went low within 5s\n")
    else:
        print("  BUSY went low after %dms\n" % took)

    print("3. SWRESET over SPI, then watch BUSY")
    spi = SPI(1, baudrate=4000_000, sck=Pin(SCK_PIN), mosi=Pin(MOSI_PIN))
    dc = Pin(DC_PIN, Pin.OUT)
    cs = Pin(CS_PIN, Pin.OUT)
    cs.value(1)
    dc.value(0)
    cs.value(0)
    spi.write(b"\x12")          # SWRESET
    cs.value(1)
    took = wait_busy_low()
    if took is None:
        print("  BUSY never went low within 5s\n")
    else:
        print("  BUSY went low after %dms -- panel is alive\n" % took)

    print("=== verdict ===")
    if state == "floating":
        print("""BUSY is not being driven by anything. The panel is electrically
absent as far as the Pico is concerned. In order of likelihood:

  1. The HAT is not seated, or is on backwards. It only goes on one way --
     line up the USB logo silkscreened on the HAT with the Pico's USB port.
  2. The display's flat flex cable is not seated in the ZIF connector on the
     HAT, or the little retaining flap is not closed. These do ship loose.
     Flip the flap up, push the FPC fully home, flip it back down.
  3. The Pico is not powering the HAT -- the HAT draws from VSYS, so it
     needs the Pico actually powered over USB, not just data.""")
    elif state == "driven-high":
        print("""BUSY reads high against both resistors. Careful: this does NOT prove
the panel is connected -- a pull-up fitted on the HAT itself would look
exactly the same with the display's flex cable unplugged.

Next step is pico/openloop.py, which drives the panel with BUSY ignored and
lets your eyes settle it:  tools/deploy.sh --openloop""")
    elif state == "driven-low":
        print("""BUSY is driven low, which means the panel is present and idle. The
hang is not where we thought -- re-run the smoke test and tell me how far it
gets now.""")
    elif state == "indeterminate":
        print("""The pull test is not usable on this chip (see probe_busy), so BUSY
tells us nothing on its own. Go straight to the open-loop test, which
ignores BUSY entirely and asks your eyes instead:

    tools/deploy.sh --openloop

If that draws nothing on the glass, the panel is not receiving commands and
the fault is physical -- seating, the display's flex cable, or power.""")
    else:
        print("Inconclusive. Tell me the two BUSY readings above.")


main()
