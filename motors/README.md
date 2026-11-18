# Patti: Motors

Drive motors. Status: driving open loop from the keyboard, with a dead-man
stop and a watchdog. No encoders yet.

## Hardware

- **Motor board:** Waveshare Pico-Motor-Driver (SKU 19764). A PCA9685 (I2C
  PWM chip) drives two TB6612FNG dual H-bridges, for up to 4 DC motors.
- **Motors:** DFRobot FIT0450, TT gear motors with hall encoders.
  - 6V rated, 7.5V maximum, 160 RPM, 120:1 gearing.
  - Stall current 2.8A. The TB6612FNG handles 1.2A continuous and 3.2A peak.
  - Encoders: 8 pulses per motor turn, so 960 per wheel turn per channel, or
    3840 counts per wheel turn in full quadrature.

| | |
|---|---|
| I2C | I2C0, SDA GP20, SCL GP21, address 0x40 (5 address jumpers) |
| Motor A / B / C / D | PCA9685 channels (PWM, IN1, IN2) = (0,1,2) / (3,4,5) / (6,7,8) / (9,10,11) |
| Power | VIN 6-12V, passed straight to the motors. Onboard 5V regulator, up to 3A |
| Don't reuse | GP20/GP21 (I2C), GP6/GP7 (alternate I2C routing), GP14 (power enable) |

The channel map comes from Waveshare's demo code. The pin notes come from
their schematic: <https://files.waveshare.com/wiki/Pico-Motor-Driver/Pico_Motor_Driver_SCH.pdf>.

### Power

A USB-PD power bank feeds everything:

```
power bank --(9V PD trigger)--+-- buck @ 6V ---------------- gimbal servos
                              +-- buck @ 7.5V -- 5A fuse --- motor board VIN
           --(5V USB port)------------------------------------ Pi 4
```

- **The board passes VIN straight to the motors,** and the motors are rated
  6V with a 7.5V maximum.
- **`MAX_DUTY = 0.8`** gives the motors 6.0V from the 7.5V rail. You can
  raise it to about 0.93 (7V) if you need more speed.
- **`MIN_DUTY = 0.18`:** any non-zero speed starts at this duty, because
  below it the gearbox friction wins and a stopped motor just hums. B (back
  right) is the stickiest: in the step test it needed 0.16 duty, and the
  others 0.08 or less. Speeds map onto 0.18 to 0.8, so 0.1 still moves every
  wheel. Raise it if a motor hums at the slowest speed, and lower it if the
  slowest speed lurches.
- **The Pico is powered from VIN too,** through the board's 5V regulator.
  Motor current spikes that sag VIN can brown out the Pico. A bulk capacitor
  on VIN (470-1000uF) and ramping speed changes in software both help.

### Safety

The PCA9685's OE pin is hard-wired to GND, so there is **no hardware
all-off**. The chip also holds its outputs if the Pico crashes. Defences:

- `Motors()` stops every channel when it's constructed. Construct it first
  thing at boot.
- Motion code wraps in `try/finally: stop_all()`.
- `drive.py` has a dead-man stop: if no command arrives for 0.5 s, every
  motor coasts. It also arms the hardware watchdog, so a hung Pico reboots,
  and `main.py` stops every motor at boot.
- To do, once the encoders are in: stall detection. If a motor is powered
  but its encoder isn't moving, cut it. 2.8A stall current is more than the
  TB6612FNG's continuous rating.

### Encoders (not connected yet)

- **Voltage:** the encoders need 4.5-7.5V, so power them from the board's 5V
  regulator. Their A/B outputs are then 5V, and **must not go straight to
  the Pico**, whose pins tolerate only 3.3V.
- **Level shifting:** use two 4-channel BSS138 level shifters.
  - HV side: 5V and the encoder signals.
  - LV side: the Pico's 3V3(OUT) and its GPIO pins.
  - Common ground for everything.
- **Pins:** 8 GPIO in all, avoiding the "don't reuse" pins above.
- **Wire colours:** DFRobot's wiki says its pinout and the labels on the
  V2.0 encoder board disagree. Identify A/B by spinning a wheel by hand
  while the Pico reports, before trusting the colours.

