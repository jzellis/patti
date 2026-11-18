"""Wire protocol between the Pi (host) and the Pico (face).

This module is imported by BOTH CPython on the Pi and MicroPython on the
Pico, so it must stay dependency-free and avoid anything MicroPython lacks.

Transport is the Pico's USB CDC serial port. Commands are newline-terminated
ASCII. One command, FRAME, is followed by a fixed-size binary payload.

    HELLO                       -> OK face <proto> <w> <h>
    PING                        -> OK pong
    STATUS                      -> OK <key>=<val> ...
    PRESET <name>               -> OK shown | OK queued <secs>
    TEXT <scale> <utf8 text>    -> OK shown | OK queued <secs>
    FRAME <nbytes> <checksum>   -> OK ready, then payload, then OK shown
    CLEAR                       -> OK shown | OK queued <secs>
    REPL                        -> OK bye    (drops to the REPL; see main.py)

Errors come back as "ERR <reason>". Every reply is a single line.

Frame payload layout
--------------------
Exactly what PIL hands you for a 250x122 image in mode "1": row-major,
MSB-first, 1 = white, rows padded to a byte boundary. 250 px -> 32 bytes
per row (last 6 bits of each row unused), 122 rows -> 3904 bytes.

The Pico wraps that buffer in a framebuf and blits it into the panel's
native (column-packed) buffer, so the host never has to know the panel's
internal layout.
"""

PROTO_VERSION = 1

# Panel geometry, landscape.
WIDTH = 250
HEIGHT = 122

# Host-side frame layout. Rows pad to a byte boundary, which is also the
# stride framebuf needs on the Pico (must be a multiple of 8 bits).
ROW_BYTES = (WIDTH + 7) // 8        # 32
FRAME_BYTES = ROW_BYTES * HEIGHT    # 3904
STRIDE_PX = ROW_BYTES * 8           # 256

# Monochrome pixel values. The panel treats 0 as black, 1 as white, and so
# does PIL mode "1" once packed, so the two agree with no inversion.
BLACK = 0
WHITE = 1

PRESETS = ("smile", "frown", "sad", "flat")

# Panel safety limits, enforced on the PICO, not trusted to the host.
# Waveshare *recommends* >= 180s between refreshes, but that's guidance on
# wear, not one of their "cannot be repaired" warnings (those are: never idle
# powered/awake, never chain partial refreshes -- both enforced elsewhere).
# Rated life is ~1M refreshes. A face only refreshes when its expression
# changes, so even thousands of changes a day last years; the floor is just
# a backstop against a host stuck in a loop (5s non-stop would be ~2 months).
MIN_REFRESH_INTERVAL_S = 5          # never refresh faster than this
MAX_REFRESH_INTERVAL_S = 20 * 3600  # refresh at least this often (<24h)
CLEAR_CYCLE_EVERY = 40              # full white/black/white scrub cadence

DEFAULT_BAUD = 115200               # nominal; CDC ignores it


def checksum(data):
    """A 32-bit additive checksum of a frame payload, as 8 lowercase hex.

    USB CDC already CRCs at the link layer, so this is not here to catch
    line noise. It catches the failure that actually bites a length-prefixed
    binary protocol: losing framing and reading the payload off by some
    bytes. A plain sum detects that, needs no table, and costs nothing on
    either side -- binascii.crc32 is not guaranteed present in every
    MicroPython build, so we don't depend on it.
    """
    total = 0
    for b in memoryview(data):
        total += b
    return "%08x" % (total & 0xFFFFFFFF)
