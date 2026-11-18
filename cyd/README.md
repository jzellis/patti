# cyd

Her chest screen: an ESP32-2432S028 "Cheap Yellow Display", the two-port
version (USB-C and micro-USB), 2.8" 320x240 with resistive touch. It's plugged
into the Pi's USB hub, which powers it and talks to it over serial (CH340,
`/dev/ttyUSB0`, 115200).

## Build and flash (on the Pi)

    arduino-cli core install esp32:esp32
    arduino-cli lib install LovyanGFX
    arduino-cli compile -b esp32:esp32:esp32 cyd
    arduino-cli upload  -b esp32:esp32:esp32 -p /dev/ttyUSB0 cyd

Stop anything else using the port first (a serial monitor, the brain).

## Talking to it

`brain/cyd.py` is the Pi's end, and the brain (rp-brain) runs it: a status
screen (mood, what she heard, motors, gimbal, camera, voice, network, time and
CPU temperature), and touching the screen wakes her (blink, and a heart on
the Sense HAT). By hand, with the brain stopped:

    python3 brain/cyd.py "Hello there"     # big words
    python3 brain/cyd.py --touches         # print touches
    python3 brain/cyd.py --calibrate       # touch the corners; kept in its flash

Opening the port reboots the CYD (Linux pulses DTR/RTS, its reset lines), so
cyd.py resends everything when it says hello.

The protocol is one line each way; everything it sends starts with `cyd: `.

| Pi sends                  | Does |
|---------------------------|------|
| `ping`                    | replies `cyd: pong` |
| `id`                      | the panel's ID registers (04, d3) and status (09), raw |
| `clear`                   | blank screen |
| `text <words>`            | the words, big and centred |
| `row <n> <colour> <text>` | status row n (0-7, 30 px each); white grey green amber red blue |
| `calibrate`               | touch calibration, saved in flash, printed as `cyd: calibrated ...` |

| CYD sends                 | When |
|---------------------------|------|
| `cyd: hello`              | it just booted (showing "Hello, world") |
| `cyd: touch <x> <y>`      | finger down, in screen pixels (320x240) |
| `cyd: move <x> <y>`       | it moved (up to 20 a second) |
| `cyd: release <x> <y>`    | finger up |

Her panel reads as an ILI9341 (`id` gives zeros for 04; an ST7789 would give
85 85 52), so the sketch uses ILI9341, with no inversion (checked by eye:
inversion on gives black on white).

The board's factory firmware is backed up on the Pi at
`~/robotpattinson-backups/cyd-factory-4MB.bin` (restore with
`esptool --port /dev/ttyUSB0 write-flash 0 cyd-factory-4MB.bin`).

## Pins (from the seller's diagram)

| What        | Pins |
|-------------|------|
| Display SPI | SCK 14, MOSI 13, MISO 12, CS 15, DC 2, backlight 21 |
| Touch (XPT2046) | CLK 25, DIN 32, OUT 39, CS 33, IRQ 36 |
| RGB LED     | 4, 16, 17 |
| Speaker     | 26 (1.5 W, 4 ohm connector) |
| Light sensor (LDR) | 34 |
| SD card     | CS 5, MOSI 23, SCK 18, MISO 19 |
| Expansion   | IO35 (input only), IO22, IO21 (shared with backlight), IO27 |
