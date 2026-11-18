#!/usr/bin/env python3
"""Drive Patti from the keyboard, through pico/drive.py.

    PORT=/dev/ttyACM0 python3 tools/drive.py

    up / w      forward            left / a    spin left
    down / s    reverse            right / d   spin right
    space       stop               + / -       speed up / down (0.1 steps)
    q           stop and quit

A key sets what the robot is doing and it keeps doing it until you press
another (no holding keys down): terminal key repeat starts too late for the
Pico's 0.5s dead-man. This tool resends the current command 10x a second;
if it dies or the cable comes out, the Pico stops the motors by itself.
"""

import curses
import os
import sys
import time

import serial

PORT = os.environ.get("PORT", "/dev/ttyACM0")
SEND_S = 0.1
SPEED = 0.4

# key -> (name, left sign, right sign)
MOVES = {
    curses.KEY_UP: ("forward", 1, 1), ord("w"): ("forward", 1, 1),
    curses.KEY_DOWN: ("reverse", -1, -1), ord("s"): ("reverse", -1, -1),
    curses.KEY_LEFT: ("spin left", -1, 1), ord("a"): ("spin left", -1, 1),
    curses.KEY_RIGHT: ("spin right", 1, -1), ord("d"): ("spin right", 1, -1),
    ord(" "): ("stopped", 0, 0),
}


def start(ser):
    """Break to the REPL and start drive.run(); wait for READY."""
    ser.write(b"\r\x03\x03")
    time.sleep(0.3)
    ser.reset_input_buffer()
    ser.write(b"import drive; drive.run()\r")
    deadline = time.monotonic() + 5
    seen = b""
    while time.monotonic() < deadline:
        seen += ser.read(256)
        if b"READY" in seen:
            return
        time.sleep(0.02)
    sys.exit("Pico didn't start drive.run(). It said:\n%s\n"
             "Is drive.py on the board? Run tools/deploy.sh first."
             % seen.decode(errors="replace"))


def ui(scr, ser):
    curses.curs_set(0)
    scr.nodelay(True)
    move, ls, rs = "stopped", 0, 0
    speed = SPEED
    said = ""
    buf = b""
    next_send = 0.0
    while True:
        key = scr.getch()
        if key == ord("q"):
            return
        if key in MOVES:
            move, ls, rs = MOVES[key]
            next_send = 0.0            # send the change now
        elif key in (ord("+"), ord("=")):
            speed = min(1.0, round(speed + 0.1, 1))
            next_send = 0.0
        elif key in (ord("-"), ord("_")):
            speed = max(0.1, round(speed - 0.1, 1))
            next_send = 0.0

        now = time.monotonic()
        if now >= next_send:
            ser.write(b"T %.2f %.2f\n" % (ls * speed, rs * speed))
            next_send = now + SEND_S

        buf += ser.read(256)
        *lines, buf = buf.split(b"\n")
        for line in lines:
            line = line.strip().decode(errors="replace")
            if line:
                said = line

        scr.erase()
        scr.addstr(0, 0, "Patti drive  (%s)" % PORT)
        scr.addstr(2, 0, "arrows/wasd move   space stop   +/- speed   q quit")
        scr.addstr(4, 0, "doing: %-12s speed: %.1f" % (move, speed))
        scr.addstr(5, 0, "pico:  %s" % said)
        scr.refresh()
        time.sleep(0.02)


def main():
    try:
        ser = serial.Serial(PORT, 115200, timeout=0)
    except serial.SerialException as e:
        sys.exit("Can't open %s: %s" % (PORT, e))
    start(ser)
    try:
        curses.wrapper(ui, ser)
    finally:
        ser.write(b"S\nq\n")
        ser.flush()
        time.sleep(0.2)
        print("stopped")


if __name__ == "__main__":
    main()
