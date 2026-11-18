"""The face controller: owns the panel and enforces its safety rules.

Everything that touches the e-paper goes through here. The rules below come
from the Waveshare precautions page, where two of them are described as
causing damage that "cannot be repaired", so they are enforced on the Pico
rather than trusted to whatever is driving it over USB:

  * The panel must never idle powered-and-awake. Every update is bracketed
    init() -> write -> display() -> sleep(), with no path that leaves it
    awake. A woken panel ignores image data until re-initialised anyway, so
    the bracket is mandatory, not just prudent.
  * Refreshes are rate-limited to one per MIN_REFRESH_INTERVAL_S. A request
    that arrives inside the cooldown is not rejected -- it is held as the
    pending frame and applied when the cooldown expires. Later requests
    overwrite the pending one, so a host stuck in a loop costs us one
    refresh per interval instead of cooking the panel.
  * The panel is refreshed at least once per MAX_REFRESH_INTERVAL_S even if
    nobody asks, and scrubbed with a white/black/white cycle every
    CLEAR_CYCLE_EVERY refreshes. That is what actually clears accumulated
    ghosting.
  * Only full refreshes. Chained partial refreshes are what produce
    permanent artefacts, and we have no use for them -- nothing animates.
"""

import json
import time

import draw
import protocol
from lib.epd2in13_v4 import EPD_2in13_V4_Landscape

STATE_PATH = "face_state.json"
FRAME_PATH = "face_frame.bin"

DEFAULT_EXPRESSION = "flat"

# Panel RAM is 16 byte-columns of 250 rows; see _push().
_COLS = protocol.WIDTH          # 250 bytes per column-row in the fb
_BANDS = 16                     # 128 padded px / 8


