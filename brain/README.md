# Patti: Brain

One process on the Pi 4. It owns the Picos and serves the web control page.
So far it handles **driving** (the motor Pico), the **live camera**, and
**aiming the camera** (the gimbal Pico). The face and audio come later.

## Running it

```sh
python3 ~/robotpattinson/brain/server.py --set-password   # once; it prompts for the password
python3 ~/robotpattinson/brain/server.py                  # Ctrl-C stops it (and the motors)
```

Then open **http://rpattinson:8080/** (or http://100.64.0.20:8080/) on any
device on your Tailscale, and log in.

### At boot

```sh
sudo ~/robotpattinson/brain/systemd/install.sh   # once: installs and starts rp-brain.service
```

After that it starts by itself at every boot, and restarts if it crashes.
If the motor Pico isn't plugged in yet, the page comes up with driving greyed
out, and it connects within a few seconds of the Pico appearing.

```sh
sudo systemctl stop rp-brain       # free the camera and motor Pico (track.py, drive.py)
sudo systemctl start rp-brain      # back on
sudo systemctl disable rp-brain    # stop starting it at boot
journalctl -u rp-brain -f          # its log
```

**Needs:** `sudo apt install python3-aiohttp python3-opencv python3-serial`.
All three are on the Pi already.

| Option | Default | |
|---|---|---|
| `--size` | 640x480 | camera size. `320x240` uses about a quarter of the bandwidth, for a mobile upload |
| `--quality` | 50 | JPEG quality, 1-100 |
| `--fps` | 15 | asks the camera for this. In dim light it gives less (about 7) |
| `--motors` | the motor Pico's `/dev/serial/by-id/...` name | its serial port |
| `--gimbal` | the gimbal Pico's `/dev/serial/by-id/...` name | its serial port |
| `--camera` | /dev/video0 | the UGREEN camera |
| `--rec-size` | 1280x720 | camera size while recording (up to 1920x1080) |
| `--rec-dir` | ~/Videos/robotpattinson | where recordings go |
| `--audio` | the UGREEN mic (ALSA) | `--audio ""` records without sound |
| `--bind` / `--port` | Tailscale IP / 8080 | where it listens (and on 127.0.0.1, for `say` and `ask`) |

### From a terminal on the Pi

```sh
say "Hello, Josh"                  # she says it
ask "What's the weather like?"     # one of her voice commands, no name needed
```

`ask` prints what she says back, and suggests the nearest commands if it
isn't one of hers. Set up once:

```sh
ln -s ~/robotpattinson/brain/talk.py ~/.local/bin/say
ln -s ~/robotpattinson/brain/talk.py ~/.local/bin/ask
```

## The page

- **Video** fills the top of the page.
- **Driving:** drag the left (orange) pad, or hold WASD or the arrow keys.
  Letting go stops. Space or the red **STOP** button stops too. The slider
  sets top speed.
- **TABLE MODE** (the amber button) blocks all driving, for when she's on a
  desk or being worked on. The drive pad greys out, and the server itself
  refuses drive commands, so another open page can't drive either.
  - **It's remembered:** it stays on through server restarts and reboots
    (the file `~/.config/robotpattinson/table-mode`), until you switch it off.
    Switching it off asks first.
  - **Not covered:** `motors/tools/drive.py` over SSH talks to the Pico
    directly and still drives.
  - The camera, gimbal and recording work as normal.
- **Camera:** drag the right (blue) pad, or hold IJKL, to pan and tilt.
  - **Speed:** it moves while you hold the pad, faster the further you push,
    up to 90 degrees a second.
  - **Letting go:** the camera stays where you left it.
  - **centre** (or C) points it straight ahead.
  - **relax** stops the servo pulses, so they stop buzzing or drawing power;
    the next move picks them up again.
