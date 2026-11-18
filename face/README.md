# Patti: Face

The e-paper face. A Waveshare Pico-ePaper-2.13 (SKU WAV-19406, 250x122,
black/white) on a Raspberry Pi Pico running MicroPython, driven over USB
serial from a Pi 4.

No animation: e-paper refresh is ~2s, so every update is a single full
refresh that then persists on the glass, including through power loss.

## Layout

```
face/
  shared/          imported by BOTH the Pico and the Pi -- keep dependency-free
    protocol.py      wire protocol, panel geometry, safety limits
    expressions.py   expression geometry as data (so both sides draw it identically)
  pico/            runs on the Pico
    main.py          USB serial command loop (runs on boot)
    face.py          panel controller: enforces the panel safety rules
    draw.py          framebuf renderer (presets + blocky text), host-frame blit
    smoketest.py     hardware bring-up test, run this first
    lib/
      epd2in13_v4.py vendored Waveshare driver (see "Vendored driver" below)
  host/            runs on the Pi 4
    facectl          CLI
    robotface/
      render.py      PIL rendering: presets, real typography, image files
      client.py      serial client
  tests/
    test_layout.py   byte-layout checks, no hardware needed
  tools/
    deploy.sh        mpremote deploy
```

## Hardware

The board is a HAT: it plugs straight onto the Pico, no wiring. For
reference, the fixed pin mapping is

| e-Paper | Pico | |
|---|---|---|
| VCC | VSYS | power |
| GND | GND | |
| DIN | GP11 | SPI1 MOSI |
| CLK | GP10 | SPI1 SCK |
| CS  | GP9  | chip select |
| DC  | GP8  | data/command |
| RST | GP12 | reset |
| BUSY| GP13 | busy |

## Board

Currently a **Raspberry Pi Pico 2 W (RP2350)**, MicroPython 1.29.0. The pin
map and this code are the same on RP2040, but one thing differs and it bit us
during bring-up:

> On RP2350, a floating GPIO input latches high even with the internal
> pull-down enabled. So the usual "read it against the pull-up, then against
> the pull-down" trick for telling a floating pin from a driven one gives a
> confident wrong answer. `pico/diagnose.py` now reads known-floating control
> pins (GP2/GP3/GP22) alongside whatever it is testing, and reports the test
> as unusable rather than inventing a verdict.

## Bring-up

Do these in order. Step 1 separates "hardware and driver revision are right"
from "our code is right", which is worth five minutes.

```sh
pip install mpremote
tools/deploy.sh --smoke          # libs only, then run the smoke test
```

You should see a clear white panel, then a smiling face, then text. If you
get garbage or a blank panel, it is almost certainly the driver revision --
see "Panel revision" below.

Then deploy the firmware and talk to it:

```sh
tools/deploy.sh                  # full deploy, resets the Pico
cd host && pip install -r requirements.txt
./facectl ping
./facectl smile
./facectl say "HELLO I AM PATTI"
./facectl status
./facectl info               # hostname, IPs (incl. Tailscale), uptime, temp, mem, disk
./facectl snap               # webcam still, cropped to fill, auto-contrast, dithered
                             #   --camera /dev/videoN, --no-dither, --save x.png --dry-run
./facectl sense              # every Sense HAT reading (Pi only); --every 60 to keep updating
```

You can render without any hardware attached, which is handy for working on
expressions:

```sh
./facectl preview smile /tmp/smile.png --scale 3
./facectl preview "text:SOME WORDS" /tmp/text.png --scale 3
```

## Protocol

Newline-terminated ASCII over the Pico's USB CDC port. One command (`FRAME`)
is followed by a binary payload. Full details in `shared/protocol.py`.

```
HELLO                      -> OK face <proto> <w> <h>
PING                       -> OK pong
STATUS                     -> OK showing=... cooldown=... refreshes=...
PRESET smile|frown|sad|flat-> OK shown | OK queued <secs>
TEXT <scale> <text>        -> OK shown | OK queued <secs>     (Pico's own font)
FRAME <nbytes> <checksum>  -> OK ready, <payload>, OK shown
CLEAR                      -> OK shown | OK queued <secs>
REFRESH                    -> OK shown | OK queued <secs>
REPL                       -> OK bye    (hands the REPL back)
```

Errors are a single `ERR <reason>` line.

### Who renders what

The Pico can draw the four presets and some blocky 8x8-derived text on its
own, so the face works at boot before the Pi is up. Anything with real
typography is rendered on the Pi with PIL and a proper TTF and shipped over
as a `FRAME`.

