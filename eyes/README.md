# eyes

Two 1.28" round GC9A01 LCDs (240x240, SPI). Left and right are hers, as seen
from behind her.

**Since 2026-10-31 they're on the gimbal Pico** (the Kitronik board in her
head), wired as the "Kitronik Eye Wiring" chart: yellow SCK on servo header 5
(GP6), green SDA on 6 (GP7), purple reset on 7 (GP8), blue DC on 8 (GP9), the
reds on 3 (GP4, her left) and 4 (GP5, her right), orange LED on the breakout's
GP27, white VCC on a breakout 3V3, black on a breakout GND. The Pi still draws
them (eyes.py); picoeyes.py sends the changed rectangles, run-length coded,
over the Pico's USB to vision/pico/keyctl.py, which hands them to
pico/eyepanels.py. vision/tools/deploy.sh puts eyepanels.py on the Pico, and
`mpremote run eyes/pico/eyetest.py` fills each eye a colour (stop rp-brain
first: it holds the port). The brain uses them there unless started with
--pi-eyes. The rest of this page is the old Pi wiring, on SPI6.

    # /boot/firmware/config.txt, then reboot
    dtoverlay=spi6-2cs

    python3 eyetest.py                  left red, right blue, then both green
    python3 eyetest.py left white       one eye white, the other black

Needs python3-spidev and python3-lgpio (both on Raspberry Pi OS), and the user
in the spi and gpio groups.

## Wiring

The eyes share every line except chip select. Wire colours are the ones on
the robot (Y-spliced, one lead per colour to the Pi; the two reds don't join).

| Wire   | Eye pin | Pi pin (physical) | GPIO   | Notes |
|--------|---------|-------------------|--------|-------|
| white  | VCC     | 17                | 3.3 V  | |
| black  | GND     | 34                | GND    | |
| yellow | SCK     | 40                | GPIO21 | SPI6 clock |
| green  | SDA     | 38                | GPIO20 | SPI6 data (MOSI) |
| blue   | DC      | 15                | GPIO22 | |
| purple | Reset   | 37                | GPIO26 | shared: resetting one resets both |
| orange | LED     | 33                | GPIO13 | backlight, both eyes |
| red    | CS      | 13                | GPIO27 | **left** eye, SPI6 CE1, spidev6.1 |
| red    | CS      | 12                | GPIO18 | **right** eye, SPI6 CE0, spidev6.0 |

The CS wires went on the other way round from the plan (left on 12); the
software follows the wiring, so leave them as they are.
