"""Motor command loop over USB serial. Driven by tools/drive.py (keyboard),
later by the brain on the Pi.

    import drive; drive.run()

Plain-text commands, one per line, so the same protocol works over USB
serial, BLE UART or WiFi:

    T <left> <right>   drive: each side -1.0 .. 1.0 (left = A, C; right = B, D)
    M <A|B|C|D> <s>    one motor, -1.0 .. 1.0, for testing
    S                  stop now (all coast)
    ?                  report "SPD <a> <b> <c> <d>" (actual, ramped speeds)
    q                  stop and return to the REPL

T and M are quiet; bad lines get "ERR <line>".

Dead-man: if no T, M or S arrives for DEADMAN_MS, every motor coasts at once
and it prints "DEADMAN". A host holds motion by resending its current
command (~10x/s); if it crashes or the link drops, the robot stops.

Ramping: actual speeds move toward their targets by at most ACCEL (full
scale per second), so starts and reversals don't pull a current spike from
the power bank (each TT motor stalls at 2.8A). Stops -- S, dead-man, q,
errors -- are never ramped: they coast immediately.

Watchdog: run() arms the hardware watchdog, so if this loop hangs the Pico
reboots, and main.py stops every motor at boot. (The PCA9685 keeps driving
whatever it was last told through a Pico reset, so that boot-time stop
matters.) The watchdog can't be disarmed once started, so when run() returns
a timer keeps feeding it; a soft reset (Ctrl-D) kills that timer, and the
Pico then hard-reboots within WDT_MS, which is harmless.
"""

import select
import sys
import time

from machine import WDT, Timer

import motors

TICK_MS = 20          # control loop, 50Hz
DEADMAN_MS = 500
ACCEL = 2.0           # full scale per second: 0 -> 0.4 takes 0.2s
WDT_MS = 1000

LEFT, RIGHT = "AC", "BD"
NAMES = "ABCD"

_idle_feed = None     # Timer feeding the watchdog while run() isn't


def _clamp(v):
    return max(-1.0, min(1.0, v))


def run():
    global _idle_feed
    wdt = WDT(timeout=WDT_MS)
    if _idle_feed:
        _idle_feed.deinit()
        _idle_feed = None

    m = motors.Motors()
    target = dict.fromkeys(NAMES, 0.0)
    speed = dict.fromkeys(NAMES, 0.0)
    step = ACCEL * TICK_MS / 1000

    def stop():
        m.stop_all()
        for n in NAMES:
            target[n] = speed[n] = 0.0

    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    buf = ""
    last = time.ticks_ms()
    armed = False         # dead-man only counts once something is commanded
    print("READY")
    try:
        while True:
            wdt.feed()
            while poll.poll(0):
                ch = sys.stdin.read(1)
                if ch not in "\r\n":
                    if len(buf) < 48:
                        buf += ch
                    continue
                line, buf = buf.strip(), ""
                if not line:
                    continue
                cmd = line.split()
                op = cmd[0]
                try:
                    if op == "q":
                        return
                    if op == "?":
                        print("SPD %+.2f %+.2f %+.2f %+.2f" % tuple(speed[n] for n in NAMES))
                        continue
                    if op == "S":
                        stop()
                    elif op == "T" and len(cmd) == 3:
                        left, right = _clamp(float(cmd[1])), _clamp(float(cmd[2]))
                        for n in LEFT:
                            target[n] = left
                        for n in RIGHT:
                            target[n] = right
                    elif op == "M" and len(cmd) == 3 and cmd[1].upper() in NAMES:
                        target[cmd[1].upper()] = _clamp(float(cmd[2]))
                    else:
                        raise ValueError
                except ValueError:
                    print("ERR", line)
                    continue
                last = time.ticks_ms()
                armed = True

            if armed and time.ticks_diff(time.ticks_ms(), last) > DEADMAN_MS:
                stop()
                armed = False
                print("DEADMAN")

            for n in NAMES:
                d = target[n] - speed[n]
                if d:
                    speed[n] += max(-step, min(step, d))
                    if abs(speed[n]) < 1e-6:
                        speed[n] = 0.0
                    m[n].run(speed[n])
            time.sleep_ms(TICK_MS)
    finally:
        m.stop_all()
        _idle_feed = Timer(period=WDT_MS // 4, callback=lambda t: wdt.feed())
        print("BYE")