The frame payload is exactly what PIL hands you for a 250x122 mode `"1"`
image: row-major, MSB-first, 1 = white, rows padded to 32 bytes, 3904 bytes
total. The Pico wraps that in a framebuf and lets `framebuf.blit` convert it
into the panel's column-packed layout in C, so neither side does bit
twiddling. `tests/test_layout.py` asserts all of this without hardware.

## Panel safety

Waveshare's precautions page describes two of these as causing damage that
"cannot be repaired", so they are enforced in `pico/face.py` rather than
trusted to whatever is driving the thing over USB:

- **Never idle powered-and-awake.** Every update is bracketed
  `init() -> write -> display() -> sleep()`. There is no code path that
  leaves the panel awake.
- **One refresh per 5s, floor.** A request inside the cooldown is not
  rejected, it is held as the pending frame and applied when the cooldown
  expires. Later requests overwrite the pending one, so a host stuck in a
  loop costs one refresh per interval instead of cooking the panel.
  (Waveshare *recommends* 180s, and this was 180s, then 30s, until
  2026-10-10, but that is wear guidance, not one of their "cannot be
  repaired" warnings. Rated life is ~1M refreshes and a face only refreshes
  when it changes, so 5s is a backstop against runaway hosts, not a budget.)
- **A refresh at least every 20h** even if nobody asks, plus a
  white/black/white scrub every 40 refreshes. That is the burn-in /
  ghosting defence.
- **Full refreshes only.** Chained partial refreshes are what leave
  permanent artefacts, and nothing here animates, so we never use them.

## Known trade-offs

- **Cooldown resets on boot.** The Pico has no battery-backed clock, so on
  power-up it genuinely cannot know how long ago the last refresh was. We
  let the first update after boot draw immediately, because a face that
  ignores you for the whole cooldown after power-on is useless. The cost is that
  repeated power-cycling could refresh faster than the 5s floor. Deliberate
  choice; if it ever matters, the fix is for the Pi to send the elapsed time
  at handshake.
- **No refresh on boot.** The glass physically holds its image through a
  power cut, so we do not redraw at startup. We only restore enough state to
  know *what* to re-send when housekeeping comes due. The exception is a
  first-ever boot with no saved state, where the panel may be holding
  garbage and we draw the default expression.
- **stdout is the wire.** The Pico's USB CDC is both the REPL and our
  protocol channel, so nothing on the device may `print()` anything that is
  not a protocol reply.
- **Ctrl-C is disabled while the firmware runs.** It has to be: a binary
  frame payload will eventually contain `0x03`, which would otherwise raise
  `KeyboardInterrupt` mid-transfer and kill the firmware. Escape hatches are
  the 3s grace window at boot, the `REPL` command, and BOOTSEL as a last
  resort.

## Vendored driver

`pico/lib/epd2in13_v4.py` is Waveshare's `Pico_ePaper-2.13_V4.py` from
<https://github.com/waveshare/Pico_ePaper_Code>, with three changes:

1. The trailing `__main__` demo block removed, so it imports as a library.
2. Its `print('busy')` / `print('init')` calls replaced with `pass` -- they
   wrote to stdout, which is our protocol channel, and would have corrupted
   every reply during a refresh. Each line is marked `# RP:`.
3. `init_fast()` (in both the Portrait and Landscape classes) called
   `self.SetWindow(...)`, which doesn't exist -- the method is `SetWindows`.
   Any fast-mode refresh crashed with `AttributeError`, and since `Face`
   defaults to fast mode, `main.py` died on its first boot-time draw. Fixed
   and marked `# RP:`. Found 2026-10-09 with a replacement V4 HAT.

`face.py` also bypasses the driver's `display()`, which sends the 4000-byte
frame one byte per SPI transaction. The byte order it produces is just the
framebuf's 16 column-bands emitted back to front, each band contiguous, so
we reorder with 16 slice copies and push it in a single SPI write.
`tests/test_layout.py` asserts our stream is byte-identical to the vendor's.

## Panel revision

The repo also ships `Pico_ePaper-2.13.py` (V1/V2) and `_V3.py`. V4 is
backward compatible with V3, so this code covers V3 and V4. **A V2 panel
running V4 init will give you garbage or a blank screen.** Check the marking
on the board or FPC. If it is V2, swap `pico/lib/epd2in13_v4.py` for the V2
driver -- nothing else in this tree depends on the revision.
