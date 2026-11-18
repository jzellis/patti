# Patti: Vision

The camera gimbal. Two MG996R servos (pan + tilt) on a Kitronik Simply
Servos board (KIT5339), driven by a Raspberry Pi Pico 2 W running MicroPython.

Status: bring-up done. The driver works, both axes are calibrated, and the
smoke test passes. There's no firmware or host protocol yet: it's driven from
the REPL.

## Layout

```
vision/
  pico/
    gimbal.py      Servo + Gimbal classes (hardware PWM, clamped, slew-limited)
    smoketest.py   bring-up test, run this first
    keyctl.py      serial control loop (driven by tools/keys.py, host/track.py)
  host/            runs on the computer the camera is plugged into
    picolink.py    serial helpers shared by keys.py and track.py
    track.py       face tracking test (OpenCV YuNet); --who adds names
    faces.py       face recognition (OpenCV SFace) shared by who.py and track.py
    who.py         enroll people, list/forget them, watch names live
    models/        YuNet (committed) + SFace (tools/get_models.sh), from opencv_zoo
  tools/
    deploy.sh      mpremote deploy
    keys.py        arrow-key control from a terminal
    get_models.sh  downloads the models too big for git
```

## Hardware

### Board (Kitronik datasheet v1.0)

| | |
|---|---|
| Input | 3-12V on the screw terminal, through the on/off switch, reverse-polarity protected |
| Servo V+ | **the raw input voltage, passed straight through** |
| Pico power | on-board 3V regulator feeds the Pico via the 40-way connector |
| Max current | 10A continuous, all channels |
| 3V3 breakout | 50mA max |
| Servo channels | GP2-GP9 (datasheet: header 1-8 in order; see Wiring) |
| Spare breakouts | GP0, GP1, GP26, GP27, GP28, 3V3, GND |

### Wiring

| Axis | Board header label | GPIO |
|---|---|---|
| Pan  | 2 | GP2 |
| Tilt | 1 | GP3 |

**The header labels don't match the datasheet.** Kitronik says header 1 is
GP2, but on our board the header labelled 1 is driven by GP3 and the one
labelled 2 by GP2. We confirmed this by driving each pin on its own and
watching which servo moved. So the code names servos by GPIO, never by
header number. To use other channels, pass the GPIOs:
`Gimbal(pan=Servo(4), tilt=Servo(5))`. Before relying on any other header,
check it the same way.

### Power

- **Supply at or below 7.2V.** That is the MG996R maximum (rated 4.8-7.2V),
  and the board does not regulate servo power. 6V is the sweet spot.
- Budget about **2.5A per servo at stall**, so a supply good for 5A or more.
  Starting both servos at once (the snap to centre) is the worst moment.
- The datasheet doesn't say whether it's safe to have USB plugged in while
  the terminal block is powered. For flashing and copying files, switch the
  board off and let USB power the Pico.

## Stopping the servos

MG996Rs are often digital servos: they latch the last pulse they saw and keep
driving to it. A pulse that gets cut short (a "runt") reads as an extreme
position, so stopping the PWM mid-pulse sends the servo hard to one end and
holds it there. We saw exactly that on the first bring-up.

`Servo.detach()` therefore sets duty to 0 (which only takes effect when the
current pulse has finished), waits one frame, then hands the pin back to
GPIO low. It never calls `PWM.deinit()`: GP2 and GP3 share PWM slice 1, so
stopping the slice for one servo would freeze the other one's line wherever
it happened to be, possibly mid-pulse.

After detach, a servo may hold its last position or go slack; that depends on
the individual servo, not on us.

## MG996R pulse range

- 50Hz frame (20ms period).
- The datasheet range is **1000-2000us, centre 1500us**. It claims ~180 deg
  over that range, but real units vary.
- Many units travel further, roughly 500-2500us. Kitronik's library uses
  500-2500 as its full range. Pushing past a servo's end stop makes it stall
  at about 2.5A and heat up.

`gimbal.py` clamps each servo to **1000-2000us** by default. Widen that per
servo only after you've measured it (see Calibration).

## Why not Kitronik's SimplyServos.py