class Face:
    def __init__(self, fast=True):
        self.fast = fast
        # Constructing the driver runs init(), which powers the panel up, so
        # put it straight back to sleep until we actually have something to
        # show.
        self.epd = EPD_2in13_V4_Landscape()
        self.epd.sleep()

        self.epd.fill(protocol.WHITE)

        self._scratch = bytearray(_BANDS * _COLS)
        self.refresh_count = 0
        # Start with the cooldown already expired so the first update
        # after power-on draws immediately. We cannot do better: the Pico
        # has no battery-backed clock, so on boot we genuinely do not know
        # how long ago the last refresh was. See README, 'Known trade-offs'.
        self.last_refresh = time.ticks_add(
            time.ticks_ms(), -(protocol.MIN_REFRESH_INTERVAL_S * 1000 + 1000))
        self.pending = None          # deferred (kind, value) during cooldown
        self.current = None          # (kind, value) actually on the glass

        self._restore()

    # --- panel plumbing ---------------------------------------------------

    def _push(self):
        """Send the framebuf to the panel and refresh, then sleep.

        The vendor display() calls send_data() once per byte: 4000 separate
        SPI transactions with GPIO toggling around each. The byte order it
        produces is just the framebuf's 16 column-bands emitted back to
        front, and each band is contiguous, so we can reorder with 16 slice
        copies and push the lot in a single SPI write.
        """
        epd = self.epd
        src = epd.buffer
        out = self._scratch
        pos = 0
        for band in range(_BANDS - 1, -1, -1):
            out[pos:pos + _COLS] = src[band * _COLS:(band + 1) * _COLS]
            pos += _COLS

        if self.fast:
            epd.init_fast()
        else:
            epd.init()
        epd.send_command(0x24)
        epd.send_data1(out)
        if self.fast:
            epd.TurnOnDisplay_Fast()
        else:
            epd.TurnOnDisplay()
        epd.sleep()

        self.refresh_count += 1
        self.last_refresh = time.ticks_ms()

    # --- cooldown ---------------------------------------------------------

    def cooldown_remaining(self):
        """Seconds until another refresh is allowed; 0 if one is allowed now."""
        elapsed = time.ticks_diff(time.ticks_ms(), self.last_refresh)
        remaining = protocol.MIN_REFRESH_INTERVAL_S * 1000 - elapsed
        return 0 if remaining <= 0 else (remaining + 999) // 1000

    def _render(self, kind, value):
        """Draw (kind, value) into the framebuf without touching the panel."""
        if kind == "preset":
            draw.expression(self.epd, value)
        elif kind == "text":
            scale, text = value
            draw.text_block(self.epd, text, scale)
        elif kind == "frame":
            self.epd.fill(protocol.WHITE)
            draw.blit_host_frame(self.epd, value)
        elif kind == "clear":
            self.epd.fill(protocol.WHITE)
        else:
            raise ValueError("unknown kind: " + str(kind))

    def show(self, kind, value):
        """Show something, or queue it if we are inside the cooldown.

        Returns 0 if it went to the glass, else seconds until it will.
        """
        wait = self.cooldown_remaining()
        if wait:
            self.pending = (kind, value)
            return wait

        self.pending = None
        self._render(kind, value)
        if self.refresh_count and self.refresh_count % protocol.CLEAR_CYCLE_EVERY == 0:
            self._scrub()
        self._push()
        self.current = (kind, value)
        self._persist()
        return 0

    # --- housekeeping -----------------------------------------------------

    def tick(self):
        """Call often. Applies any pending frame and does burn-in upkeep."""
        if self.pending and not self.cooldown_remaining():
            kind, value = self.pending
            self.show(kind, value)
            return "pending"

        elapsed = time.ticks_diff(time.ticks_ms(), self.last_refresh)
        if elapsed >= protocol.MAX_REFRESH_INTERVAL_S * 1000:
            self.refresh()
            return "housekeeping"
        return None

    def refresh(self):
        """Re-apply the current image. Resets the burn-in clock."""
        if self.current is None:
            self.show("preset", DEFAULT_EXPRESSION)
            return
        kind, value = self.current
        self._render(kind, value)
        if self.refresh_count % protocol.CLEAR_CYCLE_EVERY == 0:
            self._scrub()
        self._push()

    def _scrub(self):
        """White/black/white cycle to clear accumulated ghosting.

        Done inside one power-up bracket, the way the vendor demos clear, and
        it leaves the framebuf untouched so the caller's image still goes up
        immediately afterwards.
        """
        epd = self.epd
        white = bytearray(b'\xff') * (_BANDS * _COLS)
        black = bytearray(_BANDS * _COLS)
        epd.init()
        for fill in (white, black, white):
            epd.send_command(0x24)
            epd.send_data1(fill)
            epd.TurnOnDisplay()
        epd.sleep()

    # --- state persistence ------------------------------------------------
    #
    # The glass physically holds its image through a power cut, so we do NOT
    # refresh on boot -- that would let a reboot loop refresh the panel far
    # faster than the MIN_REFRESH_INTERVAL_S floor allows, since ticks_ms()
    # restarts at zero.
    # We only persist enough to know WHAT to re-send when housekeeping comes
    # due, and assume on boot that what is on the glass is fresh.

    def _persist(self):
        kind, value = self.current
        try:
            if kind == "frame":
                with open(FRAME_PATH, "wb") as f:
                    f.write(value)
                payload = {"kind": "frame"}
            else:
                payload = {"kind": kind, "value": value}
            with open(STATE_PATH, "w") as f:
                json.dump(payload, f)
        except OSError:
            pass  # a full or flaky filesystem must not take the face down

    def _restore(self):
        try:
            with open(STATE_PATH) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            # Nothing on record: first boot, or the panel may be holding
            # garbage. This is the one case where we do refresh at startup.
            self.show("preset", DEFAULT_EXPRESSION)
            return

        kind = saved.get("kind")
        if kind == "frame":
            try:
                with open(FRAME_PATH, "rb") as f:
                    data = bytearray(f.read())
                if len(data) == protocol.FRAME_BYTES:
                    self.current = ("frame", data)
                    return
            except OSError:
                pass
            self.current = ("preset", DEFAULT_EXPRESSION)
        elif kind == "text":
            value = saved.get("value") or [3, ""]
            self.current = ("text", (value[0], value[1]))
        elif kind in ("preset", "clear"):
            self.current = (kind, saved.get("value"))
        else:
            self.current = ("preset", DEFAULT_EXPRESSION)

    # --- introspection ----------------------------------------------------

    def status(self):
        kind = self.current[0] if self.current else "none"
        return {
            "proto": protocol.PROTO_VERSION,
            "showing": kind,
            "refreshes": self.refresh_count,
            "cooldown": self.cooldown_remaining(),
            "pending": self.pending[0] if self.pending else "none",
            "mode": "fast" if self.fast else "quality",
        }