## Bring-up

1. **Wheels off the ground.**
2. **Motor leads:** connect them to the board's motor terminals. Leave the
   encoder leads disconnected.
3. **Power:** connect VIN (7-8V) and switch it on.
4. **Run the smoke test:**

```sh
mpremote connect list                       # find the motor Pico's port
PORT=/dev/ttyACMx tools/deploy.sh --smoke
```

Each motor runs forward, then reverse, at 40%, in the order A, B, C, D.
Note which wheel each letter drives, and whether "forward" really is
forward for it. Any that run backwards go in `Motors(invert="...")`.

### Result (2026-10-15)

The smoke test passed on the first run: all four motors turned both ways.
"Forward" is forward on every wheel, so no inverts are needed.

| Motor | Wheel |
|---|---|
| A | back left |
| B | back right |
| C | front left |
| D | front right |

So the left side is A and C, and the right side is B and D.

### Rewired (2026-10-20)

After the move to the single-level deck, the back wheels ended up on each
other's ports and the front motors' leads reversed. That's fixed in
software (pico/motors.py) rather than rewiring: `CHANNELS` maps each wheel
letter to the board port it's really on, and `INVERT = "CD"` flips the
fronts. The letters still name wheels, so A/C is still the left side.

| Board port | Wheel | Letter | Inverted | Positive wire |
|---|---|---|---|---|
| 1 (A) | back right | B | no | green |
| 2 (B) | back left | A | no | red |
| 3 (C) | front left | C | yes | yellow |
| 4 (D) | front right | D | yes | blue |

Checked on the lift, 2026-10-20: each wheel spins forward on its own `M`
command.

Note: this Pico arrived blank (in BOOTSEL mode). It was flashed with MicroPython
v1.29.0 for the Pico 2 W, the same build as the other Picos.

## Driving

```sh
PORT=/dev/ttyACM0 tools/deploy.sh             # motors.py, drive.py, main.py
PORT=/dev/ttyACM0 python3 tools/drive.py      # keyboard driving
```

In `tools/drive.py`:
- Arrows or WASD set the motion (forward, reverse, spin left, spin right).
- Space stops. `+` and `-` change the speed in 0.1 steps (40% to start). `q` quits.
- A key sets what the robot is doing until you press another; you don't hold
  keys down. Terminal key repeat starts too late for the dead-man stop.
- It needs a real terminal (curses).
- **On the Pi (2026-10-15):** the motor Pico is plugged into the Pi, and
  driving over SSH works. Use the stable device name, since the
  `/dev/ttyACMx` numbers can shuffle once all three Picos are plugged in:
  `PORT=/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_ada3893d793b58fd-if00 python3 tools/drive.py`

### Protocol (`pico/drive.py`)

Start it from the REPL with `import drive; drive.run()`. It prints `READY`, then
takes plain-text lines, so the same commands work over USB serial, BLE or WiFi:

| Command | Meaning |
|---|---|
| `T <left> <right>` | drive each side, -1.0 to 1.0 (left = A, C; right = B, D) |
| `M <A-D> <speed>` | one motor, for testing |
| `S` | stop now (coast) |
| `?` | reply `SPD <a> <b> <c> <d>`, the actual (ramped) speeds |
| `q` | stop and return to the REPL |

- **Replies:** `T` and `M` are silent, and a bad line gets `ERR <line>`.
- **Dead-man:** with no `T`, `M` or `S` for 0.5 s, everything coasts and the
  Pico prints `DEADMAN`. A host holds motion by resending its command about
  10 times a second.
- **Ramping:** speed changes are limited to full scale per 0.5 s, so starts
  and reversals don't spike the power bank. Stops are never ramped: they
  coast immediately.
- **Watchdog:** once `drive.run()` has run, a soft reset (Ctrl-D) makes the
  Pico hard-reboot about a second later. That's expected and harmless:
  `main.py` stops the motors at boot.

Tested 2026-10-15 on the lift: forward, spin, the dead-man stop (tripped
0.42 s after the host went quiet) and keyboard driving all worked.