Kitronik's library
(<https://github.com/KitronikLtd/Kitronik-Pico-Simply-Servos-MicroPython>, MIT)
drives servos from PIO state machines 0-7. Two things about it make it a poor
fit here:

- It sets all 8 headers to 1500us as soon as it's constructed.
- It can clash with the Pico W's wireless chip over PIO. Kitronik's own README
  mentions this.

We only need two channels, so `gimbal.py` uses `machine.PWM` instead and
leaves PIO free for the radio. The pins and pulse maths are the same as
Kitronik's.

## Bring-up

1. Flash MicroPython onto the Pico 2 W and seat it on the board.
2. Plug in the servos: pan on the header labelled 2 (GP2), tilt on the one
   labelled 1 (GP3). The signal wire
   (orange/yellow) goes on the pin marked S.
3. Set the bench supply to 6V with a current limit of about 3A, then turn the
   board on.
4. Centre the gimbal by hand, and make sure there's room for about +/-20 deg
   of movement on each axis.
5. Run:

```sh
pip install --user mpremote
tools/deploy.sh --smoke
```

You should see:

1. Both servos snap to centre.
2. Pan swings left, then right, then back to centre.
3. Tilt does the same.
4. Both servos stop receiving pulses. Neither should move at this point:
   a jump here means a pulse was cut short (see "Stopping the servos").

The board's power switch is your e-stop.

If nothing moves, check these in order:

- Is the board switch on? The green LED should be lit.
- Are the servo plugs the right way round?
- Is the supply actually delivering current? A supply sitting at its current
  limit holds the voltage down and the servos jitter.

## Keyboard control

```sh
tools/deploy.sh          # once, to get keyctl.py onto the Pico
tools/keys.py            # PORT=/dev/ttyACM1 tools/keys.py to override
```

| Key | Does |
|---|---|
| Arrow keys | pan / tilt, 3 deg per press; hold to keep moving |
| `c` | back to centre |
| `x` | release (stop pulses); the next arrow re-attaches |
| `q` or Ctrl-C | quit: servos released, Pico back at the REPL |

- **Start-up:** the first thing it does is centre, and that move is a snap,
  as always from an unknown position.
- **Limits:** the calibrated limits apply, so holding a key against an end
  just stops there.
- **Serial port:** it needs the port to itself, so close any `mpremote`
  sessions first. If a previous run died without quitting, the next run
  interrupts it cleanly. The servos detach before restarting.
- **How it works:** `keys.py` puts your terminal in raw mode and sends one
  letter per key (`L R U D c x q`) to `keyctl.run()` on the Pico. That loop
  moves both axes toward their targets one PWM frame at a time, easing in
  and out: it accelerates at about 270 deg/s^2 up to about 80 deg/s, and
  brakes so it stops exactly on the target without overshooting. The target
  can change mid-move. `P` asks where the servos actually are (`AT pan tilt`).

## Face tracking (test)

Runs on the laptop for now. The camera is the UGREEN USB webcam on the gimbal
(`/dev/video4`).

```sh
python3 -m venv host/.venv && host/.venv/bin/pip install opencv-python pyserial numpy pillow
tools/deploy.sh                                  # keyctl.py with the A and P commands
host/.venv/bin/python host/track.py --dry-run    # camera + detection only
host/.venv/bin/python host/track.py
```

On the Pi (no screen), stream it instead and watch in a browser:

```sh
python3 host/track.py --stream 8080        # http://rpattinson:8080/ over Tailscale
```

`--stream` binds to the Tailscale address by default, so only your own devices
can reach it. There is no password yet. Ctrl-C quits.

In the preview window: `q` quits, `c` centres, space pauses tracking, and `f`
cycles the on-screen text through fonts (Hershey, plus any of a list of
installed fonts drawn with Pillow; `--font NAME` picks the starting one,
`--font-size` its size, default 16: keep pixel fonts to multiples of 8).

`--overlay "CMD"` draws a shell command's output in the bottom-left corner,
rerun every `--overlay-every` seconds (default 2) on a background thread, so
a slow command never holds up tracking. For example:
`--overlay "uptime; free -h | head -2; ip -br a"`.
The window opens at 2x the camera picture (`--window-scale`) and can be
resized, tiled or fullscreened. The picture is always scaled by the largest
whole number that fits, each pixel an exact block, and centred on black:
scaling by anything else makes pixel fonts lumpy.
Ctrl-C in the terminal quits just as cleanly, recording included.

**Recording.** `--record ~/Videos/track.mp4` saves the session as an
H.264/AAC MP4 with sound. Notes:

- **What's recorded:** it records what the preview shows, overlay included.
  Use `--record-clean` for the plain camera picture.
- **Sound:** it comes from the gimbal camera's own mic (UGREEN) when that's
  plugged in, otherwise from the default mic. Use `--audio-source <name>` to
  pick another (names from `pactl list sources short`), or `--no-audio` for
  silent video.
- **How:** OpenCV can't do audio, so frames are piped to ffmpeg, which
  records the mic itself. Both are stamped with wall-clock time, so they stay
  in sync even though the camera's frame rate wanders.
- **Cheese:** it can't record the gimbal camera at the same time, because
  only one program can stream a webcam. Point Cheese at the laptop's own
  webcam to film the rig from outside.

**How it moves.** It tracks continuously and the Pico eases every move (see
Keyboard control), so there are no hops. It picks the biggest face and works
out how far it is from the centre in degrees, using the field of view.

- **Where the face is:** frames lag behind the gimbal, so the offset in a frame
  is added to where the gimbal *was* when that frame was taken (now minus
  `--latency`), not where it is now. The tracker asks the Pico where the
  servos are on every frame. That gives the face's direction in gimbal angles,
  which doesn't depend on how the camera was moving. Correcting off stale
  frames without this is what makes a tracker overshoot and oscillate.
- **Smoothing:** that direction is smoothed (`--smooth`, 0-1) so detection
  jitter doesn't become servo twitch, then it becomes the new target.
- **Resting:** the gimbal holds still while you're within `--deadband` degrees
  of where it's aimed. Once you leave that, it follows until you're centred.

Simulated offline (2026-10-12, latency 0.12-0.16s, 17fps): a 25 deg step is
reached in about 0.5s with under 1.5 deg of overshoot, and nothing moves at
rest.

**Tuning:**

| Symptom | Change |
|---|---|
| Overshoots when you move, then comes back | raise `--latency` (default 0.16) or lower `--hfov` |
| Lags behind, or undershoots and creeps | lower `--latency`, or raise `--hfov` |
| Shaky while following | lower `--smooth` (default 0.35) |
| Smooth but sluggish | raise `--smooth` |
| Pans away from you | add `--mirror` |
| Twitchy near the middle | raise `--deadband` (default 3) |

## Face recognition

`host/who.py` teaches her faces. `track.py --who` then puts a name over each
face it tracks. Nothing is trained:

- **Fingerprints:** SFace (from opencv_zoo) turns a face into a 128-number
  fingerprint.
- **Enrolling:** saves crops of someone's face.
- **Recognizing:** compares a face's fingerprint with everyone's crops. Under
  about 0.36 similarity it's a stranger (`?`). If two people score within 0.05
  of each other, it's also `?`, rather than a guess.

```sh
vision/tools/get_models.sh                      # once per machine: SFace is 38 MB, so not in git
python3 host/who.py enroll Josh --stream 8081   # look at the camera, keep moving your head
python3 host/who.py enroll Josh --photos ~/Pictures/josh/
python3 host/who.py watch --stream 8081         # names over faces, live (gimbal untouched)
python3 host/who.py list
python3 host/who.py forget Josh
python3 host/track.py --camera /dev/video0 --who --stream 8081
```

- **Enrolling from the camera:** it takes 20 crops (`--count`), one every
  0.7 s.
  - It skips crops too much like ones it already has, so keep moving: turn and
    tilt your head, glasses on and off, nearer and farther.
  - It only enrols when exactly one face is in view.
  - Running it again adds more crops. That's the fix if someone gets `?` in
    some lighting.
- **Enrolling from photos:** `--photos` uses the biggest face in each photo.
- **Where faces are kept:** `~/.local/share/robotpattinson/faces/<name>/`, out
  of the repo. Use `--faces` or `RP_FACES` for somewhere else. Copy the folder
  between machines to share who she knows.
- **Speed on the Pi 4:** about 150 ms per face, so faces are re-checked once a
  second (`watch --check-every`). In between, a face keeps the name from the
  last check.
- **Stop the brain first** (`sudo systemctl stop rp-brain`), because it holds
  the camera.

Cats come next. YuNet only finds human faces, so they'll need their own
detector.

## Calibration

Pan calibrated on 2026-10-08, tilt again on 2026-10-31 (with the head on, after
the gimbal was rebuilt level). The numbers live in `PAN_LIMITS` /
`TILT_LIMITS` in `pico/gimbal.py`, and the brain and track.py keep a copy in
degrees (`TILT_LIM`).

| Axis | Range | Centre | Notes |
|---|---|---|---|
| Pan  | 500-2500us, about +/-90 deg | 1500 | nothing in the way |
| Tilt | 1289-1944us, -19 to +40 deg | 1500 | the tilt servo hits the gimbal at -23 deg; the head nearly touches at +45 |

- **Angles:** +pan is the camera's right and +tilt is up, so `g.look(30, 10)`
  means "30 deg right, 10 deg up".
- **Pulse direction:** pan is mounted so that a *lower* pulse moves it the +
  way (`invert=True`); since the rebuild, tilt goes up with a *higher* pulse
  (`invert=False`).
- **Degrees per pulse:** `US_PER_DEG` is about 11.1 (2000us per 180 deg).
  This checked out on both servos: 1000us from centre is about 90 deg.
- **Tilt's limits:** found with pan centred, in small steps watched by eye:
  contact at -23 deg down (the tilt servo against the gimbal) and nearly
  touching at +45 up, so -19 and +40 leave a few degrees of margin.

### Redoing it

Jog from the REPL. Use `glide_us`, not `write_us`: `write_us` jumps at full
speed.

```sh
tools/deploy.sh --repl
```
```python
from gimbal import *
g = Gimbal()
g.center()
g.pan.min_us, g.pan.max_us = 500, 2500   # widen for exploring; clamps still apply
g.glide_us(1100, 1500)                   # (pan_us, tilt_us); step 100us at a time
g.detach()
```

When you find a limit, back off 50us from where the gimbal touches something
or the servo buzzes.

### Don't reset the Pico while servos are attached

A plain `mpremote exec` or `run` soft-resets the Pico first. A soft reset
stops the PWM wherever it is, which can produce a cut-short pulse (see
"Stopping the servos"). That's harmless after `detach()`, which leaves the
pins idle low. Mid-session, while servos are attached, use
`mpremote resume exec ...` instead. It also keeps your REPL variables, so
`g` survives between commands.
