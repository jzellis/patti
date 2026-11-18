"""Patti's face: USB serial command loop.

Runs on boot. Speaks the line protocol documented in protocol.py over the
Pico's USB CDC port, which is also the MicroPython REPL -- hence two things
worth knowing before editing this file:

  * micropython.kbd_intr(-1) is not optional. A binary frame payload will
    sooner or later contain 0x03, and with the default REPL handling that
    byte raises KeyboardInterrupt and kills the firmware mid-transfer. We
    disable it for the life of the loop.
  * Because of the above, you cannot Ctrl-C your way back in. Two escape
    hatches: there is a BOOT_GRACE_MS window at startup before kbd_intr is
    disabled (mpremote can interrupt there), and the REPL command re-enables
    it and exits the loop cleanly. Worst case, hold BOOTSEL and reflash.

Nothing in this file may print anything that is not a protocol reply --
stdout IS the wire.
"""

import select
import sys
import time

import micropython

import protocol
from face import Face

BOOT_GRACE_MS = 3000        # window to break in with mpremote before lockout
POLL_MS = 200               # loop cadence; also how often face.tick() runs
PAYLOAD_TIMEOUT_MS = 5000   # abandon a frame whose payload stops arriving
MAX_LINE = 512

_stdin = sys.stdin.buffer
_stdout = sys.stdout.buffer


def reply(text):
    _stdout.write(text.encode() if isinstance(text, str) else text)
    _stdout.write(b"\n")


def read_payload(n, poll):
    """Read exactly n bytes, or return None if the host stalls."""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    deadline = time.ticks_add(time.ticks_ms(), PAYLOAD_TIMEOUT_MS)
    while got < n:
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return None
        if not poll.poll(100):
            continue
        chunk = _stdin.readinto(view[got:])
        if chunk:
            got += chunk
            # Only extend the deadline while bytes are actually flowing.
            deadline = time.ticks_add(time.ticks_ms(), PAYLOAD_TIMEOUT_MS)
    return buf


def handle(face, line, poll):
    """Handle one command line. Returns False to leave the loop."""
    parts = line.split(" ", 1)
    cmd = parts[0].upper()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "PING":
        reply("OK pong")

    elif cmd == "HELLO":
        reply("OK face %d %d %d" % (
            protocol.PROTO_VERSION, protocol.WIDTH, protocol.HEIGHT))

    elif cmd == "STATUS":
        st = face.status()
        reply("OK " + " ".join("%s=%s" % kv for kv in sorted(st.items())))

    elif cmd == "PRESET":
        name = arg.lower()
        if name not in protocol.PRESETS:
            reply("ERR unknown preset %s" % name)
        else:
            wait = face.show("preset", name)
            reply("OK queued %d" % wait if wait else "OK shown")

    elif cmd == "TEXT":
        bits = arg.split(" ", 1)
        try:
            scale = int(bits[0])
        except (ValueError, IndexError):
            reply("ERR usage: TEXT <scale> <text>")
            return True
        text = bits[1] if len(bits) > 1 else ""
        if not 1 <= scale <= 8:
            reply("ERR scale must be 1..8")
        else:
            wait = face.show("text", (scale, text))
            reply("OK queued %d" % wait if wait else "OK shown")

    elif cmd == "FRAME":
        bits = arg.split()
        if len(bits) != 2:
            reply("ERR usage: FRAME <nbytes> <checksum>")
            return True
        try:
            nbytes = int(bits[0])
        except ValueError:
            reply("ERR bad length")
            return True
        if nbytes != protocol.FRAME_BYTES:
            reply("ERR expected %d bytes, got %d" % (protocol.FRAME_BYTES, nbytes))
            return True
        reply("OK ready")
        data = read_payload(nbytes, poll)
        if data is None:
            reply("ERR payload timeout")
            return True
        want = bits[1].lower()
        have = protocol.checksum(data)
        if have != want:
            reply("ERR checksum %s != %s" % (have, want))
            return True
        wait = face.show("frame", data)
        reply("OK queued %d" % wait if wait else "OK shown")

    elif cmd == "CLEAR":
        wait = face.show("clear", None)
        reply("OK queued %d" % wait if wait else "OK shown")

    elif cmd == "REFRESH":
        # Force the housekeeping refresh now, ignoring nothing -- the panel
        # cooldown still applies.
        wait = face.cooldown_remaining()
        if wait:
            reply("OK queued %d" % wait)
        else:
            face.refresh()
            reply("OK shown")

    elif cmd == "REPL":
        reply("OK bye")
        return False

    elif cmd == "":
        pass

    else:
        reply("ERR unknown command %s" % cmd)

    return True


def main():
    poll = select.poll()
    poll.register(_stdin, select.POLLIN)

    face = Face()

    # Grace window: anything sent here is still handled by the normal REPL,
    # so mpremote can get a word in before we take the keyboard interrupt
    # away.
    time.sleep_ms(BOOT_GRACE_MS)
    micropython.kbd_intr(-1)

    pending = bytearray()
    try:
        while True:
            try:
                face.tick()
            except OSError as exc:
                reply("ERR panel %s" % exc)

            if not poll.poll(POLL_MS):
                continue

            chunk = _stdin.read(1)
            if not chunk:
                continue
            if chunk == b"\n":
                try:
                    line = pending.decode().strip()
                except UnicodeError:
                    line = ""
                pending = bytearray()
                if line:
                    try:
                        if not handle(face, line, poll):
                            break
                    except OSError as exc:
                        reply("ERR panel %s" % exc)
            elif chunk != b"\r":
                if len(pending) < MAX_LINE:
                    pending += chunk
                # Past MAX_LINE we drop bytes rather than grow unbounded;
                # the command will fail to parse, which is the right outcome.
    finally:
        micropython.kbd_intr(3)


if __name__ == "__main__":
    main()