- **Recording:** the **record** button (or R) records what the camera sees,
  with sound from its mic, to the Pi's own storage.
  - **Keeps going without you:** it carries on if you close the page or lose
    the connection. Press the button again (it shows the running time) to stop.
  - **Getting the files:** **Recordings** at the bottom lists them to download
    or delete.
  - **Format:** 1280x720, 15 fps H.264/AAC MP4, about 1.5 GB an hour. It uses
    about one of the Pi's four cores.
    - **Encoder:** software (x264). The Pi's hardware encoder hung or wrote
      broken files.
    - **When stopping:** it takes a few seconds to tidy the file into a plain
      MP4 that phones and VLC play.
  - **Crash-safe:** a power cut loses only the last second or so. A file
    that was cut off can be tidied by hand:
    `ffmpeg -i in.mp4 -c copy -movflags +faststart out.mp4`.
  - **The live view** stays at its usual size while recording.
  - **Where files go:** `~/Videos/robotpattinson/`. `--rec-dir` points
    elsewhere, such as a USB drive. It won't start with under 1 GB free, and
    it stops at 250 MB free.
- **Intercom** (intercom.py), under the video:
  - **listen** (or H) plays her mic on your device, about a quarter of a
    second behind. It shares the mic, so voice commands and recording carry on.
  - **hold to talk** (or hold T) plays your voice on her speaker. Her ears are
    off while you talk, so your voice can't wake her, and music turns down.
    What she hears is muted on your end while you talk, like a walkie-talkie.
  - **Needs HTTPS:** browsers only give a page the mic over HTTPS, so talking
    works from `https://rpattinson.<tailnet>.ts.net/` (see Access), not the
    plain address. The page links there when it's needed. Listening works on
    either.
  - **The room can tell:** her Sense HAT shows an orange ear while anyone
    listens and a blue speaker while anyone talks, over anything else it shows.
- **Say:** type in the box and press Enter, and she says it in her own voice
  (the same as `say` on the Pi). Works on either address.
- **Status dots:** link, motors, camera, and gimbal (with its pan and tilt).
- **Stops by itself** when the tab loses focus or the phone locks.

## Safety

Each layer catches what the one above misses:

1. **The page** sends drive commands only while a control is held, about 10
   times a second.
2. **The server** stops the motors if those commands go quiet for 0.3 s, or if
   the page's connection closes.
3. **The motor Pico** stops by itself 0.5 s after its last command (its
   dead-man stop, see motors/README.md). So the robot stops even if the server
   dies.

**The gimbal at startup:** if the gimbal Pico is already running its control
loop (after a server restart), the server picks up wherever it's pointing.
After a power-up it starts the loop, which snaps the camera to centre once.
When the server stops, the camera keeps holding its last position.

## Access

- **Network:** it listens only on the Tailscale address, so only your devices
  can reach it.
- **Password:** every page needs it. The password is in
  `~/.config/robotpattinson/web-password` (mode 600).
- **Login cookie:** lasts 30 days. Changing the password logs everyone out.
- **HTTPS:** Tailscale puts the page at `https://rpattinson.<tailnet>.ts.net/`
  with a real certificate, tailnet-only like the plain address. Set up once,
  it survives reboots:
  `sudo tailscale serve --bg http://$(tailscale ip -4):8080`
  (pointed at the Tailscale address, not 127.0.0.1, so nothing relayed counts
  as "from the Pi itself" for `/say` and `/ask`). The first time, the tailnet's
  admin has to allow Serve and HTTPS certificates.
- **Not public:** to let someone else in, share the `rpattinson` machine with
  their Tailscale account. Exposing it to the internet (Tailscale Funnel) would
  need hardening first, because then the password alone guards the motors.

## Video lag

The camera stream is MJPEG, a series of JPEGs.

- **The server never queues frames.** It always sends the newest one, and the
  video socket's send buffer is kept at about 2 frames.
- **Over a phone hotspot there's still some lag.** On 2026-10-15 the laptop was
  on cafe wifi and saw about 4 s, because the phone's mobile upload buffers the
  stream.
- **On the same network it's close to live:** both devices on the hotspot, or
  on home wifi.
- **Lower bitrate helps:** `--size 320x240` when you're stuck going over mobile.
- **Proper fix:** WebRTC with the Pi's H.264 encoder, which adapts to the link.
  It's planned along with audio.

Only one program can use the camera, the motor Pico and the gimbal Pico at a
time, so stop `vision/host/track.py`, `vision/tools/keys.py` and
`motors/tools/drive.py` before starting this, and
stop the service (`sudo systemctl stop rp-brain`) before running those.
