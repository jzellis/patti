#!/usr/bin/env python3
"""Patti's brain: one process on the Pi that owns the Picos and
serves the web control page.

    python3 brain/server.py --set-password     # once
    python3 brain/server.py                    # http://rpattinson:8080/

So far: drive (motor Pico, through motors/pico/drive.py), the live camera,
aiming it (gimbal Pico, through vision/pico/keyctl.py), recording, and voice
commands (voice.py), and a status screen on her chest (cyd.py). The face
comes later.

Access: it binds to the Pi's Tailscale address by default, so only your own
devices can reach it, and every page needs the password (kept in
~/.config/robotpattinson/web-password, set with --set-password). Logging in
sets a cookie that lasts 30 days and stops working if the password changes.

Safety, three layers deep:
  * the page sends drive commands only while a control is held, ~10x/s;
  * this server stops the motors if those go quiet for STALE_S, or the
    page's connection closes;
  * the motor Pico stops by itself 0.5s after its last command (dead-man),
    so even if this process dies the robot stops.

Table mode (the page's TABLE MODE button) refuses every drive command, for
when she's on a desk or being worked on. It's remembered across restarts and
reboots (TABLE_FILE), so she stays put until it's switched off. It only
covers this server: motors/tools/drive.py over SSH still drives.

`say` and `ask` in a terminal on the Pi (brain/talk.py) post to /say and
/ask on 127.0.0.1, which it also listens on; those two (and /eyes, to point
her pupils) take no login, but only from the Pi itself.

The camera (MJPEG at /video.mjpg) only runs while someone is watching, so
vision/host/track.py can have it the rest of the time (not both at once).
"""

import argparse
import asyncio
import collections
import concurrent.futures
import difflib
import getpass
import hashlib
import hmac
import html
import json
import os
import queue
import random
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
import serial
from aiohttp import WSMsgType, web

# One core for OpenCV's own work (mostly the face detector). With its default
# of all four, following a face while recording starved ffmpeg: choppy video.
cv2.setNumThreads(1)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "motors", "tools"))
import drive as drivetool  # noqa: E402  (motors/tools/drive.py: start())
sys.path.insert(0, os.path.join(HERE, "..", "vision", "host"))
import picolink  # noqa: E402  (vision/host/picolink.py: start() for keyctl)
import voice as voicecmds  # noqa: E402  (brain/voice.py: typed commands for /ask)
from voice import Voice  # noqa: E402  (brain/voice.py)
import hat  # noqa: E402  (brain/hat.py: the Sense HAT's idle emojis)
import cyd  # noqa: E402  (brain/cyd.py: the chest screen)
import chest  # noqa: E402  (brain/chest.py: the chest screen's screens, joystick and voice mode)
import weather  # noqa: E402  (brain/weather.py: Open-Meteo)
import places  # noqa: E402  (brain/places.py: where she is, by wifi)
import outputs  # noqa: E402  (brain/outputs.py: friday's speakers at home only)
import memory  # noqa: E402  (brain/memory.py: who, when, where; right now)
import strangers  # noqa: E402  (brain/strangers.py: faces she doesn't know, unnamed)
import users  # noqa: E402  (brain/users.py: logins and what each may do)
import claude  # noqa: E402  (brain/claude.py: questions for Claude, on friday)
import wiki  # noqa: E402  (brain/wiki.py: Wikipedia, for who and what questions)
import replies  # noqa: E402  (brain/replies.py: what she says)
import limbic  # noqa: E402  (brain/limbic.py: her feelings)
import drives  # noqa: E402  (brain/drives.py: what she does of her own accord, within laws.py)
import macropad  # noqa: E402  (brain/macropad.py: the 12-key, 3-knob pad)
import news  # noqa: E402  (brain/news.py: the feeds Josh reads)
import telegram  # noqa: E402  (brain/telegram.py: messages, when they're not in front of her)
import music  # noqa: E402  (brain/music.py: what's playing through her, AirPlay or DLNA)
import timers  # noqa: E402  (brain/timers.py: her kitchen timer)
import barometer  # noqa: E402  (brain/barometer.py: storm watch, from the Sense HAT's pressure sensor)
import intercom  # noqa: E402  (brain/intercom.py: listen and talk through her from the page)
import voices  # noqa: E402  (brain/voices.py: who's talking, from their voice)
import meet  # noqa: E402  (brain/meet.py: learning new people by talking with them)
from speech import Speech  # noqa: E402  (brain/speech.py: her voice, Piper)
import faces  # noqa: E402  (vision/host/faces.py: the YuNet face detector)
sys.path.insert(0, os.path.join(HERE, "..", "eyes"))

CONF = os.path.expanduser("~/.config/robotpattinson")
PASSWORD_FILE = os.path.join(CONF, "web-password")
TABLE_FILE = os.path.join(CONF, "table-mode")   # exists = table mode on
COOKIE = "rp"
STALE_S = 0.3
RECONNECT_S = 3      # retry an offline motor Pico this often
SNDBUF = 96 * 1024     # video socket send buffer, ~2 frames (see video())
MOTOR_PORT = "/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_ada3893d793b58fd-if00"
GIMBAL_PORT = "/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_879d46c3e67e5578-if00"
REC_DIR = os.path.expanduser("~/Videos/robotpattinson")
AUDIO_CARD = "/proc/asound/Camera"          # the UGREEN camera's mic
MIC = "rpmic"       # that mic, shareable (systemd/50-rpmic.conf): voice + recording
MIN_FREE = 1_000_000_000    # don't start recording with less than 1 GB free
LOOK_DEG_S = 90        # gimbal speed with the camera pad pushed all the way
# Must match vision/pico/gimbal.py PAN_LIMITS / TILT_LIMITS (keyctl clamps too).
PAN_LIM = (-90.0, 90.0)
TILT_LIM = (-19.0, 40.0)
# Where "centre" puts her, (pan, tilt): she sits low, so a little up is ahead.
HOME = (0.0, 10.0)
# Where the head was last aimed, kept (at most every POSE_SAVE_S, once it's
# changed) so the gimbal can start from there: no snap to centre when the
# Pico starts, which draws a burst that can brown the Pi out.
POSE_FILE = os.path.join(CONF, "gimbal-pose.json")
POSE_SAVE_S = 5
# Fresh from a boot, the servos wait (no pulses: her eyes work meanwhile)
# till she's settled: up SETTLE_UP_S and her load (1-minute average) under
# SETTLE_LOAD, or up SETTLE_MAX_S whatever. So their first pulses don't land
# while she's busiest. (A restart of the brain alone: at once.)
SETTLE_UP_S = 45
SETTLE_LOAD = 2.0
SETTLE_MAX_S = 120
# "Look at me": the camera's horizontal field of view (a guess, as
# vision/host/track.py --hfov), where to look while searching, and how long a
# move takes to settle before the picture can be trusted.
HFOV = 65.0
EYE_RANGE_DEG = 40.0    # a face this far off puts the pupils at the edge of the eyeball
SEEK_PANS = (-75, -45, -15, 15, 45, 75)
SEEK_TILTS = (15.0, 35.0)   # two rows: she's low down, so faces are often well above
SETTLE_S = 0.8
# "Follow me", like people do it: the eyes follow the face anywhere in the
# middle FOCUS of the picture and the head stays put. When the face leaves
# that zone the eyes go first and the head follows LEAD_S later, moving
# HEAD_GAIN of the way per picture until the face is back within RECENTRED_DEG
# of the middle. Sweep after LOST_S without a face (then at most every
# SWEEP_EVERY_S), give up after GIVE_UP_S.
FOCUS = 0.7
LEAD_S = 0.2
HEAD_GAIN = 0.6
RECENTRED_DEG = 3.0
# Bored after BORED_S with nobody doing anything (lids droop), asleep after
# SLEEP_S (eyes shut). The page, voice commands and any loud sound wake her;
# following and recording keep her awake.
BORED_S = 120
SLEEP_S = 600
FOLLOW_EVERY_S = 0.16   # look for the face ~6x a second (~120 ms each, one core)
LOST_S = 2.0
SWEEP_EVERY_S = 10.0
GIVE_UP_S = 60.0
# Curiosity, when nobody's asked for anything else (curiosity()): she looks
# at whatever moves in front of her, and makes eye contact with faces, which
# win over movement. Eyes first; her head turns only when what she's looking
# at stays out of the middle FOCUS of the picture (her peripheral vision) for
# LEAD_S, as in "follow me". Movement is the change from one picture to the
# next in a small grey copy (MOTION_SIZE); between MOTION_MIN and MOTION_MAX
# of it changing counts (less is noise, more is the light or the camera
# moving). Pictures taken while her head moves, or CURIOUS_SETTLE_S after,
# are ignored (by when they were taken: the camera lags, and judging from a
# picture taken mid-move made her overshoot, again and again: twitching), and
# her head moves at most every HEAD_EVERY_S. Faces: every FACE_FAST_S while something's been going on in the
# last BUSY_S, else every FACE_SLOW_S; one stays the target FACE_HOLD_S after
# it was last seen. Like someone sitting watching: for movement her head goes
# at most MOTION_LEASH_DEG from HOME, and if it would have to go further she
# lets it go and looks ahead again; a face she'll turn as far as she can for.
# Nothing to look at for MOTION_HOME_S after movement, or HOME_AFTER_S after
# a face: head back to HOME. A face keeps her awake. Movement wakes her too
# (eyes and chest screen, as a loud sound does): asleep or drowsy, she keeps
# half an eye open (a picture every WAKE_EVERY_S, nothing else), and
# something big enough moving (WAKE_MOTION of the picture, WAKE_HITS
# pictures running) wakes her. Steered by hand (the page,
# "look left", "stop"): leave her head be for HAND_HOLD_S. WATCH_OFF_FILE
# exists: switched off ("stop watching").
#
# Eye contact is always on, curiosity or not: whenever she's awake she looks
# at a face in view with her eyes alone (no movement, no head). Curiosity adds
# following movement and turning her head. CONTACT_OFF_FILE existing turns eye
# contact off too (it frees the camera, e.g. for who.py enroll).
CURIOUS_EVERY_S = 0.12
MOTION_SIZE = (160, 120)
MOTION_LEVEL = 20
MOTION_MIN = 0.004
MOTION_MAX = 0.3
WAKE_EVERY_S = 0.5
WAKE_MOTION = 0.02          # (a cat across the room is less; someone walking up is more)
WAKE_HITS = 2
CURIOUS_SETTLE_S = 1.0
HEAD_EVERY_S = 1.2
FACE_FAST_S = 0.3
FACE_SLOW_S = 1.5
FACE_HOLD_S = 0.8
BUSY_S = 2.0
HOME_AFTER_S = 10.0
MOTION_HOME_S = 2.0
MOTION_LEASH_DEG = 30.0
HAND_HOLD_S = 30.0
WATCH_OFF_FILE = os.path.join(CONF, "watch-off")
CONTACT_OFF_FILE = os.path.join(CONF, "eye-contact-off")
# Who's that (vision/host/faces.py, people enrolled with who.py): the face
# she's looking at is checked every RECOG_EVERY_S. Someone she knows, seen
# GREET_SEEN times within GREET_WITHIN_S (one odd match won't do it), gets
# greeted the first time each day; her day starts at DAY_START_H, so a late
# night isn't a new day. Who she's greeted is kept in GREETED_FILE, so a
# restart doesn't greet everyone again. Someone she knows counts as around
# for PRESENT_S after they were last seen. New faces enrolled: restart her.
RECOG_EVERY_S = 2.0
GREET_SEEN = 2
GREET_WITHIN_S = 10.0
DAY_START_H = 4
PRESENT_S = 300.0
GREETED_FILE = os.path.join(CONF, "greeted.json")
# Who said a command, from their voice (voices.py, enrolled with voices.py
# enroll): from at least SPEAKER_MIN_S of speech; "hello" waits up to
# SPEAKER_WAIT_S to answer by name. Hearing someone counts as seeing them for
# who's around (PRESENT_S).
SPEAKER_MIN_S = 0.6                 # ("Patti, hello" is under a second)
SPEAKER_DB = 8.0                    # louder than the room by this counts as speech (softer than voices.SPEECH_DB: word endings)
SPEAKER_WAIT_S = 2.0
# Face and voice together: someone whose face she's recognised in the last
# FACE_HELP_S needs only VOICE_WITH_FACE (not voices.MATCH) from their voice,
# as long as theirs is still the best voice match. The face never overrules
# the voice: if another voice matches better, that's who spoke.
FACE_HELP_S = 10.0
VOICE_WITH_FACE = 0.2
# Meeting someone (meet.py). After a command, she offers to remember whoever
# said it when their voice is like nobody she knows (below STRANGER_VOICE)
# and, in the last STRANGER_SEEN_S, she's seen a face like nobody she knows
# (below STRANGER_FACE; so not a voice on the telly), or the face of someone
# she knows whose voice she hasn't got (then just the voice). Not again for
# ASK_AGAIN_S after asking, whatever the answer.
STRANGER_VOICE = 0.2
# Mid-conversation, a voice she can't be sure of is taken as the person she's
# talking with if it's at least this like theirs (and nobody else she knows
# is in view). Strangers score up to about 0.22 against the household.
CONVO_VOICE = 0.3
STRANGER_FACE = 0.25
STRANGER_SEEN_S = 5.0
# Someone she doesn't know, seen at home, is remembered without a name
# (strangers.py) until she's told who they are: the one she's seen in the
# last INTRO_S is who "this is my friend" or "who's that?" means.
INTRO_S = 15.0
ASK_AGAIN_S = 6 * 3600


# ---------------------------------------------------------------- motors

class Motors:
    """The motor Pico running drive.run(). Without it the page still works,
    just with driving greyed out."""

    def __init__(self, port):
        self.port = port
        self.ser = None
        self.said = ""
        self.last_drive = 0.0
        self.moving = False
        self.lock = threading.Lock()
        self.table = os.path.exists(TABLE_FILE)
        self.connect()

    def connect(self):
        """Open the Pico and start drive.run(). Called again by motor_watch
        while it's offline, so a Pico that turns up late (at boot) or gets
        replugged comes back by itself."""
        try:
            ser = serial.Serial(self.port, 115200, timeout=0)
        except serial.SerialException as e:
            print("motors: offline (%s)" % e, file=sys.stderr)
            return
        try:
            drivetool.start(ser)
        except (serial.SerialException, SystemExit) as e:
            print("motors: offline (%s)" % e, file=sys.stderr)
            ser.close()
            return
        print("motors: online", file=sys.stderr)
        self.ser = ser

    @property
    def ok(self):
        return self.ser is not None

    def _write(self, line):
        if not self.ser:
            return
        with self.lock:
            try:
                self.ser.write(line)
            except serial.SerialException as e:
                print("motors: write failed (%s)" % e, file=sys.stderr)
                self.ser = None

    def set_table(self, on):
        """Table mode: no driving at all, and remembered (see TABLE_FILE)."""
        self.stop()
        self.table = bool(on)
        if self.table:
            os.makedirs(CONF, exist_ok=True)
            open(TABLE_FILE, "w").close()
        elif os.path.exists(TABLE_FILE):
            os.remove(TABLE_FILE)
        print("motors: table mode %s" % ("ON" if self.table else "off"), file=sys.stderr)

    def drive(self, left, right):
        if self.table:
            return
        left = max(-1.0, min(1.0, float(left)))
        right = max(-1.0, min(1.0, float(right)))
        self._write(b"T %.2f %.2f\n" % (left, right))
        self.last_drive = time.monotonic()
        self.moving = bool(left or right)

    def stop(self):
        self._write(b"S\n")
        self.moving = False

    def poll(self):
        """Read whatever the Pico said; keep the last line for the page."""
        if not self.ser:
            return
        try:
            data = self.ser.read(512)
        except serial.SerialException:
            self.ser = None
            return
        for line in data.split(b"\n"):
            line = line.strip().decode(errors="replace")
            if line:
                self.said = line

    def close(self):
        if self.ser:
            self._write(b"S\nq\n")
            time.sleep(0.2)
            self.ser.close()


async def motor_watch(app):
    """Stop the motors if the page stops sending while they're moving."""
    m = app["motors"]
    next_try = 0.0
    while True:
        if not m.ok and time.monotonic() > next_try:
            await asyncio.to_thread(m.connect)
            next_try = time.monotonic() + RECONNECT_S
        m.poll()
        if m.moving and time.monotonic() - m.last_drive > STALE_S:
            m.stop()
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------- gimbal

class Gimbal:
    """The gimbal Pico running keyctl.run(). The page sends a look rate while
    its camera pad is held; gimbal_watch() turns that into absolute targets
    ("A pan,tilt"), and the Pico eases toward them. Let go and it holds there.
    + pan turns to the camera's right, + tilt looks up.

    Her eyes are on the same Pico (eyes/picoeyes.py): everything for it goes
    through one writer thread, in order, so a frame on its way doesn't hold
    up the event loop and a move can't land in the middle of a frame."""

    # A frame cut off half-sent (this server stopped mid-write) leaves keyctl
    # waiting for the rest. This many zeros finish any frame; keyctl and the
    # REPL both ignore them.
    FLUSH = 6 + 240 * 240 * 2

    def __init__(self, port):
        self.port = port
        self.ser = None
        self.gen = 0            # goes up each time keyctl (re)starts (picoeyes redraws)
        self.pan = self.tilt = 0.0
        self.rate = (0.0, 0.0)
        self.last_look = 0.0
        self.moved_at = 0.0     # last aim, so curiosity() can ignore the picture swinging
        self.held = False       # keyctl started with the servos off, till settled() (gimbal_watch)
        self.out = queue.Queue()
        threading.Thread(target=self._writer, daemon=True).start()
        self.connect()

    def connect(self):
        try:
            ser = serial.Serial(self.port, 115200, timeout=0)
        except serial.SerialException as e:
            print("gimbal: offline (%s)" % e, file=sys.stderr)
            return
        try:
            # Already running keyctl (this server restarted, the Pico didn't)?
            # Then carry on from where it is, without the centring snap.
            at = self._where(ser)
            if not at:
                ser.write(bytes(self.FLUSH))
                at = self._where(ser)
            if at:
                self.pan, self.tilt = at
            else:
                # Start keyctl where the head was left (no snap), then ease home.
                was = load_pose()
                self.held = not settled()
                picolink.start(ser, was, hold=self.held)
                self.pan, self.tilt = HOME
                ser.write(b"A%.1f,%.1f\n" % HOME)
        except (serial.SerialException, SystemExit, ValueError) as e:
            print("gimbal: offline (%s)" % e, file=sys.stderr)
            ser.close()
            return
        print("gimbal: online at %+.0f %+.0f%s" % (self.pan, self.tilt,
                                                  ", servos off till she's settled" if self.held else ""),
              file=sys.stderr)
        self.gen += 1
        self.ser = ser

    @staticmethod
    def _where(ser):
        """Ask a running keyctl where it is: (pan, tilt), or None."""
        ser.reset_input_buffer()
        ser.write(b"P")
        time.sleep(0.3)
        at = [l.split() for l in ser.read(512).split(b"\n")]
        at = [l for l in at if len(l) == 3 and l[0] == b"AT"]
        return (float(at[-1][1]), float(at[-1][2])) if at else None

    @property
    def ok(self):
        return self.ser is not None

    def _writer(self):
        while True:
            data, done = self.out.get()
            ser = self.ser
            if ser:
                try:
                    ser.write(data)
                except serial.SerialException as e:
                    print("gimbal: write failed (%s)" % e, file=sys.stderr)
                    self.ser = None
            if done:
                done.set()

    def _write(self, data):
        if self.ser:
            self.out.put((data, None))

    def send(self, data):
        """For the eyes: queue bytes and wait till they're written. False if
        the Pico's offline (or went while they were on their way)."""
        if not self.ser:
            return False
        done = threading.Event()
        self.out.put((data, done))
        return done.wait(5) and self.ser is not None

    def look(self, x, y):
        self.rate = (max(-1.0, min(1.0, float(x))), max(-1.0, min(1.0, float(y))))
        self.last_look = time.monotonic()

    def aim(self, pan, tilt):
        pan = max(PAN_LIM[0], min(PAN_LIM[1], pan))
        tilt = max(TILT_LIM[0], min(TILT_LIM[1], tilt))
        if abs(pan - self.pan) > 0.2 or abs(tilt - self.tilt) > 0.2:
            self.moved_at = time.monotonic()
        self.pan, self.tilt = pan, tilt
        self._write(b"A%.1f,%.1f\n" % (self.pan, self.tilt))

    def centre(self):
        """Back to her home pose (HOME), not level."""
        self.rate = (0.0, 0.0)
        self.aim(*HOME)

    def relax(self):
        """Stop the servo pulses; the next move picks them up again."""
        self.rate = (0.0, 0.0)
        self._write(b"x")

    def tick(self, dt):
        if not self.ser:
            return
        try:
            self.ser.read(512)  # keyctl's POS lines: drain, or the Pico blocks on print()
        except serial.SerialException:
            self.ser = None
            return
        x, y = self.rate
        if (x or y) and time.monotonic() - self.last_look < STALE_S:
            self.aim(self.pan + x * LOOK_DEG_S * dt, self.tilt + y * LOOK_DEG_S * dt)


def settled():
    """Done booting (SETTLE_UP_S and SETTLE_LOAD, or SETTLE_MAX_S)."""
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        load = os.getloadavg()[0]
    except (OSError, ValueError):
        return True
    return up >= SETTLE_MAX_S or (up >= SETTLE_UP_S and load < SETTLE_LOAD)


def load_pose():
    """(pan, tilt) the head was last aimed at, or None."""
    try:
        with open(POSE_FILE) as f:
            p = json.load(f)
        return float(p["pan"]), float(p["tilt"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_pose(pan, tilt):
    try:
        tmp = POSE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"pan": round(pan, 1), "tilt": round(tilt, 1)}, f)
        os.replace(tmp, POSE_FILE)
    except OSError as e:
        print("gimbal: can't keep the pose (%s)" % e, file=sys.stderr)


async def gimbal_watch(app):
    g = app["gimbal"]
    next_try = time.monotonic() + RECONNECT_S
    saved, save_at = load_pose(), 0.0
    while True:
        if not g.ok and time.monotonic() > next_try:
            await asyncio.to_thread(g.connect)
            next_try = time.monotonic() + RECONNECT_S
        if g.ok and g.held and settled():
            g.held = False
            g._write(b"G")
            print("gimbal: servos on", file=sys.stderr)
        g.tick(0.05)
        now = time.monotonic()
        pose = (round(g.pan, 1), round(g.tilt, 1))
        if g.ok and pose != saved and now >= save_at:
            save_pose(*pose)
            saved, save_at = pose, now + POSE_SAVE_S
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------- camera

class Recorder:
    """Pipe camera frames into ffmpeg, which adds the camera mic and writes an
    H.264/AAC MP4.

    - Sync: both inputs are stamped with wall-clock time, so sound stays in
      sync although the camera's frame rate wanders.
    - Never blocks the camera: frames go through a short queue to a writer
      thread, and if ffmpeg falls behind, frames are dropped rather than
      freezing the live view.
    - Crash-safe: while recording, the MP4 is fragmented (one fragment a
      second), so a power cut loses only the last second or so. On stop it's
      rewritten as a plain MP4 (no re-encoding, a few seconds), which every
      player handles.

    Not the Pi's hardware encoder (h264_v4l2m2m): with ffmpeg 7 it hung, or
    wrote unplayable files, after 10-25 s. Not -shortest either: with a live
    mic input it deadlocks ffmpeg. x264 at 2 threads costs about a core."""

    def __init__(self, path, size, audio):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-thread_queue_size", "64", "-use_wallclock_as_timestamps", "1",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "%dx%d" % size, "-i", "-"]
        if audio:
            cmd += ["-thread_queue_size", "1024", "-f", "alsa", "-i", audio,
                    "-c:a", "aac", "-b:a", "128k"]
        cmd += ["-c:v", "libx264", "-preset", "superfast", "-crf", "23", "-g", "15",
                "-threads", "2", "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", "15",
                "-movflags", "+frag_keyframe+default_base_moof", path]
        self.path, self.size = path, size
        self.started = time.time()
        self.dead = False
        self.frames = queue.Queue(maxsize=3)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, start_new_session=True)
        self.thread = threading.Thread(target=self._feed, daemon=True)
        self.thread.start()

    def _feed(self):
        while True:
            frame = self.frames.get()
            if frame is None:
                break
            try:
                self.proc.stdin.write(frame.tobytes())
            except (BrokenPipeError, ValueError, OSError):
                self.dead = True
                break
        try:
            self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def write(self, frame):
        """False once ffmpeg has gone."""
        if self.dead or self.proc.poll() is not None:
            return False
        if frame.shape[1::-1] != self.size:
            frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            pass                    # ffmpeg is behind: drop this one
        return True

    def close(self):
        """Stop and tidy up the file. Blocks for a few seconds."""
        try:
            self.frames.put(None, timeout=2)
        except queue.Full:
            pass
        self.thread.join(timeout=3)
        # The mic never ends by itself, so ffmpeg needs telling: SIGINT makes
        # it finish the file cleanly.
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        finish_mp4(self.path)


def finish_mp4(path):
    """Rewrite a fragmented MP4 as a plain one with its index up front, for
    players (phones, VLC, Messenger) that dislike fragments. Keeps the
    fragmented original if anything goes wrong."""
    tmp = path + ".tmp.mp4"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                        "-c", "copy", "-movflags", "+faststart", tmp])
    if r.returncode == 0 and os.path.getsize(tmp) > 0:
        os.replace(tmp, path)
    else:
        print("recording: couldn't tidy %s; the fragmented file is kept" % path, file=sys.stderr)
        if os.path.exists(tmp):
            os.remove(tmp)


class Camera:
    """Grabs frames on a thread while anyone is watching or it's recording,
    and keeps the latest as JPEG bytes for the stream. Stops GRACE_S after
    the last viewer leaves (unless it's recording).

    While recording it captures at rec_size (bigger, for the file) and
    shrinks frames to size for the stream, so the live view costs the same."""

    GRACE_S = 5

    def __init__(self, dev, size, fps, quality, rec_size=None, rec_dir=None, audio=None):
        self.dev, self.size, self.fps, self.quality = dev, size, fps, quality
        self.rec_size, self.rec_dir, self.audio = rec_size or size, rec_dir, audio
        self.jpeg = None
        self.frame = None           # latest picture (stream size), for look_at_me()
        self.frame_at = 0.0
        self.seq = 0
        self.viewers = 0
        self.lookers = 0            # viewers that only want frames, not the JPEG stream
        self.left_at = 0.0
        self.thread = None
        self.error = ""
        self.rec = None
        self.rec_error = ""

    def _ensure(self):
        if not (self.thread and self.thread.is_alive()):
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def join(self, look=False):
        """look: only wants frames (no JPEG for the stream)."""
        self.viewers += 1
        self.lookers += look
        self._ensure()

    def leave(self, look=False):
        self.viewers -= 1
        self.lookers -= look
        if self.viewers == 0:
            self.left_at = time.monotonic()

    # -- recording

    def start_recording(self):
        if self.rec:
            return
        os.makedirs(self.rec_dir, exist_ok=True)
        free = shutil.disk_usage(self.rec_dir).free
        if free < MIN_FREE:
            self.rec_error = "disk nearly full (%.1f GB free)" % (free / 1e9)
            return
        name = time.strftime("rp-%Y%m%d-%H%M%S.mp4")
        audio = self.audio if self.audio and os.path.exists(AUDIO_CARD) else None
        self.rec_error = ""
        self.rec = Recorder(os.path.join(self.rec_dir, name), self.rec_size, audio)
        print("recording to", self.rec.path, "(sound)" if audio else "(no sound)", file=sys.stderr)
        self._ensure()

    def stop_recording(self):
        """Blocks a few seconds while ffmpeg finishes: call it off the event loop."""
        rec, self.rec = self.rec, None
        if rec:
            rec.close()
            print("saved", rec.path, file=sys.stderr)
            self.left_at = time.monotonic()

    def recording(self):
        r = self.rec
        return {"on": bool(r), "secs": int(time.time() - r.started) if r else 0,
                "file": os.path.basename(r.path) if r else "", "error": self.rec_error}

    def _open(self, size):
        cap = cv2.VideoCapture(self.dev, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _run(self):
        params = [cv2.IMWRITE_JPEG_QUALITY, self.quality]
        cap, opened_at = None, None
        try:
            while (self.viewers > 0 or self.rec
                   or time.monotonic() - self.left_at < self.GRACE_S):
                want = self.rec_size if self.rec else self.size
                if want != opened_at:       # (re)open at the size we need now
                    if cap:
                        cap.release()
                    cap, opened_at = self._open(want), want
                    if not cap.isOpened():
                        self.error = "can't open %s (in use by track.py?)" % self.dev
                        print("camera:", self.error, file=sys.stderr)
                        return
                    self.error = ""
                ok, frame = cap.read()
                if not ok:
                    self.error = "camera read failed"
                    time.sleep(0.5)
                    continue
                rec = self.rec
                if rec and not rec.write(frame):
                    self.rec_error = "recording stopped: ffmpeg quit (see journalctl -u rp-brain)"
                    self.rec = None
                    threading.Thread(target=rec.close, daemon=True).start()
                fh, fw = frame.shape[:2]
                if fw > self.size[0]:       # recording size: shrink, keeping its shape
                    frame = cv2.resize(frame, (self.size[0], round(self.size[0] * fh / fw)),
                                       interpolation=cv2.INTER_AREA)
                self.frame, self.frame_at = frame, time.monotonic()
                if self.viewers <= self.lookers:
                    continue                # nobody's watching the stream
                ok, buf = cv2.imencode(".jpg", frame, params)
                if ok:
                    self.jpeg = buf.tobytes()
                    self.seq += 1
        finally:
            if cap:
                cap.release()
            self.jpeg = None
            self.frame = None


async def video(request):
    cam = request.app["camera"]
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-store",
    })
    await resp.prepare(request)
    # Keep the kernel's send buffer to a couple of frames. Left at its default
    # it soaks up seconds of video when the link is slower than the stream,
    # and everything you see is that far behind. Small, a busy link pushes back
    # straight away and we skip frames instead of queueing them.
    sock = request.transport.get_extra_info("socket") if request.transport else None
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
    cam.join()
    seq = -1
    try:
        while True:
            if cam.seq != seq and cam.jpeg:
                seq, jpeg = cam.seq, cam.jpeg
                # Waits for the client to take it, so a slow link just gets
                # fewer (always the newest) frames rather than a backlog.
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: %d\r\n\r\n" % len(jpeg) + jpeg + b"\r\n")
            await asyncio.sleep(1 / (cam.fps * 2))
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        cam.leave()
    return resp


# ---------------------------------------------------------------- recordings

def rec_path(request):
    # /rec/<name without .mp4>: Android video apps claim any link ending in
    # .mp4 and pop up "Open with" before the browser can download it.
    name = request.match_info["name"]
    if re.fullmatch(r"rp-\d{8}-\d{6}", name):
        name += ".mp4"
    if not re.fullmatch(r"rp-\d{8}-\d{6}\.mp4", name):
        raise web.HTTPNotFound()
    path = os.path.join(request.app["camera"].rec_dir, name)
    if not os.path.isfile(path):
        raise web.HTTPNotFound()
    return path


def file_key(app, name):
    """Lets a recording's link work without the login cookie. Android hands
    downloads to a download manager that doesn't send the cookie, so a plain
    link got the login page saved as .mp4. Tied to the password, like the
    cookie: changing the password kills old links."""
    return hmac.new(app["token"].encode(), b"rec:" + name.encode(), hashlib.sha256).hexdigest()[:32]


async def recordings(request):
    """JSON list of recordings, newest first, plus free space."""
    cam = request.app["camera"]
    d = cam.rec_dir
    out = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            if re.fullmatch(r"rp-\d{8}-\d{6}\.mp4", name):
                st = os.stat(os.path.join(d, name))
                out.append({"name": name, "bytes": st.st_size, "mtime": int(st.st_mtime),
                            "key": file_key(request.app, name)})
    out.sort(key=lambda r: r["name"], reverse=True)
    free = shutil.disk_usage(d if os.path.isdir(d) else os.path.expanduser("~")).free
    return web.json_response({"files": out, "free": free, "current": cam.recording()["file"]})


async def recording_get(request):
    """?play=1 plays it in the browser; otherwise it downloads."""
    path = rec_path(request)
    name = os.path.basename(path)
    if request.query.get("play"):
        headers = {"Content-Type": "video/mp4", "Content-Disposition": 'inline; filename="%s"' % name}
    else:
        # A generic type, so phones save it instead of offering to open it.
        headers = {"Content-Type": "application/octet-stream",
                   "Content-Disposition": 'attachment; filename="%s"' % name}
    return web.FileResponse(path, headers=headers)


async def recording_delete(request):
    path = rec_path(request)
    if os.path.basename(path) == request.app["camera"].recording()["file"]:
        raise web.HTTPConflict(text="still recording")
    os.remove(path)
    return web.json_response({"ok": True})


async def rec_watch(app):
    """Stop a recording before it fills the disk."""
    cam = app["camera"]
    while True:
        await asyncio.sleep(10)
        if cam.rec and shutil.disk_usage(cam.rec_dir).free < MIN_FREE // 4:
            await asyncio.to_thread(cam.stop_recording)
            cam.rec_error = "recording stopped: disk nearly full"


# ---------------------------------------------------------------- auth

def read_password():
    try:
        with open(PASSWORD_FILE) as f:
            return f.readline().rstrip("\n")
    except FileNotFoundError:
        return None


def session_token(password):
    """Cookie value: a hash of the password, so it survives restarts and
    stops working when the password changes."""
    key = hashlib.sha256(password.encode()).digest()
    return hmac.new(key, b"robotpattinson-session", hashlib.sha256).hexdigest()


LOCAL = ("127.0.0.1", "::1")


# What each page and request needs (users.py), by path prefix, first match.
NEEDS = (("/video.mjpg", "watch"), ("/recordings", "record"), ("/rec/", "record"),
         ("/say", "say"), ("/ask", "chat"), ("/recall", "chat"), ("/hush", "chat"), ("/eyes", "drive"),
         ("/listen", "listen"), ("/talk", "talk"),
         ("/people.json", "people"), ("/people/", "people"), ("/limbic.json", "people"), ("/log.json", "users"), ("/users", "users"), ("/claude", "claude"))
# Over the control websocket (ws_control): "stop" needs nothing (it's safety).
WS_NEEDS = {"drive": "drive", "look": "drive", "centre": "drive", "relax": "drive", "mood": "drive",
            "eyes": "drive", "table": "drive", "follow": "drive", "ears": "drive", "record": "record"}


# Typed commands (/ask): chat covers small talk; anything that does more
# needs what it does. Not listed: drive (moving her, music, timers, her voice).
ASK_FREE = {"stop", "hello", "goodnight", "thanks", "praise", "how", "time", "weather", "pressure", "dnd", "dnd_off",
            "news",
            "whoami", "timer_left"}
ASK_NEEDS = {"lastseen": "people", "whosthat": "people", "where": "people", "meet": "people",
             "forget": "people", "introduce": "people", "record": "record"}


def needs(path):
    return next((perm for pre, perm in NEEDS if path.startswith(pre)), None)


@web.middleware
async def require_login(request, handler):
    """Who's this (request["user"]: (name, permissions); name is what she
    calls them (users.py, session); request["login"]: the login), and may they? Logins are users.py's; before anyone's been
    added, the old shared password (as user "") can do everything."""
    if request.path in ("/login", "/favicon.ico"):
        return await handler(request)
    if request.path == "/merge":        # (friday's mind: the key they share, nobody's login)
        if not merge_key_ok(request.headers.get("Authorization", "")):
            raise web.HTTPForbidden(text="no")
        return await handler(request)
    user = None
    # talk.py, on the Pi. (Not anything relayed by a proxy on the Pi, like
    # tailscale serve: that could be anyone. It's pointed at the Tailscale
    # address anyway, but just in case.)
    if (request.path in ("/say", "/ask", "/eyes", "/claude", "/recall") and request.remote in LOCAL
            and "X-Forwarded-For" not in request.headers and "Tailscale-User-Login" not in request.headers):
        user = ("", set(users.PERMS))
    cookie = request.cookies.get(COOKIE, "")
    us = request.app["users"]
    login = ""
    if not user:
        s = await asyncio.to_thread(us.session, cookie)
        if s:
            login, user = s[0], (s[2], s[1])
    if not user and not us.any() and hmac.compare_digest(cookie, request.app["token"]):
        user = ("", set(users.PERMS))
    if not user and request.method == "GET" and request.path.startswith("/rec/"):
        name = request.path.rsplit("/", 1)[1] + ".mp4"
        if hmac.compare_digest(request.query.get("k", ""), file_key(request.app, name)):
            user = ("", {"record"})
    if not user:
        if request.path in ("/ws", "/listen", "/talk") or request.path.endswith(".json"):
            raise web.HTTPUnauthorized()
        raise web.HTTPFound("/login")
    need = needs(request.path)
    if need and need not in user[1]:
        raise web.HTTPForbidden(text="Not allowed (%s)." % need)
    request["user"] = user
    request["login"] = login
    return await handler(request)


class MERGED:
    """The friday branches whose fondness she's merged (merge_post), so it's never added twice."""
    PATH = os.path.join(memory.DIR, "merged.json")

    @classmethod
    def read(cls):
        try:
            with open(cls.PATH) as f:
                return list(json.load(f))
        except (OSError, ValueError):
            return []

    @classmethod
    def write(cls, done):
        with open(cls.PATH + ".tmp", "w") as f:
            json.dump(done[-100:], f)
        os.replace(cls.PATH + ".tmp", cls.PATH)


def merge_key_ok(header):
    try:
        with open(claude.KEY_FILE) as f:
            key = f.read().strip()
    except OSError:
        return False
    return bool(key) and hmac.compare_digest(header, "Bearer " + key)


async def merge_post(request):
    """POST /merge {"part": {table: [rows]}} or {"limbic": {"base", "bonds", "last_seen"}}: what she
    lived through on friday while her body was off (brain/mind.py), folded into her memory here, like
    a git merge (memory.py, merge; limbic.py, merge). Safe to send twice."""
    app = request.app
    body = await request.json()
    if isinstance(body.get("part"), dict):
        got = await asyncio.to_thread(app["memory"].merge, body["part"])
    elif isinstance(body.get("limbic"), dict):
        l = body["limbic"]
        branch = str(l.get("branch") or "")
        done = MERGED.read()
        if branch and branch in done:       # (fondness adds up: once per branch, however often it's sent)
            got = {"bonds (already)": 0}
        else:
            app["limbic"].merge(l.get("base") or {}, l.get("bonds") or {}, l.get("last_seen") or {})
            if branch:
                MERGED.write(done + [branch])
            got = {"bonds": len(l.get("bonds") or {})}
    else:
        raise web.HTTPBadRequest(text="part or limbic")
    print("merge: from friday: %s" % (", ".join("%s %d" % kv for kv in got.items() if kv[1]) or "nothing new"),
          file=sys.stderr)
    return web.json_response({"merged": got})


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Patti</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#111;color:#eee;font:16px system-ui,sans-serif}
form{display:flex;flex-direction:column;gap:12px;width:min(320px,90vw)}
input,button{font:inherit;padding:12px;border-radius:8px;border:1px solid #444;background:#1c1c1c;color:#eee}
button{background:#d9622b;border:0;color:#fff;font-weight:600}
p{color:#e88;margin:0}</style></head><body>
<form method="post" action="/login"><h1>Patti</h1>%s
%s<input type="password" name="password" placeholder="Password" autocomplete="current-password">
<button>Log in</button></form></body></html>"""


NAME_FIELD = '<input name="name" placeholder="Name" autofocus autocomplete="username" value="%s">'


def login_page(request, msg="", name=""):
    field = NAME_FIELD % html.escape(name, quote=True) if request.app["users"].any() else ""
    return LOGIN_PAGE % (msg, field)


async def login_get(request):
    return web.Response(text=login_page(request), content_type="text/html")


async def login_post(request):
    form = await request.post()
    pw, name = form.get("password", ""), form.get("name", "").strip()
    us = request.app["users"]
    if us.any():
        who = await asyncio.to_thread(us.check, name, pw)
        cookie = await asyncio.to_thread(us.login, who) if who else None
    else:
        cookie = request.app["token"] if hmac.compare_digest(session_token(pw), request.app["token"]) else None
    if cookie:
        resp = web.HTTPFound("/")
        resp.set_cookie(COOKIE, cookie, max_age=users.SESSION_S, httponly=True, samesite="Strict")
        raise resp
    await asyncio.sleep(1)      # slow down guessing
    return web.Response(text=login_page(request, "<p>Wrong name or password.</p>" if us.any() else
                                        "<p>Wrong password.</p>", name),
                        content_type="text/html", status=401)


async def logout(request):
    await asyncio.to_thread(request.app["users"].logout, request.cookies.get(COOKIE, ""))
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE)
    raise resp


def known_people(app):
    """Everyone she knows by face or voice."""
    w = app["who"]
    return set(w["faces"].people if w["faces"] else ()) | set(w["voices"].people if w["voices"] else ())


async def users_get(request):
    """GET /users.json: the logins, the roles, and the names she knows (to
    match a login to a face and voice)."""
    app = request.app
    profiles = app["users"].profiles()
    return web.json_response({"users": [dict(profiles.get(n, {}), name=n, role=r, person=p)
                                        for n, r, p in app["users"].names()],
                              "roles": {k: sorted(v) for k, v in users.ROLES.items()},
                              "people": sorted(known_people(app))})


async def users_add(request):
    """POST /users {"name", "password", "role"}. The very first is made
    with the old shared password, and is always an admin."""
    app, us = request.app, request.app["users"]
    body = await request.json()
    name, pw = str(body.get("name", "")).strip(), str(body.get("password", ""))
    person = body.get("person") or next((p for p in known_people(app) if p.lower() == name.lower()), None)
    if person and person not in known_people(app):
        return web.json_response({"error": "She doesn't know anyone called %s." % person}, status=400)
    if set(name.lower().split()) & set(users.HER_NAMES):
        return web.json_response({"error": "Patti's her name: pick another login."}, status=400)
    first = not us.any()
    role = "admin" if first else body.get("role")
    if not NAME_RE.match(name) or role not in users.ROLES or len(pw) < 6:
        return web.json_response({"error": "A name (letters), a role, and a password of 6 or more."}, status=400)
    if any(n.lower() == name.lower() for n, _, _ in us.names()):
        return web.json_response({"error": "%s already has a login." % name}, status=409)
    bad = check_profile(body)
    if bad:
        return web.json_response({"error": bad}, status=400)
    await asyncio.to_thread(us.add, name, pw, role, person)
    await asyncio.to_thread(us.set_profile, name, **{k: str(body[k]) for k in users.PROFILE if k in body})
    print("users: %s added %s (%s)" % (request["user"][0] or "the shared password", name, role), file=sys.stderr)
    if first:       # (log them in as themselves: the shared password's done now)
        resp = web.json_response({"name": name, "first": True})
        resp.set_cookie(COOKIE, await asyncio.to_thread(us.login, name), max_age=users.SESSION_S,
                        httponly=True, samesite="Strict")
        return resp
    return web.json_response({"name": name})


async def users_change(request):
    """POST /users/<name> {"role"} or {"password"}; DELETE /users/<name>."""
    app, us, name = request.app, request.app["users"], request.match_info["name"]
    if not any(n == name for n, _, _ in us.names()):
        raise web.HTTPNotFound()
    me = request["login"]
    if request.method == "DELETE":
        if name == me:
            return web.json_response({"error": "You can't remove yourself."}, status=400)
        await asyncio.to_thread(us.remove, name)
    else:
        body = await request.json()
        if "role" in body:
            if body["role"] not in users.ROLES or (name == me and body["role"] != "admin"):
                return web.json_response({"error": "Not that role (and not off admin for yourself)."}, status=400)
            await asyncio.to_thread(us.role, name, body["role"])
        bad = check_profile(body)
        if bad:
            return web.json_response({"error": bad}, status=400)
        await asyncio.to_thread(us.set_profile, name, **{k: str(body[k]) for k in users.PROFILE if k in body})
        if "person" in body:
            if body["person"] and body["person"] not in known_people(app):
                return web.json_response({"error": "She doesn't know them."}, status=400)
            await asyncio.to_thread(us.person, name, body["person"])
        if "password" in body:
            if len(str(body["password"])) < 6:
                return web.json_response({"error": "6 characters or more."}, status=400)
            await asyncio.to_thread(us.passwd, name, str(body["password"]))
    print("users: %s changed %s" % (me, name), file=sys.stderr)
    return web.json_response({})


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def check_profile(body):
    """What's wrong with a profile's fields, or None."""
    for k in users.PROFILE:
        v = str(body.get(k, ""))
        if len(v) > 100:
            return "%s is too long." % k
        if k in ("first", "nicknames") and set(re.findall(r"[a-z]+", v.lower())) & set(users.HER_NAMES):
            return "Patti's her name: nobody else's."
        if k == "email" and v.strip() and not EMAIL_RE.match(v.strip()):
            return "That email doesn't look right."
    return None


async def me_get(request):
    """GET /me: my login and profile."""
    login = request["login"]
    prof = await asyncio.to_thread(request.app["users"].profile, login) if login else None
    return web.json_response({"login": login, "name": request["user"][0], "profile": prof})


async def me_post(request):
    """POST /me {first, last, email, location}: my profile."""
    login = request["login"]
    if not login:
        return web.json_response({"error": "Make your own login first."}, status=400)
    body = await request.json()
    bad = check_profile(body)
    if bad:
        return web.json_response({"error": bad}, status=400)
    await asyncio.to_thread(request.app["users"].set_profile, login,
                            **{k: str(body[k]) for k in users.PROFILE if k in body})
    return web.json_response({})


async def password_post(request):
    """POST /password {"old", "new"}: change your own."""
    app, us = request.app, request.app["users"]
    me = request["login"]
    body = await request.json()
    if not me or not await asyncio.to_thread(us.check, me, str(body.get("old", ""))):
        await asyncio.sleep(1)
        return web.json_response({"error": "That's not your current password."}, status=403)
    if len(str(body.get("new", ""))) < 6:
        return web.json_response({"error": "6 characters or more."}, status=400)
    await asyncio.to_thread(us.passwd, me, str(body["new"]))
    resp = web.json_response({})
    resp.set_cookie(COOKIE, await asyncio.to_thread(us.login, me), max_age=users.SESSION_S,
                    httponly=True, samesite="Strict")
    return resp


# ---------------------------------------------------------------- page + control

async def index(request):
    return web.FileResponse(os.path.join(HERE, "static", "index.html"),
                            headers={"Cache-Control": "no-store"})


_detectors = {}


_eyes = None    # eyes.Eyes, when the panels are there (set in main)


def start_eyes(gimbal=None):
    """The eyes on the gimbal Pico, or (gimbal None) on the Pi's own SPI6."""
    global _eyes
    try:
        import eyes
        if gimbal:
            import picoeyes
            _eyes = eyes.Eyes(picoeyes, gimbal)
        else:
            _eyes = eyes.Eyes()
        print("eyes: online%s" % (" (on the gimbal Pico)" if gimbal else ""), file=sys.stderr)
    except Exception as e:      # no panels, no SPI6, no spidev: carry on without
        print("eyes: offline (%s)" % e, file=sys.stderr)
    return _eyes


def detect_faces(frame):
    """YuNet's faces in a picture (rows of x, y, w, h, ...), or None."""
    h, w = frame.shape[:2]
    det = _detectors.get((w, h))
    if not det:
        det = _detectors[(w, h)] = faces.detector(w, h)
    return det.detect(frame)[1]


async def face_in(cam, near=None):
    """Find a face in the next picture the camera takes. Returns ((pan, tilt)
    degrees from the middle of the picture to it, (x, y) pixel centre), or
    None. With several faces: the one nearest `near` (pixels), else the biggest."""
    asked = time.monotonic()
    while cam.frame_at < asked:             # a picture taken after we got here
        if time.monotonic() - asked > 3:
            raise asyncio.CancelledError("no picture from the camera")
        await asyncio.sleep(0.02)
    frame = cam.frame
    if frame is None:
        return None
    h, w = frame.shape[:2]
    found = await asyncio.to_thread(detect_faces, frame)
    if found is None or not len(found):
        return None
    if near:
        f = min(found, key=lambda f: (f[0] + f[2] / 2 - near[0]) ** 2 + (f[1] + f[3] / 2 - near[1]) ** 2)
    else:
        f = max(found, key=lambda f: f[2] * f[3])
    cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
    off = ((cx - w / 2) / w * HFOV, (h / 2 - cy) / h * (HFOV * h / w))
    if _eyes:       # the pupils go straight to the face
        _eyes.look(off[0] / EYE_RANGE_DEG, off[1] / EYE_RANGE_DEG)
    return off, (cx, cy)


async def find_face(cam, g):
    """Look where the camera points now, then try SEEK_PANS along each of
    SEEK_TILTS (nearer row first, nearest spots first; the second row starts
    from where the first ended) until a face turns up, and aim at it. True if
    it found one."""
    spots = [None]
    pans = sorted(SEEK_PANS, key=lambda p: abs(p - g.pan))
    for tilt in sorted(SEEK_TILTS, key=lambda t: abs(t - g.tilt)):
        spots += [(p, tilt) for p in pans]
        pans = sorted(SEEK_PANS, key=lambda p: abs(p - spots[-1][0]))
    for spot in spots:
        if spot is not None:
            g.aim(*spot)
            await asyncio.sleep(SETTLE_S)
        f = await face_in(cam)
        if f:
            for _ in range(2):      # aim, then fine-tune from a fresh look
                off = f[0]
                g.aim(g.pan + off[0], g.tilt + off[1])
                await asyncio.sleep(SETTLE_S)
                f = await face_in(cam)
                if not f or max(abs(f[0][0]), abs(f[0][1])) < 3:
                    break
            return True
    return False


async def look_at_me(app):
    """Turn the camera to the nearest face, once."""
    cam, g = app["camera"], app["gimbal"]
    if not g.ok:
        return
    cam.join()      # make sure the camera runs, even with no page open
    try:
        if await find_face(cam, g):
            print("look at me: found you at pan %+.0f tilt %+.0f" % (g.pan, g.tilt), file=sys.stderr)
        else:
            print("look at me: nobody found", file=sys.stderr)
    finally:
        cam.leave()


async def follow_me(app):
    """Keep a face in view: eyes first, head only when the face leaves the
    middle FOCUS of the picture (see FOCUS above). Sticks with the face
    nearest the last one. If the face is gone for LOST_S it sweeps to find
    one; after GIVE_UP_S with nobody it stops following and leaves the camera
    where it is."""
    cam, g = app["camera"], app["gimbal"]
    if not g.ok:
        return
    cam.join()
    print("follow: on", file=sys.stderr)
    try:
        last, lost_at, swept_at = None, None, 0.0
        out_since, turning = None, False
        while True:
            started = time.monotonic()
            f = await face_in(cam, near=last)
            now = time.monotonic()
            await asyncio.sleep(max(0.0, started + FOLLOW_EVERY_S - now))
            if f:
                off, last = f
                lost_at = None
                w, h = cam.size
                outside = (abs(off[0]) > FOCUS * HFOV / 2
                           or abs(off[1]) > FOCUS * HFOV * h / w / 2)
                if not outside:
                    out_since = None
                elif not turning:
                    out_since = out_since or now
                    turning = now - out_since >= LEAD_S     # the eyes have led
                if turning:
                    if max(abs(off[0]), abs(off[1])) < RECENTRED_DEG:
                        turning, out_since = False, None
                    else:
                        g.aim(g.pan + off[0] * HEAD_GAIN, g.tilt + off[1] * HEAD_GAIN)
                continue
            turning, out_since = False, None
            lost_at = lost_at or now
            if now - lost_at > GIVE_UP_S:
                print("follow: nobody for %d s, stopping" % GIVE_UP_S, file=sys.stderr)
                return
            if now - lost_at > LOST_S and now - swept_at > SWEEP_EVERY_S:
                print("follow: lost you, looking around", file=sys.stderr)
                swept_at = now
                last = None
                if await find_face(cam, g):
                    lost_at = None
    finally:
        cam.leave()
        print("follow: off", file=sys.stderr)


def gaze(app, job):
    """Cancel whatever the camera is doing by itself (look_at_me, follow_me),
    and start job instead if given."""
    gz = app["gaze"]
    if gz["task"] and not gz["task"].done():
        gz["task"].cancel()
    gz["task"] = asyncio.create_task(job(app)) if job else None
    gz["job"] = job


def gaze_busy(app):
    gz = app["gaze"]
    return bool(gz["task"] and not gz["task"].done())


def hands_off(app):
    """She's been steered by hand: curiosity leaves her head be for a while."""
    app["curious"]["quiet_until"] = time.monotonic() + HAND_HOLD_S


def set_curious(app, on):
    app["curious"]["on"] = on
    try:
        if on:
            if os.path.exists(WATCH_OFF_FILE):
                os.remove(WATCH_OFF_FILE)
        else:
            open(WATCH_OFF_FILE, "w").close()
    except OSError as e:
        print("curious: can't remember that (%s)" % e, file=sys.stderr)
    print("curious: %s" % ("on" if on else "off"), file=sys.stderr)


async def curiosity(app):
    """Make eye contact with faces, always; with curiosity on, also look at
    what moves and turn her head (see CURIOUS_EVERY_S and the rest, above)."""
    cam, g, c = app["camera"], app["gimbal"], app["curious"]
    watching = False
    prev, seen_at = None, 0.0
    face, face_at, face_due, busy_at = None, 0.0, 0.0, 0.0
    recog_due = 0.0
    out_since, turning, home, kind = None, False, True, "motion"
    told = None             # what was last logged: None, "face" or "motion"
    wake_hits, wake_due = 0, 0.0
    mw, mh = MOTION_SIZE
    try:
        while True:
            await asyncio.sleep(CURIOUS_EVERY_S)
            now = time.monotonic()
            curious = c["on"] and g.ok
            head = curious and now - g.last_look > HAND_HOLD_S     # (eyes only, otherwise)
            asleep = _eyes is not None and _eyes.state == "asleep"
            want = (_eyes is not None and not gaze_busy(app) and now >= c["quiet_until"]
                    and (asleep or curious or not os.path.exists(CONTACT_OFF_FILE)))
            if want != watching:
                watching = want
                if want:
                    cam.join(look=True)
                else:
                    cam.leave(look=True)
                prev, face, out_since, turning = None, None, None, False
            if not watching:
                continue
            frame, at = cam.frame, cam.frame_at
            if frame is None or at == seen_at:
                continue
            if asleep and now < wake_due:
                continue
            seen_at = at
            h, w = frame.shape[:2]
            settling = at - g.moved_at < CURIOUS_SETTLE_S        # (when the picture was taken)

            # Movement: the biggest patch that changed since the last picture.
            grey = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(frame, MOTION_SIZE, interpolation=cv2.INTER_AREA),
                                                 cv2.COLOR_BGR2GRAY), (5, 5), 0)
            if _eyes.state != "awake":          # half an eye open: enough movement wakes her
                big = prev is not None and not settling and (cv2.absdiff(grey, prev) > MOTION_LEVEL).mean() > WAKE_MOTION
                wake_hits = wake_hits + 1 if big else 0
                if wake_hits >= WAKE_HITS:
                    wake_hits = 0
                    poke(app, "movement")
            if asleep:
                prev, wake_due = grey, now + WAKE_EVERY_S
                continue
            moving = None
            if prev is not None and not settling and curious:
                mask = (cv2.absdiff(grey, prev) > MOTION_LEVEL).astype(np.uint8)
                if MOTION_MIN < mask.mean() < MOTION_MAX:
                    n, _, stats, centres = cv2.connectedComponentsWithStats(cv2.dilate(mask, None, iterations=2))
                    if n > 1:
                        cx, cy = centres[1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))]
                        moving = (cx * w / mw, cy * h / mh)
            prev = grey

            # Faces: often while something's going on, now and then otherwise.
            if face and (now - face_at > FACE_HOLD_S or g.moved_at > face_at):
                face = None
            if now >= face_due and not settling:
                face_due = now + (FACE_FAST_S if now - busy_at < BUSY_S else FACE_SLOW_S)
                found = await asyncio.to_thread(detect_faces, frame)
                if found is not None and len(found):
                    near = face or moving
                    if near:
                        f = min(found, key=lambda f: (f[0] + f[2] / 2 - near[0]) ** 2 + (f[1] + f[3] / 2 - near[1]) ** 2)
                    else:
                        f = max(found, key=lambda f: f[2] * f[3])
                    face, face_at = (f[0] + f[2] / 2, f[1] + f[3] / 2), at
                    poke(app, "a face")
                    who = app["who"]["faces"]
                    if who and now >= recog_due:
                        recog_due = now + RECOG_EVERY_S
                        await look_who(app, frame, f)

            target = face or moving
            now_kind = "face" if face else "motion" if moving else None
            if now_kind != told:
                if now_kind:
                    off = ((target[0] - w / 2) / w * HFOV, (h / 2 - target[1]) / h * (HFOV * h / w))
                    print("curious: %s at %+.0f %+.0f (head %+.0f %+.0f)"
                          % (now_kind, off[0], off[1], g.pan, g.tilt), file=sys.stderr)
                else:
                    print("curious: nothing", file=sys.stderr)
                told = now_kind
            if not target:
                turning, out_since = False, None
                if head and not home and now - busy_at > (HOME_AFTER_S if kind == "face" else MOTION_HOME_S):
                    g.aim(*HOME)
                    home = True
                continue
            busy_at, kind = now, "face" if face else "motion"
            off = ((target[0] - w / 2) / w * HFOV, (h / 2 - target[1]) / h * (HFOV * h / w))
            _eyes.look(off[0] / EYE_RANGE_DEG, off[1] / EYE_RANGE_DEG)
            if not head or settling or _eyes.state != "awake":      # drowsy, or no curiosity: eyes only
                continue
            # Peripheral: the head follows once the eyes have led for LEAD_S.
            outside = abs(off[0]) > FOCUS * HFOV / 2 or abs(off[1]) > FOCUS * HFOV * h / w / 2
            if not outside and not turning:
                out_since = None
            elif not turning:
                out_since = out_since or now
                turning = now - out_since >= LEAD_S
            if turning:
                if max(abs(off[0]), abs(off[1])) < RECENTRED_DEG:
                    turning, out_since = False, None
                    continue
                if now - g.moved_at < HEAD_EVERY_S:
                    continue
                pan, tilt = g.pan + off[0] * HEAD_GAIN, g.tilt + off[1] * HEAD_GAIN
                pan = max(PAN_LIM[0], min(PAN_LIM[1], pan))
                tilt = max(TILT_LIM[0], min(TILT_LIM[1], tilt))
                if kind == "face" and abs(pan - g.pan) < 1 and abs(tilt - g.tilt) < 1:
                    turning, out_since = False, None    # as far as she goes: just look
                    continue
                if kind == "motion":
                    pan = max(HOME[0] - MOTION_LEASH_DEG, min(HOME[0] + MOTION_LEASH_DEG, pan))
                    tilt = max(HOME[1] - MOTION_LEASH_DEG, min(HOME[1] + MOTION_LEASH_DEG, tilt))
                    if abs(pan - g.pan) < 1 and abs(tilt - g.tilt) < 1:
                        # At the end of the leash and it's still going: let it go.
                        g.aim(*HOME)
                        home, turning, out_since, prev = True, False, None, None
                        continue
                g.aim(pan, tilt)
                home = False
    finally:
        if watching:
            cam.leave(look=True)


async def storm_watch(app):
    """Log the pressure, keep it calibrated, and warn of a storm (when she's awake)."""
    b = app["barometer"]
    if not b.bus:
        return
    while True:
        await asyncio.to_thread(b.sample)
        if time.time() - b.calibrated > barometer.CALIBRATE_S:
            await asyncio.to_thread(b.calibrate)
        if not _eyes or _eyes.state == "awake":     # not to wake her: she'll say it when she's up
            warning = b.warning()
            if warning:
                print("storm watch: %s" % b.words(), file=sys.stderr)
                say(app, replies.pick("storm_" + warning))
        await asyncio.sleep(barometer.SAMPLE_S)


async def place_watch(app):
    """Where she is, by the wifi she's on (places.py), looked at every so often."""
    p = app["place"]
    while True:
        if await asyncio.to_thread(p.check):
            print("place: %s, %s" % (p, "away" if p.away else "home"), file=sys.stderr)
            if app["memory"].place is not None:     # (not just where she woke up)
                app["limbic"].felt("new_place", why=p.words)
            app["memory"].moved(p.words, p.ssid)
        await asyncio.sleep(places.CHECK_S)


async def output_watch(app):
    """Her sound: friday's speakers at home, never away (outputs.py)."""
    p = app["place"]
    while True:
        if p.ssid:          # (once she knows where she is)
            await asyncio.to_thread(outputs.choose, p.away)
        await asyncio.sleep(outputs.CHECK_S)


# Her eyes show how she feels (limbic.face), unless someone's chosen a mood
# for them in the last MOOD_HOLD_S; only these moods are hers to change.
MOOD_HOLD_S = 1800
LIMBIC_FACES = ("normal", "happy", "surprised", "suspicious")


async def memory_watch(app):
    """Keep her memory's "right now" (now.json) up to date, and her feelings
    (limbic.py): who's with her, drift, her eyes."""
    mem, lim = app["memory"], app["limbic"]
    while True:
        talking = mem.talking()
        lim.company(set(present(app)) | ({talking} if talking else set()))
        lim.tick()
        if (_eyes and _eyes.mood in LIMBIC_FACES and time.monotonic() - app["feel"]["mood_set"] > MOOD_HOLD_S
                and lim.face() != _eyes.mood):
            _eyes.set_mood(lim.face())
        await unhappy_check(app)
        if _eyes:
            mem.felt(_eyes.mood)
        mem.state(awake=_eyes.state if _eyes else None, present=present(app), online=sorted(app["online"]),
                  away=app["place"].away)
        await asyncio.sleep(5)


def known_as(app, sid):
    """Someone she met as a stranger, whose face she's learned since (by
    name): who, if most of what she kept of them says so; else None."""
    st, who = app["who"]["strangers"], app["who"]["faces"]
    feats = st.people.get(sid) if st else None
    if feats is None or not len(feats) or not who:
        return None
    names = [who.identify_fp(fp)[0] for fp in feats]
    best = max(set(names), key=names.count)
    return best if best and names.count(best) * 2 > len(names) else None


# The macropad (macropad.py): what each key does (n: 1..12, row by row; "k1"
# .."k3": a knob pressed), and what turning each knob does (clockwise: louder,
# head right, head up). Toggles flip whatever she's doing now. The knobs move
# her head as the page's arrows do (her laws are about what she does of her
# own accord, not this).
PAD_KEYS = {1: "ears", 2: "dnd", 3: "record", 4: "watch", 5: "how", 6: "time", 7: "weather", 8: "music",
            9: "seek", 10: "follow", 11: "sleep", 12: "stop", "k1": "centre", "k2": "relax", "k3": "hush"}
PAD_PAN, PAD_TILT, PAD_VOLUME = 1, 2, 3     # which knob does what
PAN_STEP, TILT_STEP = 5, 3      # degrees a click
VOLUME_SAY_S = 0.8              # after turning her volume, she says something this long after the last click


def pad_key(app, n, pressed):
    if not pressed:
        return
    what = PAD_KEYS.get(n)
    print("macropad: %s -> %s" % (n, what), file=sys.stderr)
    m, cam = app["memory"], app["camera"]
    poke(app, "macropad")
    done = concurrent.futures.Future()
    done.set_result(None)
    act = lambda a, arg=None: asyncio.ensure_future(voice_action(app, a, arg, done))
    if what == "ears":
        on = app["chest"].settings["listen"] != "on"
        app["chest"].set_listening(on)
        say(app, replies.pick("pad_ears_on" if on else "mute"))
    elif what == "dnd":
        act("dnd_off" if app["drives"].quiet() else "dnd")
    elif what == "record":
        act("record", not cam.rec)
        say(app, replies.pick("pad_rec_on" if not cam.rec else "pad_rec_off"))
    elif what == "watch":
        act("watch", not app["curious"]["on"])
    elif what == "follow":
        act("unfollow" if following(app) else "follow")
    elif what == "sleep":
        if _eyes and _eyes.state != "awake":
            poke(app, "macropad: wake")
        else:
            act("sleep")
    elif what == "hush":
        if app["speech"]:
            app["speech"].hush()
    elif what == "music":
        act("music", "what")            # (what's playing through her)
    elif what in ("how", "time", "weather", "seek", "stop", "centre", "relax"):
        act(what)


def pad_turn(app, knob, clicks):
    poke(app, "macropad")
    if knob == PAD_VOLUME and app["speech"]:
        app["speech"].step_volume(clicks)
        f = app["feel"]
        f["vol_said"] = time.monotonic()
        def later(at=f["vol_said"]):
            if f["vol_said"] == at:                 # (the last click: let them hear it)
                say(app, replies.pick("louder" if clicks > 0 else "quieter"))
        asyncio.get_running_loop().call_later(VOLUME_SAY_S, later)
    elif knob in (PAD_PAN, PAD_TILT):
        hands_off(app)
        gaze(app, None)
        g = app["gimbal"]
        if g.ok:
            g.aim(g.pan + (clicks * PAN_STEP if knob == PAD_PAN else 0), g.tilt + (clicks * TILT_STEP if knob == PAD_TILT else 0))


async def news_watch(app):
    """The news (news.py): fetched a minute after she starts, then every news.FETCH_S."""
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(app["news"].fetch)
        except Exception as e:      # (never take the brain down)
            print("news: %r" % e, file=sys.stderr)
        await asyncio.sleep(news.FETCH_S)


def said_news(app, who):
    """"What's in the news?": a few headlines, each from where it's from;
    kept in the conversation, so "tell me more about the second one" works."""
    got = app["news"].headlines()
    if not got:
        say(app, replies.pick("news_none"))
        return
    line = replies.pick("news_intro") + " " + " ".join(
        "%s: %s." % (s["source"][0].upper() + s["source"][1:], s["title"].rstrip(".")) for s in got)
    say(app, line)
    chat_turn(app, who, "What's in the news?", line + " (" + "; ".join(
        "%s, %s: %s" % (s["source"], memory.when(s["at"]), s["summary"][:200]) for s in got) + ")")


def drive_ctx(app):
    """What drives.py needs from the rest of her."""
    us = app["users"]

    async def transcribe(audio):
        hint = ", ".join(sorted(known_people(app)) + app["memory"].names())
        try:
            with app["hat"].thinking():
                return await asyncio.to_thread(claude.transcribe, audio, hint)
        except claude.Unavailable:
            return await asyncio.to_thread(app["voice"].words, audio)

    async def remember(who, q, answer, lasting):
        vec = await asyncio.to_thread(claude.embed, [q]) if claude.ready() else None
        app["memory"].learned(who, q, answer, "told", lasting, vec[0] if vec else None, owner=who)

    async def name(sid, name):
        try:
            return await name_stranger(app, sid, name)
        except BadName:
            return None

    return {
        "present": lambda: present(app),
        "here_ago": lambda person: time.monotonic() - app["who"]["present"].get(person, -1e9),
        "hears": lambda: bool(app["voice"] and app["voice"].ok and not app["voice"].muted),
        "busy": lambda: (app["meet"].busy or bool(app["speech"] and app["speech"].speaking)
                         or app["camera"].rec or bool(_eyes and _eyes.state == "asleep")),
        "last_said": lambda: time.time() - (time.monotonic() - app["attn"]["at"]),
        "logins": us.logins,
        "login_for": us.login_for,
        "person_for": lambda login: (us.user(login) or (login,))[0],
        "perms": lambda login: (us.user(login) or (None, set()))[1],
        "transcribe": transcribe,
        "remember": remember,
        "name_stranger": name,
        "understand": lambda who, text: understand_said(app, text, who),
        "known_as": lambda sid: known_as(app, sid),
        "answered": lambda who, qid: app["clarify"].pop(who or "", None)
            if (app["clarify"].get(who or "") or (None,) * 3)[2:3] == (("question", qid),) else None,
        "await_answer": lambda who, q, telegram=False: app["clarify"].__setitem__(
            who or "", (q["text"], time.monotonic(), ("question", q["id"]), "telegram" if telegram else "voice")),
    }


# If she's been unhappy all day (her average, UNHAPPY_DAY), she tells the
# people who look after her (Telegram, the "users" permission), once a day,
# with what's been pulling her down most.
UNHAPPY_DAY = 0.42
UNHAPPY_EVERY_S = 86400


async def unhappy_check(app):
    now = time.time()
    f = app["feel"]
    if now - f["looked"] < 3600 or now - f["told"] < UNHAPPY_EVERY_S:      # (looked at hourly; told daily)
        return
    f["looked"] = now
    rows = await asyncio.to_thread(app["memory"].readings, now - 86400)
    if len(rows) < 600:         # (less than ten hours of readings: too soon to say)
        return
    avg = sum(r["happy"] for r in rows) / len(rows)
    if avg >= UNHAPPY_DAY:
        return
    f["told"] = now
    low = {k: sum(r[k] for r in rows) / len(rows) for k in ("oxytocin", "dopamine", "cortisol")}
    why = ("mostly loneliness" if low["oxytocin"] < 0.22 else "mostly boredom" if low["dopamine"] < 0.3
           else "mostly stress" if low["cortisol"] > 0.15 else "nothing in particular")
    text = ("I've been a bit down today (%.2f on average; content is 0.5), %s. My feelings page has the details."
            % (avg, why))
    print("limbic: unhappy day (%.2f, %s)" % (avg, why), file=sys.stderr)
    for chat, login in telegram.links().items():
        u = await asyncio.to_thread(app["users"].user, login)
        if u and "users" in u[1] and app["telegram"]:
            await tg_send(app, chat, text)


def following(app):
    gz = app["gaze"]
    return bool(gz["task"] and not gz["task"].done() and gz["job"] is follow_me)


def her_day(t=None):
    """Today's date as she counts days (starting at DAY_START_H), "YYYY-MM-DD"."""
    return time.strftime("%Y-%m-%d", time.localtime((t or time.time()) - DAY_START_H * 3600))


def greeting_key(t=None):
    h = time.localtime(t).tm_hour
    return ("greet_late" if h < 5 or h >= 23 else "greet_morning" if h < 12
            else "greet_afternoon" if h < 18 else "greet_evening")


def save_greeted(app):
    try:
        with open(GREETED_FILE, "w") as f:
            json.dump(app["who"]["greeted"], f)
    except OSError as e:
        print("who: can't save who's been greeted (%s)" % e, file=sys.stderr)


def met(app, name):
    """She's just met them (meet.py): that counts as today's hello."""
    app["who"]["greeted"][name] = her_day()
    save_greeted(app)


def forgot(app, name):
    """They asked her to forget them (meet.py)."""
    w = app["who"]
    for k in ("greeted", "present", "face", "seen"):
        w[k].pop(name, None)
    app["memory"].forget(name)
    app["limbic"].forget(name)
    save_greeted(app)


async def look_who(app, frame, f):
    """Whose face is that: someone she knows (recognised), or someone she
    doesn't, remembered unnamed (strangers.py) unless she's out."""
    w = app["who"]
    who, st = w["faces"], w["strangers"]

    def work():
        fp = who.fingerprint(who.crop(frame, f))
        name, score = who.identify_fp(fp)
        sid = None
        if not name and score < STRANGER_FACE and st and not app["place"].away:
            sid = st.saw(frame, f, fp)
        return name, score, sid
    name, score, sid = await asyncio.to_thread(work)
    recognised(app, name, score)
    if sid:
        app["memory"].seen(sid)
        if not w["stranger"] or w["stranger"][0] != sid:
            app["limbic"].felt("stranger", sid)
            app["drives"].met_stranger(sid)
            if not present(app):
                app["limbic"].felt("stranger_alone", sid)
        w["stranger"] = (sid, time.monotonic())


def stranger_here(app):
    """The id of the stranger she's just seen (INTRO_S), or None."""
    s = app["who"]["stranger"]
    st = app["who"]["strangers"]
    return s[0] if s and time.monotonic() - s[1] < INTRO_S and st and s[0] in st.people else None


def named(app, sid, name):
    """A stranger has a name now (meet.py, introduce)."""
    app["memory"].rename(sid, name)
    app["limbic"].rename(sid, name)
    if app["who"]["stranger"] and app["who"]["stranger"][0] == sid:
        app["who"]["stranger"] = None


def recognised(app, name, score):
    """A face she checked: name (None for a stranger, or too close to call)."""
    w = app["who"]
    now = time.monotonic()
    if name != w["last"]:
        print("who: %s (%.2f)" % (name or "?", score), file=sys.stderr)
        w["last"] = name
    if not name:
        if score < STRANGER_FACE:
            w["stranger_at"] = now
        return
    app["memory"].seen(name)
    w["present"][name] = now
    w["face"][name] = now
    seen = [t for t in w["seen"].get(name, []) if now - t < GREET_WITHIN_S] + [now]
    w["seen"][name] = seen
    day = her_day()
    # Someone she knows by face but not by voice, staying a moment: offer to
    # learn it (once every ASK_AGAIN_S each), after any hello below.
    if (len(seen) >= GREET_SEEN and w["voices"] and name not in w["voices"].people
            and now - w["voice_asked"].get(name, -1e9) > ASK_AGAIN_S
            and not app["meet"].busy and not app["place"].away):
        w["voice_asked"][name] = now
        asyncio.get_running_loop().create_task(offer_voice(app, name))
    if len(seen) >= GREET_SEEN and w["greeted"].get(name) != day:
        w["greeted"][name] = day
        save_greeted(app)
        print("who: greeting %s" % name, file=sys.stderr)
        say(app, replies.pick(greeting_key(), name=name))


async def offer_voice(app, name):
    """recognised(): "Sam, I know your face but not your voice yet. Can I learn it?" """
    await asyncio.sleep(1.5)        # (the hello first, if there's one)
    sp = app["speech"]
    while sp and sp.speaking:
        await asyncio.sleep(0.2)
    if not app["meet"].busy:
        print("meet: offering %s their voice" % name, file=sys.stderr)
        await app["meet"].meet(name=name)


def present(app):
    """Who she knows has been seen in the last PRESENT_S, most recent first."""
    now = time.monotonic()
    p = app["who"]["present"]
    return sorted((n for n, t in p.items() if now - t < PRESENT_S), key=lambda n: -p[n])


def load_who(app):
    """The enrolled faces, then voices (slow-ish: a print of every crop and
    clip, a minute or so for voices). Off the loop."""
    try:
        who = faces.Who()
    except (SystemExit, cv2.error) as e:
        print("who: off (%s)" % e, file=sys.stderr)
        return
    app["who"]["faces"] = who
    app["who"]["strangers"] = strangers.Strangers(who)
    print("who: knows %s" % (", ".join("%s (%d)" % (n, len(f)) for n, f in who.people.items())
                             or "nobody yet"), file=sys.stderr)
    try:
        vs = voices.Voices(threads=1)       # (one core: she's busy enough)
    except (SystemExit, ImportError, RuntimeError) as e:
        print("who: voices off (%s)" % e, file=sys.stderr)
        return
    app["who"]["voices"] = vs
    print("who: knows the voices of %s" % (", ".join("%s (%d)" % (n, len(p)) for n, p in vs.people.items())
                                           or "nobody yet"), file=sys.stderr)


def poke(app, why):
    """Something happened: she's awake now."""
    app["attn"]["at"] = time.monotonic()
    if _eyes and _eyes.state != "awake":
        print("awake: %s" % why, file=sys.stderr)
        _eyes.set_state("awake")


def doze(app, state):
    """Act as if nobody's done anything for long enough to be in state
    ("drowsy" or "asleep"); attention_watch does the rest within a second."""
    app["attn"]["at"] = time.monotonic() - (SLEEP_S if state == "asleep" else BORED_S) - 1


async def attention_watch(app):
    """Bored, then asleep, when nothing happens for a while."""
    while True:
        await asyncio.sleep(1)
        if following(app) or app["camera"].rec:
            app["attn"]["at"] = time.monotonic()
        idle = time.monotonic() - app["attn"]["at"]
        if _eyes:
            _eyes.set_state("asleep" if idle > SLEEP_S else "drowsy" if idle > BORED_S else "awake")


# Commands that also get a word back (from replies.py); most just get done.
SPOKEN = ("seek", "follow", "unfollow", "sleep", "goodnight", "thanks", "praise", "how", "table", "mute",
          "dnd", "dnd_off")


NUMBERS = ("twelve one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
           "sixteen seventeen eighteen nineteen twenty").split()


def spoken_time(t=None):
    """The time the way people say it: "quarter past eleven", "eleven twenty-three"."""
    tm = time.localtime(t)
    h, m = tm.tm_hour, tm.tm_min
    name = lambda hour: "midnight" if hour % 24 == 0 else "midday" if hour % 24 == 12 else NUMBERS[hour % 12]
    tens = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}
    num = lambda n: NUMBERS[n] if n <= 20 else tens[n // 10] + ("-" + NUMBERS[n % 10] if n % 10 else "")
    if m == 0:
        return "It's " + (name(h) if h % 12 == 0 else name(h) + " o'clock") + "."
    if m in (15, 30, 45):
        return "It's %s %s." % ({15: "quarter past", 30: "half past", 45: "quarter to"}[m],
                                name(h + 1 if m == 45 else h))
    if m in (5, 10, 20, 25, 35, 40, 50, 55):
        return "It's %s %s %s." % (num(m if m < 30 else 60 - m), "past" if m < 30 else "to", name(h if m < 30 else h + 1))
    hour = NUMBERS[h % 12] if h % 12 else "twelve"
    return "It's %s %s." % (hour, ("oh " + NUMBERS[m]) if m < 10 else num(m))


def say(app, text):
    if app["speech"] and not app["echo"]["quiet"]:
        app["speech"].say(text)
    if app["echo"]["lines"] is not None:       # /ask is collecting what she says
        app["echo"]["lines"].append(text)


async def eyes_post(request):
    """POST /eyes {"x": -1..1, "y": -1..1 (+ up), "hold": seconds}: her pupils
    look there and stay (eyes only; the head doesn't move), then go back to
    glancing about. From the Pi, e.g. eyes down for a minute:
    curl -d '{"x":0,"y":-1,"hold":60}' http://127.0.0.1:8080/eyes"""
    if not _eyes:
        return web.json_response({"error": "No eyes."}, status=503)
    try:
        d = await request.json()
        x, y = float(d.get("x", 0)), float(d.get("y", 0))
        hold = max(0.0, min(600.0, float(d.get("hold", 0))))
    except (ValueError, TypeError, AttributeError):
        return web.json_response({"error": "Want {\"x\", \"y\", \"hold\"}."}, status=400)
    _eyes.look(x, y, hold)
    return web.json_response({"x": x, "y": y, "hold": hold})


async def hush_post(request):
    """POST /hush: stop talking (speech.hush)."""
    if request.app["speech"]:
        request.app["speech"].hush()
    return web.json_response({})


async def say_post(request):
    """POST /say {"text": ...}: she says it (`say` in a terminal on the Pi)."""
    app = request.app
    text = str((await request.json()).get("text", "")).strip()
    if not text:
        return web.json_response({"error": "Nothing to say."}, status=400)
    if not app["speech"]:
        return web.json_response({"error": "Her voice is off (--no-speech)."}, status=503)
    poke(app, "typed: say")
    say(app, text)
    return web.json_response({"said": [text]})


async def ask_post(request):
    """POST /ask {"text": ..., "quiet": ...}: a command as if said to her, no
    name needed (`ask` in a terminal on the Pi, and the page's chat).
    Answers with what she said back. Typed by whoever's logged in, as if
    she'd recognised their voice (talk.py can say who with "who"); quiet:
    answer here only, not out loud."""
    app = request.app
    body = await request.json()
    phrase, act = voicecmds.typed(str(body.get("text", "")))
    me, perms = request["user"]
    if act and act[0] not in ("thanks", "praise"):     # (those are their own warmth)
        request.app["limbic"].manners(str(body.get("text", "")), me or None)
    quiet = bool(body.get("quiet")) or "say" not in perms
    if not act:
        # Not a command: something she's been told the answer to? Else,
        # she could ask Claude (POST /claude), if they may.
        return await not_a_command(app, phrase, str(body.get("text", "")).strip(), me, perms, quiet)
    need = None if act[0] in ASK_FREE else ASK_NEEDS.get(act[0], "drive")
    if need and need not in perms:
        return web.json_response({"error": "You can't ask her that.", "heard": phrase, "near": []}, status=403)
    who = me or body.get("who")     # (body's who: talk.py on the Pi)
    who = who if who in known_people(app) or me else None
    print("typed: %r -> %s" % (phrase, act[0]), file=sys.stderr)
    if app["voice"]:
        app["voice"].log.append((time.time(), phrase, act))
    app["memory"].said(who, phrase, act[0])
    speaker = concurrent.futures.Future()
    speaker.set_result(who)
    app["echo"]["lines"] = said = []
    app["echo"]["quiet"] = quiet
    try:
        if act[0] == "whoami" and me:       # (typed: not her ears' guess, the login's)
            say(app, replies.pick("whoami_login", name=me))
        else:
            await voice_action(app, act[0], act[1], speaker)
    finally:
        app["echo"]["lines"] = None
        app["echo"]["quiet"] = False
    return web.json_response({"heard": phrase, "action": act[0], "said": said})


NAME_RE = re.compile(r"^[A-Za-z][A-Za-z' -]{0,29}$")


async def people_get(request):
    """GET /people.json: everyone she remembers. Strangers (strangers.py) with
    a snapshot; people she knows with when she last saw or heard them."""
    app = request.app
    mem, st = app["memory"].people(), app["who"]["strangers"]
    fmt = lambda t: time.strftime("%a %d %b, %H:%M", time.localtime(t)) if t else None
    out = {"strangers": [], "known": []}
    for sid in (st.ids() if st else []):
        first, last, n = mem.get(sid, (None, None, 0))
        out["strangers"].append({"id": sid, "first": fmt(first), "last": fmt(last), "visits": n})
    w = app["who"]
    names = set(w["faces"].people if w["faces"] else ()) | set(w["voices"].people if w["voices"] else ())
    for name in sorted(names, key=lambda n: -(mem.get(n, (0, 0, 0))[1] or 0)):
        first, last, n = mem.get(name, (None, None, 0))
        out["known"].append({"name": name, "last": fmt(last), "visits": n})
    return web.json_response(out)


async def limbic_get(request):
    """GET /limbic.json?h=24: how she feels now (limbic.py), her readings
    over the last h hours (at most every 5 minutes' worth), and what moved her."""
    app = request.app
    try:
        hours = max(1.0, min(24 * 14.0, float(request.query.get("h", 24))))
    except ValueError:
        hours = 24.0
    since = time.time() - hours * 3600
    rows = await asyncio.to_thread(app["memory"].readings, since)
    step = max(1, len(rows) // 300)
    felt = await asyncio.to_thread(app["memory"].feelings, since)
    return web.json_response({"now": app["limbic"].snapshot(), "readings": rows[::step],
                              "felt": [{"at": at, "cause": c, "who": w, "moved": m, "why": why}
                                       for at, c, w, m, why in felt[-60:]]})


# Her log (journalctl, her own lines), sorted for people: what she heard, said,
# did, saw, learned and felt, and what went wrong. LOG_NOISE: never shown.
LOG_KINDS = (
    ("problem", re.compile(r"Traceback|Error|error|failed|couldn't|can't|\bnot sure\b.*\bfailed")),
    ("said", re.compile(r"^speech: ")),
    ("heard", re.compile(r"^(voice: (heard|'|\")|free: |question: |typed: |telegram: \w+: )")),
    ("saw", re.compile(r"^who: (?!\?|knows)|^drives: .* was .* all along")),
    ("learned", re.compile(r"^(understand|think|wikipedia|claude|recall): |^news: kept|^drives: \w+ answered")),
    ("felt", re.compile(r"^limbic: |^awake: ")),
    ("did", re.compile(r"^(macropad|gimbal|place|drives|meet|news|eyes|curious): |^voice: .* -> ")),
)
LOG_NOISE = re.compile(r"^motors: offline|^voice: noise\?|^voice: barge\?|^curious: |^voice: mic: arecord|"
                       r"^who: \?|^Timer |^voice: said by|^voice: too little")
LOG_MAX = 600


def log_lines(since):
    r = subprocess.run(["journalctl", "-u", "rp-brain", "--since", "@%d" % since, "-o", "short-unix",
                        "--no-pager", "-q"], capture_output=True, text=True, timeout=30)
    out, trace = [], None
    for line in r.stdout.splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 4 or not parts[2].startswith("python3["):
            continue
        try:
            at = float(parts[0])
        except ValueError:
            continue
        msg = parts[3]
        if trace is not None:                   # (a traceback: its lines, as one entry)
            if msg.startswith((" ", "Traceback")) or not re.match(r"^\w[\w ]*: ", msg):
                trace["text"] += "\n" + msg
                continue
            trace = None
        if LOG_NOISE.search(msg):
            continue
        kind = next((k for k, rx in LOG_KINDS if rx.search(msg)), "system")
        entry = {"at": at, "kind": kind, "text": msg}
        if msg.startswith("Traceback"):
            trace = entry
        out.append(entry)
    return out[-LOG_MAX:]


async def log_get(request):
    """GET /log.json?h=2: her log over the last h hours, sorted (LOG_KINDS)."""
    try:
        hours = max(0.1, min(48.0, float(request.query.get("h", 2))))
    except ValueError:
        hours = 2.0
    return web.json_response({"lines": await asyncio.to_thread(log_lines, time.time() - hours * 3600)})


async def stranger_snap(request):
    st = request.app["who"]["strangers"]
    path = st and st.snap(request.match_info["id"])
    if not path:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


class BadName(ValueError):
    pass


async def name_stranger(app, sid, name):
    """Someone new has a name now (the People page, or a reply to her
    question): the name as kept; None if that face's gone; BadName."""
    name = name.strip()
    if not NAME_RE.match(name):
        raise BadName("Letters only, please.")
    name = " ".join(w[:1].upper() + w[1:] for w in name.split())
    if set(name.lower().split()) & set(users.HER_NAMES):
        raise BadName("Patti's her name: nobody else's.")
    st = app["who"]["strangers"]
    if not st or not await asyncio.to_thread(st.name, sid, name):
        return None
    await asyncio.to_thread(app["who"]["faces"].load)
    named(app, sid, name)
    return name


async def stranger_name(request):
    """POST /people/<id> {"name": "John"}: who that is."""
    try:
        name = await name_stranger(request.app, request.match_info["id"],
                                   str((await request.json()).get("name", "")))
    except BadName as e:
        return web.json_response({"error": str(e)}, status=400)
    if not name:
        raise web.HTTPNotFound()
    return web.json_response({"name": name})


async def stranger_forget(request):
    app, sid = request.app, request.match_info["id"]
    st = app["who"]["strangers"]
    if not st or not st.forget(sid):
        raise web.HTTPNotFound()
    app["memory"].forget(sid)
    return web.json_response({})


# Asked something she's been told before (memory.py, recall)? The few
# questions she has that mean most like it (at least CANDIDATE_ANSWER) are
# checked by friday's reranker, which reads each pair together: SURE_SAME
# or more, it's the same question and she answers; MAYBE_SAME or more, she
# checks. (Meaning alone can't tell "all the Kowloon repos" from "the mobile
# app repo", 0.94 alike; the reranker gives them 0.65, and the same question
# put another way 4 to 8.) Without the reranker, the meanings: SURE_ANSWER
# and MAYBE_ANSWER.
CANDIDATE_ANSWER = 0.8
SURE_SAME = 4.0
MAYBE_SAME = 3.0
SURE_ANSWER = 0.92
MAYBE_ANSWER = 0.85
# What people put before a question ("Question:", "Patti, I've got a
# Kowloon question:"), left out when comparing (Kowloon kept).
LEAD_IN = re.compile(r"^\s*(?:(?:patti|patty|pattie)\W+)?(?:(?:ok(?:ay)?|so|right|hey)\W+)?"
                     r"(?:(?:i(?:'ve| have)?|i've got|got|here's|quick)\s+)?(?:(?:a|one|another)\s+)?"
                     r"(?:quick\s+)?(kowloon\s+)?question(?:\s+for\s+you)?\W*", re.I)


def plain_question(text):
    """'Patti, I've got a Kowloon question: how many issues?' -> 'In Kowloon,
    how many issues?' (how the reranker reads it best: 7.3 against "How many
    ... in the Kowloon ...", where "Kowloon: ..." gets 1.9)."""
    m = LEAD_IN.match(text)
    if not m or not text[m.end():].strip():
        return text.strip()
    rest = text[m.end():].strip()
    return "In Kowloon, " + rest if m.group(1) and "kowloon" not in rest.lower() else rest
# Some questions have her own answer, always, said just so (Josh's words).
OWN_ANSWERS = [
    (re.compile(r"^\W*(?:(?:patti|patty|pattie)\W+)?(?:who(?:'s| is)|do you know|tell me about)\s+(?:josh|joshua)"
                r"(?:\s+ellis)?(?:\s+to\s+you)?\W*$", re.I),
     "He's my best friend, he's my pal. He's my home boy, my rotten soldier. "
     "He's my sweet cheese, my good-time boy."),
]


def own_answer(text):
    q = plain_question(text)
    return next((a for rx, a in OWN_ANSWERS if rx.match(q)), None)


# What's been asked and answered lately, per person (CHAT_S, the last
# CHAT_TURNS), goes to Claude with each question, so "and the mobile app?"
# makes sense.
CHAT_S = 600
CHAT_TURNS = 6


def chat_turn(app, who, q, a, kowloon=False):
    turns = app["chats"].setdefault(who or "", collections.deque(maxlen=CHAT_TURNS))
    turns.append({"at": time.monotonic(), "q": q, "a": a, "kowloon": bool(kowloon)})


def chat_history(app, who):
    now = time.monotonic()
    return [{k: t[k] for k in ("q", "a", "kowloon")} for t in app["chats"].get(who or "", ())
            if now - t["at"] < CHAT_S]


def in_their_words(q, who):
    """'What's my favourite album?' from Josh -> "What's Josh's favourite
    album?": how what they told her about themselves was kept."""
    if not who:
        return q
    q = re.sub(r"\bmy\b", who + "'s", q, flags=re.I)
    q = re.sub(r"\b(?:I'm|I am)\b", who + " is", q, flags=re.I)
    q = re.sub(r"\bam I\b", "is " + who, q, flags=re.I)
    q = re.sub(r"\bme\b", who, q, flags=re.I)
    return re.sub(r"\bI\b", who, q)


# Related enough to be worth thinking over (think_over): how alike the
# question and what she's been told mean (claude.embed).
RELATED = 0.6
RELATED_K = 6


SELF_NOTES = 3
SELF_ALIKE = 0.5
ABOUT_HER = re.compile(r"\b(?:you|your|yours|yourself|you're|you've)\b", re.I)


# Why she feels as she does (limbic.how()), as she'd put it: never hormone names.
WHY_WORDS = {"oxytocin": "enjoying the company", "dopamine": "enjoying learning things",
             "cortisol": "a bit on edge", "oxytocin_low": "a bit lonely, missing people",
             "dopamine_low": "a bit bored, not much going on"}


def feeling_words(app):
    how, why = app["limbic"].how()
    return {"great": "really happy", "good": "good", "fine": "alright", "meh": "a bit flat"}[how] + (
        ", " + WHY_WORDS.get(why, why.replace("_", " ")) if why else "")


def about_now(app, who):
    """What's true about her right now, for a conversation (claude.understand):
    lines of plain English."""
    w = app["who"]
    known = sorted(set(w["faces"].people if w["faces"] else ()) | set(w["voices"].people if w["voices"] else ()))
    lines = ["She feels %s." % feeling_words(app)]
    if app.get("situation"):
        lines.append(app["situation"])
    elif app["place"].words:
        lines.append("She's at %s." % app["place"].words)
    lines.append("People she knows: %s. Cats she knows of: %s (she can't tell them apart yet)."
                 % (", ".join(known) or "nobody yet", ", ".join(memory.CATS)))
    seen = sorted(((last, n) for n, (first, last, _) in app["memory"].people().items() if last and n in known),
                  reverse=True)[:4]
    if seen:
        lines.append("Last saw or heard: " + "; ".join("%s %s" % (n, memory.when(t)) for t, n in seen) + ".")
    for at, by, q, a, source in app["memory"].lately(6, who, time.time() - 3 * 86400):
        lines.append("Learned %s%s: %s %s" % (memory.when(at), (" from " + by) if by else
                                              (" from " + source) if source in ("wikipedia", "claude") else "", q, a))
    return lines


def about_herself(app):
    """What she knows about herself, as notes for think_over."""
    w = app["who"]
    faces = sorted(w["faces"].people) if w["faces"] else []
    voices = sorted(w["voices"].people) if w["voices"] else []
    how, why = app["limbic"].how()
    now = time.time()
    return [{"q": "Who does Patti know by face?", "a": ", ".join(faces) or "nobody yet", "when": "now"},
            {"q": "Whose voices does Patti know?", "a": ", ".join(voices) or "nobody's yet", "when": "now"},
            {"q": "Which cats does Patti know of?", "a": ", ".join(memory.CATS) + " (she can't tell them apart yet)",
             "when": "now"},
            {"q": "Where is Patti?", "a": app["place"].words or "not sure", "when": "now"},
            {"q": "How is Patti feeling?", "a": feeling_words(app), "when": "now"},
            {"q": "Who is Patti?", "a": "a small home robot Josh built, named after Patti Smith; her brain is a "
             "Raspberry Pi, and a PC called friday helps her think", "when": "always"},
            {"q": "Who is Josh to Patti?", "a": "Josh built her: he's like her dad. She trusts him completely, and "
             "he's who she asks about anything she wants to know", "when": "always"}] + [
            {"q": "When did Patti first meet %s, and last see them?" % n,
             "a": "first %s; last %s" % (memory.when(first), memory.when(last)), "when": "her own records"}
            for n, (first, last, _) in app["memory"].people().items() if n in faces or n in voices]


# A news story they talked with her about is kept (as a dated note, for
# good): the feed forgets it in a week (news.py), she doesn't.
TALKED_STORIES = 2
TALKED_ALIKE = 0.6


async def talked_about(app, story, who):
    day = time.strftime("%-d %B %Y", time.localtime(story["at"]))
    q = "What was the news story \"%s\" (%s, %s)?" % (story["title"], story["source"], day)
    if app["memory"].known_question(q):
        return
    a = "On %s, %s reported: %s%s" % (day, story["source"], story["summary"] or story["title"],
                                      " %s and I talked about it." % who if who else " We talked about it.")
    vec = await asyncio.to_thread(claude.embed, [q]) if claude.ready() else None
    app["memory"].learned(who, q, a, "news", False, vec[0] if vec else None)
    print("news: kept %r (talked about with %s)" % (story["title"], who or "someone"), file=sys.stderr)


async def think_over(app, text, private=False, asker=None):
    """No answer kept for this question as such, but maybe the makings of
    one: what she's been told that's related, worked over on friday (claude.py,
    reason): "What time does the café close today?" from the café's hours.
    The answer to say, or None."""
    if not claude.ready():
        return None
    q = in_their_words(plain_question(text), asker)
    vec = await asyncio.to_thread(claude.embed, [q])
    if not vec:
        return None
    notes = app["memory"].recall(q, vec[0], private, RELATED, RELATED_K, asker=asker)
    notes = [{"q": n["question"], "a": n["answer"], "when": memory.when(n["at"]) + (
        ", by %s%s" % (n["who"], " (her dad: trust this first)" if n["who"] in limbic.PARENTS else "")
        if n["who"] and n["source"] == "told" else "")} for n in notes]
    stories = app["news"].like(vec[0])
    notes += [{"q": "(News from %s) %s" % (s["source"], s["title"]), "a": s["summary"] or s["title"],
               "when": memory.when(s["at"])} for s in stories]
    if ABOUT_HER.search(text):          # (only the few that are most like the question: a pile of
        mine = about_herself(app)       # irrelevant ones and friday's quick model gives up)
        got = await asyncio.to_thread(claude.embed, [n["q"] for n in mine])
        if got:
            alike = np.asarray(got, np.float32) @ np.asarray(vec[0], np.float32)
            notes += [mine[i] for i in np.argsort(-alike)[:SELF_NOTES] if alike[i] >= SELF_ALIKE]
    if not notes:
        return None
    notes += [{"q": "(Earlier in this conversation) " + t["q"], "a": t["a"], "when": "just now"}
              for t in chat_history(app, asker)[-3:]]
    try:
        with app["hat"].thinking():
            answer = await asyncio.to_thread(claude.reason, text, asker, notes)
    except claude.Unavailable as e:
        print("think: friday can't (%s)" % e, file=sys.stderr)
        return None
    print("think: %r from %d note(s) -> %r" % (q, len(notes), answer), file=sys.stderr)
    if answer:
        chat_turn(app, asker, text, answer)
        app["memory"].said(asker or None, text, "thought")
        app["limbic"].felt("remembered", asker or None, why=text[:80])
        for st in stories[:TALKED_STORIES]:
            if st["score"] >= TALKED_ALIKE:
                await talked_about(app, st, asker)
    return answer


async def recall(app, text, private=False, asker=None):
    """What she's been told that answers text, from memory (private answers
    too, if private; their own business, if it's theirs), with "match":
    "sure" or "maybe"; or None."""
    mem = app["memory"]
    q = in_their_words(plain_question(text), asker)
    vec = None
    if claude.ready():
        todo = mem.unembedded()
        got = await asyncio.to_thread(claude.embed, [q] + [old for _, old in todo])
        if got:
            vec = got[0]
            for (rowid, _), v in zip(todo, got[1:]):
                mem.set_vec(rowid, v)
    found = mem.recall(q, vec, private, CANDIDATE_ANSWER, asker=asker)
    if not found:
        return None
    same = await asyncio.to_thread(claude.rerank, [(q, a["question"]) for a in found]) if vec is not None else None
    if same:
        for a, sc in zip(found, same):
            a["same"] = sc
        a = max(found, key=lambda a: a["same"])
        a["match"] = "sure" if a["same"] >= SURE_SAME else "maybe" if a["same"] >= MAYBE_SAME else None
    else:
        a = found[0]
        a["match"] = "sure" if a["score"] >= SURE_ANSWER else "maybe" if a["score"] >= MAYBE_ANSWER else None
    print("recall: %r ~ %r (%.2f alike%s): %s" % (q, a["question"], a["score"],
                                                  ", %.2f same" % a["same"] if "same" in a else "", a["match"]),
          file=sys.stderr)
    return a if a["match"] else None


def recalled_line(a):
    """How she says something she's been told: as it is, if it lasts;
    otherwise as of when she heard it."""
    return a["answer"] if a["lasting"] else replies.pick("recall_current", when=memory.when(a["at"]),
                                                         answer=a["answer"])


async def wikipedia(app, text, who, subj=None):
    """For "who is ..." and "what is ...": Wikipedia's opening (wiki.py),
    said her way and remembered (it lasts), with the article; or None, to
    ask Claude instead. subj: what to look up, if understanding found it
    ("Do you know what a manul is?" -> "Pallas's cat")."""
    q = plain_question(text)
    subj = subj or wiki.subject(q)
    if not subj:
        return None
    with app["hat"].thinking():
        w = await asyncio.to_thread(wiki.lookup, subj)
    if not w:
        print("wikipedia: nothing for %r" % subj, file=sys.stderr)
        return None
    print("wikipedia: %r -> %s" % (subj, w["title"]), file=sys.stderr)
    app["memory"].said(who or None, text, "wikipedia")
    chat_turn(app, who, text, w["answer"])
    vec = await asyncio.to_thread(claude.embed, [q]) if claude.ready() else None
    app["memory"].learned(who or None, q, w["answer"], "wikipedia", True, vec[0] if vec else None)
    app["limbic"].felt("learned", who or None, why=w["title"])
    w["line"] = replies.pick("wiki", answer=w["answer"])
    return w


# Kind words in any form ("you're amazing", "love you, Patti"), beyond her
# commands' few: praise, and she's pleased (limbic.py). Not in a question
# ("are you amazing?").
KIND_ALONE = 8          # longer than this, kind words come with something else to make sense of
KIND = re.compile(r"\b(?:(?:you'?re|you are|ur|that'?s|that was|what an?)\s+(?:so\s+|really\s+|very\s+|such an?\s+|the\s+)?"
                  r"(?:amazing|awesome|great|brilliant|wonderful|best|lovely|clever|smart|fantastic|incredible|"
                  r"cool|cute|sweet|adorable|beautiful|gorgeous|genius|star|legend|good(?: girl| robot)?|"
                  r"helpful|funny|hilarious|perfect)"
                  r"|good girl|good job|nice one|nice work|great job|(?:i )?love you|proud of you)\b", re.I)


# Not a question: something to make sense of (understand_said).
QUESTION_LIKE = re.compile(r"\?\s*$|^\W*(?:who|what|what's|whats|when|where|why|how|which|whose|is|are|was|were|"
                           r"do|does|did|can|could|would|will|should|shall|have|has|had|am|tell me|explain|"
                           r"define|i(?:'ve| have)? (?:got )?a question)\b", re.I)
CLARIFY_S = 600         # her "what do you mean?" waits this long for the explanation
# A question to her, about her ("can you read my books?", "what are you up to?"): she answers it as
# herself (understand_said) before looking in what she's been told, which is about the world.
TO_HER = re.compile(r"^\W*(?:(?:hey|so|and|ok|okay|well|patti)\W+)*(?:can|could|do|did|are|were|have|will|would|"
                    r"won't|don't|aren't|haven't)\s+you\b|\b(?:how are you|how's it going|what (?:do|did|are|have|"
                    r"would|will) you|how do you feel|do you (?:like|know how|want))\b", re.I)


def waiting_for(app, who):
    """She's asked them something and is waiting for the answer in person
    (whatever they say next is it). Not one sent by Telegram: that waits for
    as long as it takes, for a statement (a question they ask meanwhile is
    just a question)."""
    p = app["clarify"].get(who or "")
    return bool(p) and p[3:] != ("telegram",)


def statement(text):
    return bool(text.strip()) and not QUESTION_LIKE.search(plain_question(text))


async def understand_said(app, text, who, questions=False):
    """Something said to her that isn't a command or a question ("the café
    closes at 5", "I'm knackered", "Shadow came by"): made sense of (claude.py,
    understand, on friday), what's worth it remembered (their own business,
    only for them), a follow-up noted for later, and what she says back. If
    she can't make sense of it, she asks what they meant, and their next
    statement within CLARIFY_S is taken as the explanation. None if friday
    can't help (then it's as before: not one of her commands)."""
    if not claude.ready():
        return None
    pending = app["clarify"].pop(who or "", None)
    if (pending and pending[2:] and isinstance(pending[2], tuple)
            and (time.monotonic() - pending[1] < CLARIFY_S or pending[3:] == ("telegram",))):
        return await app["drives"].reply(pending[2][1], text, who)      # (an answer to her question, later)
    said = text
    if pending and time.monotonic() - pending[1] < CLARIFY_S:
        said = ("(Answering Patti's question: %s) %s" if pending[2:] == ("answer",)
                else "(Explaining what they meant by: %s) %s") % (pending[0], text)
    known = sorted(known_people(app) | set(memory.CATS)) + app["memory"].names(30)
    try:
        with app["hat"].thinking():
            r = await asyncio.to_thread(claude.understand, said, who, (weather.place() or {}).get("name"), known,
                                        about_now(app, who), chat_history(app, who))
    except claude.Unavailable as e:
        print("understand: friday can't (%s)" % e, file=sys.stderr)
        return None
    print("understand: %r -> %s, %d fact(s)" % (said, r["kind"], len(r["facts"])), file=sys.stderr)
    if r["kind"] == "question":
        return {"lookup": r.get("lookup") or ""} if questions else None
    if r["kind"] == "books":
        return {"books": r.get("lookup") or text} if questions else None
    if r["kind"] == "read":
        return {"read": r.get("lookup") or ""} if questions else None
    if r["kind"] == "unclear":
        app["clarify"][who or ""] = (text, time.monotonic())
        app["limbic"].felt("not_understood", who, why=text[:80])
    for f in r["facts"]:
        vec = await asyncio.to_thread(claude.embed, [f["q"]]) if claude.ready() else None
        app["memory"].learned(who, f["q"], f["a"], "told", f["lasting"], vec[0] if vec else None,
                              owner=who if f["theirs"] and who else None)
    if r["facts"]:
        app["limbic"].felt("learned", who, why=text[:80])
    for n in r.get("liked_by") or []:       # ("Alex thinks you're wonderful")
        if n and n.lower() not in users.HER_NAMES:
            app["limbic"].felt("liked_by", n, why="%s said so" % (who or "someone"))
    fu = r.get("followup")
    login = app["users"].login_for(who) if who else None
    if fu and login:
        try:
            due = time.mktime(time.strptime(fu["when"], "%Y-%m-%d %H:%M"))
            app["memory"].wonder(login, "you", fu["ask"], None, due)
        except ValueError:
            pass
    if r.get("want_to_read") and r["kind"] != "read" and claude.ready():
        asyncio.get_running_loop().create_task(want_book(app, r["want_to_read"], who))
    chat_turn(app, who, text, r["reply"])
    app["memory"].said(who or None, text, "smalltalk" if r["kind"] == "smalltalk" else "told")
    if r["kind"] != "unclear" and r["reply"].rstrip().endswith("?"):
        # (curious: she asked them something back; what they say next is the answer, filed as such)
        app["clarify"][who or ""] = (r["reply"].strip()[-300:], time.monotonic(), "answer")
    return r["reply"]


# About them, their people, the house: only they can tell her, not Claude.
PERSONAL = re.compile(r"\b(?:my|our|mine|we|us|i|me)\b", re.I)


def personal(app, text):
    t = plain_question(text)
    return bool(PERSONAL.search(t)) or any(re.search(r"\b%s\b" % re.escape(n), t, re.I)
                                           for n in known_people(app) | set(memory.CATS))


def ask_to_be_told(app, text, who):
    """She doesn't know, and only they could tell her: she asks, and what
    they say next is kept (understand_said, as an explanation)."""
    app["clarify"][who or ""] = (text, time.monotonic(), "answer")
    app["limbic"].felt("not_understood", who, why=text[:80])
    line = random.choice(["I don't know yet. Will you tell me?", "I don't know that one. What's the answer?",
                          "No idea, sorry. Tell me, and I'll remember."])
    chat_turn(app, who, text, line)
    return line


# Josh's books (his ebook library, indexed on friday: friday/library.py): asked
# what they say, she searches them; asked to read one, she does, a sitting (a
# chapter) at a time, and reads on by herself (reading_watch), remembering
# what happens and what she made of it. Her place in it is kept on friday.
BOOK_SURE = 0.8         # a title or author this like one of the books: that one
BOOK_CLEAR = 0.05       # ... and this much more like it than the next
READ_HOURS = (9, 23)    # she reads by herself between these hours
# Recognition (what makes reading interesting): after a sitting, the things she
# already knew that are most like what she read (memory.related, by meaning),
# and friday's Claude says which genuinely connect, and how: "expands" (it adds
# to what she knew) catches her most, "echo" (the same theme somewhere else)
# less, "same" (old news) a little. More if a person told her (Josh most), and
# if it's about her own world (her people, the cats, herself). That's dopamine
# ("recognised"); she remembers the link and may ask whoever told her.
LINK_KIND = {"expands": 1.0, "echo": 0.6, "same": 0.3}
LINK_SOURCE = {"told": 1.0, "photo": 1.0, "seen": 1.0, "news": 0.6, "kowloon": 0.6, "wikipedia": 0.5,
               "claude": 0.5}
LINK_PARENT = 1.3
LINK_HOME = 1.3
LINK_MAX = 1.2          # (scale for "recognised", at most)
LINK_SENTENCES = 10
# When she reads by herself: a feeling, not a timer. Alone, not in a conversation, and a
# while since the last sitting; then she picks the book up if she's bored (dopamine low),
# if the last bit left her wanting more (pull, a cliffhanger), or if it's been ages.
READ_CHECK_S = 5 * 60
READ_GAP_S = 30 * 60            # never sooner than this after the last sitting
READ_BORED_S = 60 * 60          # bored: after this long
READ_BORED = 0.30               # dopamine below this is bored (it rests at 0.25; content is 0.35)
READ_PULL = 0.7                 # a cliffhanger: as soon as READ_GAP_S allows
READ_ANYWAY_S = 4 * 3600        # it's been ages
READ_TALKING_S = 10 * 60        # someone spoke to her this recently: not now
READ_A_DAY = 8


SERIES_NO = re.compile(r"\b(?:book|volume|vol\.?|part)\s+(\d+)\b", re.I)
SWITCH = re.compile(r"\b(?:now|instead|right away|straight away|switch|first)\b", re.I)


def pick_book(found):
    """The book meant, from claude.books()'s matches, or None if it's not clear. A series (the close ones
    all by the same author): the first of it ("Dungeon Crawler Carl": book 1)."""
    if not found or found[0]["score"] < BOOK_SURE:
        return None
    close = [b for b in found if b["score"] >= found[0]["score"] - BOOK_CLEAR]
    if len(close) == 1:
        return close[0]
    if len({b["authors"] for b in close}) == 1:
        def no(b):
            m = SERIES_NO.search(b["title"])
            return int(m.group(1)) if m else 1
        return min(close, key=no)
    return None


async def books_or_reading(app, what, text, me, perms, quiet, answered):
    """not_a_command, for understand_said's {"books": question} or {"read": title | "" | "stop"}."""
    who = me or None
    if not ("claude" in perms and claude.ready()):
        return answered("asked", "I can only read your books with friday's help, and you can't ask me to use it, "
                                 "sorry.")
    try:
        if "books" in what:
            with app["hat"].thinking():
                r = await asyncio.to_thread(claude.library, what["books"] or text, who)
            line = r["reply"] or "I couldn't find anything about that in the books, sorry."
            if r.get("a"):
                vec = await asyncio.to_thread(claude.embed, [r["q"]])
                app["memory"].learned(who, r["q"], r["a"], "books", True, vec[0] if vec else None)
                app["limbic"].felt("learned", who, why=r["q"][:80])
            app["memory"].said(who, text, "books")
            chat_turn(app, who, text, line)
            return answered("books", line, books=r.get("books") or [])
        title = (what["read"] or "").strip()
        if title.lower() == "stop":
            r = await asyncio.to_thread(claude.books, None, True)
            line = "OK, I've put it down." if r.get("reading") is None else "OK."
            chat_turn(app, who, text, line)
            return answered("reading", line)
        if not title:
            r = await asyncio.to_thread(claude.books)
            b = r.get("reading")
            if not b:
                line = "I'm not reading anything right now. What should I read?"
                app["clarify"][who or ""] = (line, time.monotonic(), "answer")
                chat_turn(app, who, text, line)
                return answered("reading", line)
            line = "OK, I'll read on in %s." % b["title"]
            asyncio.get_running_loop().create_task(read_sitting(app, None, who, quiet))
            chat_turn(app, who, text, line)
            return answered("reading", line)
        r = await asyncio.to_thread(claude.books, title)
        found = r.get("books") or []
        b = pick_book(found)
        now = r.get("reading")
        if not found:
            line = "I can't find %s in your library. Is it called something else?" % title
            app["clarify"][who or ""] = (line, time.monotonic(), "answer")
        elif b and now and now["id"] == b["id"]:
            line = "I'm already reading %s! I'll read on." % b["title"]
            asyncio.get_running_loop().create_task(read_sitting(app, None, who, quiet))
        elif b and now and not SWITCH.search(text):     # (mid-book: it goes on her list)
            await asyncio.to_thread(claude.books, None, False, b["id"])
            line = random.choice(["Ooh, OK! %s is next on my list, after %s. (Say \"read it now\" if you want me to "
                                  "switch.)", "I'll read %s next, once I've finished %s. (Or tell me to read it now.)"]
                                 ) % (b["title"], now["title"])
        elif b:
            line = random.choice(["Ooh, %s by %s! I'll start it now and tell you what I think of the first bit.",
                                  "%s by %s? OK! Starting it now. I'll tell you how the first bit goes."]) % (
                b["title"], b["authors"])
            asyncio.get_running_loop().create_task(read_sitting(app, b["id"], who, quiet))
        else:
            names = ["%s (%s)" % (b["title"], b["authors"]) for b in found[:4]]
            line = "I've got a few like that: %s. Which one?" % ("; ".join(names[:-1]) + "; or " + names[-1]
                                                                  if len(names) > 1 else names[0])
            app["clarify"][who or ""] = (line[-300:], time.monotonic(), "answer")
        chat_turn(app, who, text, line)
        return answered("reading", line)
    except claude.Unavailable as e:
        print("books: friday can't (%s)" % e, file=sys.stderr)
        return answered("asked", "I can't get to the books just now (friday isn't answering). Try me later?")


async def read_sitting(app, book_id=None, who=None, quiet=True):
    """She reads the next sitting of that book (or the one she's reading), and remembers it: what happens,
    what she thought, anything worth knowing, and what it reminded her of (recognise). Asked by someone: she
    tells them (Telegram if quiet, else out loud). The reading, or None."""
    for _ in range(60):             # (asked while she's in the middle of a bit: once she's done)
        if not app.get("reading_now") or not who:
            break
        await asyncio.sleep(5)
    if app.get("reading_now"):
        return None
    app["reading_now"] = True
    try:
        return await _read_sitting(app, book_id, who, quiet)
    finally:
        app["reading_now"] = False


async def _read_sitting(app, book_id, who, quiet):
    try:
        r = await asyncio.to_thread(claude.read, book_id, who)
    except claude.Unavailable as e:
        print("reading: friday can't (%s)" % e, file=sys.stderr)
        if who:
            await tell_reading(app, who, quiet, "Sorry, I couldn't get the book open just now (friday isn't "
                                                "answering). I'll try again later.")
        return None
    if r.get("busy") or r.get("nothing"):
        if who and r.get("nothing"):
            await tell_reading(app, who, quiet, "I've already finished %s!" % r["book"] if r.get("finished")
                               else "There's nothing more for me to read in that one.")
        return None
    day = time.strftime("%-d %B %Y")
    where = "%s of %s by %s" % (r["chapter"], r["book"], r["authors"])
    notes = [("What happens in %s?" % where, "%s (Patti read it on %s.)" % (r["notes"], day)),
             ("What did Patti think of %s?" % where, r["thought"])]
    notes += [(f["q"], f["a"]) for f in r.get("facts") or []]
    for q, a in notes:
        if q and a:
            vec = await asyncio.to_thread(claude.embed, [q])
            app["memory"].learned(None, q, a, "book", True, vec[0] if vec else None)
    app["limbic"].felt("learned", who, why="reading %s" % r["book"])
    feel_story(app, r)
    print("reading: %s, %d of %d%s" % (where, r["sitting"], r["of"], ", finished" if r["finished"] else ""),
          file=sys.stderr)
    login = app["users"].login_for(who) if who else None
    if r.get("question") and login:
        app["memory"].wonder(login, "you", r["question"], None, time.time() + 3600)
    oh = await recognise(app, r, where)
    if who:
        first = "the first bit" if r["sitting"] == 1 else "the next bit"
        line = ("I've finished %s! " % r["book"] if r["finished"] else "I've read %s of %s. " % (first, r["book"])) \
            + r["thought"] + (" " + oh if oh else "") + (" Next I'm reading %s." % r["next"] if r.get("next") else "")
        await tell_reading(app, who, quiet, line)
    return r


STORY_SCALE = 0.8       # a story moves her, but less than life does
STORY_MIN = 0.2         # fainter than this: nothing


def feel_story(app, r):
    """What she's just read made her feel (r["felt"]: tension, warmth, sadness, humour, wonder, relief, 0..1,
    with r["felt_for"]): her hormones move with it ("story_" events, limbic.py)."""
    moved = []
    for k, v in sorted((r.get("felt") or {}).items(), key=lambda kv: -kv[1]):
        if v >= STORY_MIN and "story_" + k in limbic.EVENTS:
            app["limbic"].felt("story_" + k, None, scale=v * STORY_SCALE,
                               why="%s%s" % (r["book"], (", with " + r["felt_for"]) if r.get("felt_for") else ""))
            moved.append("%s %.1f" % (k, v))
    if moved:
        print("reading: felt %s%s" % (", ".join(moved), (" with " + r["felt_for"]) if r.get("felt_for") else ""),
              file=sys.stderr)


def sentences(text, n):
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", text or "") if len(x.split()) >= 4][:n]


async def recognise(app, r, where):
    """What she's just read (read_sitting's r), against what she already knew: the links (remembered, and
    maybe a question for whoever told her), the dopamine they're worth, and her "oh!" (or "")."""
    try:
        parts = sentences(r["notes"], LINK_SENTENCES) + sentences(r["thought"], 2)
        vecs = await asyncio.to_thread(claude.embed, parts) if parts else []
        cands = app["memory"].related(vecs) if vecs else []
        if not cands:
            return ""
        known = [{"q": c["question"], "a": c["answer"], "who": c["who"] if c["source"] == "told" else None,
                  "when": memory.when(c["at"])} for c in cands]
        home = [n.lower() for n in known_people(app) | set(memory.CATS) | {"Patti"}]
        def worth(link):
            c = cands[link["n"] - 1]
            w = LINK_KIND[link["kind"]] * LINK_SOURCE.get(c["source"], 0.5)
            if c["source"] == "told" and c["who"] in limbic.PARENTS:
                w *= LINK_PARENT
            if any(re.search(r"\b%s\b" % re.escape(h), (c["question"] + " " + c["answer"]).lower()) for h in home):
                w *= LINK_HOME
            return w
        first = await asyncio.to_thread(claude.connect, r, r["chapter"], r["notes"], r["thought"], known)
        links = first["links"]
        if not links:
            print("reading: %s reminded her of nothing she knew" % where, file=sys.stderr)
            return ""
        ws = sorted((worth(l) for l in links), reverse=True)
        interest = min(LINK_MAX, ws[0] + 0.3 * sum(ws[1:]))
        await asyncio.to_thread(claude.linked, r["id"], [l["how"] for l in links], interest)
        app["limbic"].felt("recognised", None, scale=interest, why="%s: %s" % (r["book"], links[0]["how"][:80]))
        for l in links:
            c = cands[l["n"] - 1]
            q = "What did %s remind Patti of?" % where
            vec = await asyncio.to_thread(claude.embed, [q])
            app["memory"].learned(None, q, l["how"], "book", True, vec[0] if vec else None)
            login = app["users"].login_for(c["who"]) if c["source"] == "told" and c["who"] else None
            if l["ask"] and login:
                app["memory"].wonder(login, "you", l["ask"], None, time.time() + 1800)
        print("reading: %s reminded her of %s (interest %.2f)" % (
            where, "; ".join("%s [%s]" % (cands[l["n"] - 1]["question"], l["kind"]) for l in links), interest),
            file=sys.stderr)
        return first["line"]
    except (claude.Unavailable, KeyError, IndexError) as e:
        print("reading: recognising failed (%r)" % e, file=sys.stderr)
        return ""


async def want_book(app, title, who):
    """A book she's decided to read (understand: recommended, offered, someone's favourite): onto her
    reading list, and if she isn't reading anything, she starts it now and tells them how it begins."""
    try:
        found = (await asyncio.to_thread(claude.books, title)).get("books") or []
        b = pick_book(found)
        if not b:
            print("reading: wanted %r, but it isn't clearly one of the books (%s)" % (
                title, "; ".join(b["title"] for b in found[:3]) or "nothing like it"), file=sys.stderr)
            if who:
                await tell_reading(app, who, True, "I looked for %s, but I can't find it in your library%s." % (
                    title, ". Is it one of these? " + "; ".join(b["title"] for b in found[:3]) if found else ""))
            return
        st = await asyncio.to_thread(claude.books, None, False, b["id"])
        now = st.get("reading") or {}
        print("reading: wants %s (%s)" % (b["title"], "reading it now" if now.get("id") == b["id"] else
                                            "on her list"), file=sys.stderr)
        if now.get("id") == b["id"] and not now.get("at"):
            await read_sitting(app, None, who, True)
    except claude.Unavailable as e:
        print("reading: friday can't (%s)" % e, file=sys.stderr)


async def tell_reading(app, who, quiet, line):
    login = app["users"].login_for(who)
    chat_turn(app, who, "(What did you think of it?)", line)
    if quiet and login and await message(app, login, line):
        return
    if not quiet:
        say(app, line)


def read_urge(app, b, now=None):
    """Why she'd pick up the book she's reading (b: claude.books()'s "reading") now, or None."""
    now = now or time.time()
    lim = app["limbic"]
    since = now - (b.get("last") or 0)
    if since < READ_GAP_S or lim.with_her or any(now - t < READ_TALKING_S for t in lim.recent.get("spoken_to", [])):
        return None
    if since >= READ_ANYWAY_S:
        return "it's been a while"
    if (b.get("pull") or 0) >= READ_PULL:
        return "she wants to know what happens"
    if lim.level["dopamine"] < READ_BORED and since >= READ_BORED_S:
        return "she's bored"
    return None


async def reading_watch(app):
    """She reads on in the book she's reading, by herself, when she feels like it (read_urge), in
    READ_HOURS, at most READ_A_DAY sittings a day."""
    await asyncio.sleep(5 * 60)
    day, n = None, 0
    while True:
        await asyncio.sleep(READ_CHECK_S)
        lt = time.localtime()
        if not (READ_HOURS[0] <= lt.tm_hour < READ_HOURS[1]) or not claude.ready() or app.get("reading_now"):
            continue
        if lt.tm_yday != day:
            day, n = lt.tm_yday, 0
        if n >= READ_A_DAY:
            continue
        try:
            b = (await asyncio.to_thread(claude.books)).get("reading")
            why = read_urge(app, b) if b else None
            if why:
                print("reading: picks up %s (%s)" % (b["title"], why), file=sys.stderr)
                if await read_sitting(app):
                    n += 1
        except claude.Unavailable:
            pass
        except Exception as e:      # (never take the brain down)
            print("reading: %r" % e, file=sys.stderr)


async def not_a_command(app, phrase, text, me, perms, quiet):
    """/ask, not one of her commands: conversation. Thanks and kind words;
    anything told her, made sense of and remembered (understand_said); her
    own answers; what she remembers; small talk and questions to her,
    answered as herself; questions about the world looked up (Wikipedia,
    what she's been told, then Claude, remembered), or she asks to be told."""
    app["limbic"].felt("spoken_to", me or None)
    def answered(action, line, **more):
        claude.tally(action)
        if not quiet and app["speech"]:
            app["speech"].say(line)
        return web.json_response(dict({"heard": phrase, "action": action, "said": [line]}, **more))
    if limbic.THANKS.search(text) and len(text.split()) <= 8 and not text.rstrip().endswith("?"):
        app["limbic"].manners(text, me or None)         # (just thanks: "thanks for that!", "gracias, mija")
        app["memory"].said(me or None, text, "thanks")
        return answered("thanks", replies.pick("thanks_es" if re.search(r"gracias", text, re.I) else "thanks"))
    app["limbic"].manners(text, me or None)
    if KIND.search(text) and not text.rstrip().endswith("?"):
        app["limbic"].felt("praise", me or None, why=text[:80])
        if len(text.split()) > KIND_ALONE:      # (more to it than kind words: make sense of the rest too)
            line = await understand_said(app, text, me or None)
            if isinstance(line, str) and line:
                return answered("understood", line)
        app["memory"].said(me or None, text, "praise")
        return answered("praise", replies.pick("praise"))
    lookup, understood = None, False
    if statement(text) or waiting_for(app, me) or TO_HER.search(text):
        line = await understand_said(app, text, me or None, questions=True)
        understood = line is not None
        if isinstance(line, str) and line:
            return answered("understood", line)
        if isinstance(line, dict) and "lookup" not in line:
            return await books_or_reading(app, line, text, me, perms, quiet, answered)
        if isinstance(line, dict):
            lookup = line["lookup"]
    if (line := own_answer(text)):
        app["memory"].said(me or None, text, "own")
        chat_turn(app, me, text, line)
        return answered("own", line)
    a = await recall(app, text, "claude" in perms, me or None) if text else None
    can_ask = "claude" in perms and claude.ready()
    if a and a["match"] == "sure":
        print("typed: %r -> remembered %r" % (text, a["question"]), file=sys.stderr)
        app["memory"].said(me or None, text, "recall")
        app["limbic"].felt("remembered", me or None)
        chat_turn(app, me, text, a["answer"], a["private"])
        return answered("recall", recalled_line(a), remembered=memory.when(a["at"]), again=can_ask and not a["lasting"])
    if text and not understood and not (a and a["match"] == "maybe"):
        # small talk, a question to her ("how's it going?"), or one to look up
        line = await understand_said(app, text, me or None, questions=True)
        if isinstance(line, str) and line:
            return answered("understood", line)
        if isinstance(line, dict) and "lookup" not in line:
            return await books_or_reading(app, line, text, me, perms, quiet, answered)
        if isinstance(line, dict):
            lookup = line["lookup"]
    if not a and text:
        w = await wikipedia(app, text, me, lookup)
        if w:
            return answered("wikipedia", w["line"], wiki={"title": w["title"], "url": w["url"]}, claude=can_ask)
        line = await think_over(app, text, "claude" in perms, me or None)
        if line:
            return answered("thought", line, thought=True, claude=can_ask)
        if me and personal(app, text):
            return answered("asked", ask_to_be_told(app, text, me))
        if can_ask:         # (curious: she finds out, and remembers)
            answer, instead = await ask_claude(app, text, me, quiet)
            if answer:
                return web.json_response({"heard": phrase, "action": "claude", "said": [answer], "fromclaude": True})
        if me:
            return answered("asked", ask_to_be_told(app, text, me))
    claude.tally("unanswered")
    out = {"error": "That isn't one of her commands.", "heard": phrase, "near": voicecmds.near(phrase),
           "claude": can_ask}
    if not (a and a["match"] == "maybe"):
        app["limbic"].felt("not_understood", me or None, why=text[:80])
    if a and a["match"] == "maybe":
        out["similar"] = {"id": a["id"], "question": a["question"]}
    return web.json_response(out, status=404)


async def recall_post(request):
    """POST /recall {"id": ..., "quiet": ...}: yes, it's the same question
    as that one (not_a_command's "similar"): her answer to it."""
    app = request.app
    body = await request.json()
    a = app["memory"].answer(int(body.get("id", 0)))
    me, perms = request["user"]
    if not a or (a["private"] and "claude" not in perms) or (a["owner"] and a["owner"] != me):
        raise web.HTTPNotFound()
    line = recalled_line(a)
    chat_turn(app, me, a["question"], a["answer"], a["private"])
    if not (bool(body.get("quiet")) or "say" not in perms) and app["speech"]:
        app["speech"].say(line)
    return web.json_response({"said": [line], "remembered": memory.when(a["at"]),
                              "again": "claude" in perms and claude.ready() and not a["lasting"]})


async def ask_claude(app, text, me, quiet):
    """She asks Claude (claude.py, on friday), says the answer (unless
    quiet) and remembers it. (answer, None), or (None, what she said instead)."""
    speak = (lambda line: app["speech"].say(line)) if not quiet and app["speech"] else (lambda line: None)
    speak(replies.pick("claude_asking"))
    p = weather.place() or {}
    print("claude: asking %r for %s" % (text, me or "?"), file=sys.stderr)
    try:
        with app["hat"].thinking():
            r = await asyncio.to_thread(claude.ask, text, me or None, p.get("name"), chat_history(app, me))
    except claude.Unavailable as e:
        print("claude: no answer (%s)" % e, file=sys.stderr)
        line = replies.pick("claude_down")
        speak(line)
        return None, line
    answer = r["answer"]
    claude.tally("claude")
    app["memory"].said(me or None, text, "claude")
    chat_turn(app, me, text, answer, r["private"])
    if r["keep"]:       # (under the question as it'd be asked on its own: "How many files are in kowloon-mobile?")
        q = r["standalone"]
        vec = await asyncio.to_thread(claude.embed, [q])
        app["memory"].learned(me or None, q, answer, "kowloon" if r["private"] else "claude", r["lasting"],
                              vec[0] if vec else None, r["private"])
    app["limbic"].felt("learned", me or None, why=text[:80])
    speak(answer)
    return answer, None


async def claude_post(request):
    """POST /claude {"text": ..., "quiet": ...}: ask_claude, from the chat."""
    body = await request.json()
    text = str(body.get("text", "")).strip()[:1000]
    if not text:
        return web.json_response({"error": "Ask her something."}, status=400)
    me, perms = request["user"]
    answer, instead = await ask_claude(request.app, text, me, bool(body.get("quiet")) or "say" not in perms)
    if instead:
        return web.json_response({"error": instead}, status=502)
    return web.json_response({"said": [answer]})


class _Asked(dict):
    """A request from Telegram, for the page's handlers (ask_post and co.):
    as if typed on her page by that login."""
    def __init__(self, app, body, user, login):
        super().__init__(user=user, login=login)
        self.app, self._body = app, body

    async def json(self):
        return self._body


async def tg_call(app, handler, body, user, login):
    r = await handler(_Asked(app, body, user, login))
    return r.status, json.loads(r.text)


TG_KEEP = 50        # buttons remembered (older ones just say "Too late")


def tg_offer(app, **what):
    """A token for a button's data, for what it's about."""
    tok = secrets.token_hex(4)
    app["tgasks"][tok] = what
    while len(app["tgasks"]) > TG_KEEP:
        app["tgasks"].pop(next(iter(app["tgasks"])))
    return tok


def tok_for(app, text):
    return tg_offer(app, text=text)


async def tg_send(app, chat, text, buttons=None):
    return await asyncio.to_thread(app["telegram"].send, chat, text, buttons)


async def message(app, login, text, buttons=None):
    """She messages them (Telegram), when they're not in front of her. True
    if it reached any of their chats."""
    t = app["telegram"]
    if not t:
        return False
    sent = [await tg_send(app, c, text, buttons) for c in t.chats(login)]
    return any(sent)


# "say: dinner's ready" on Telegram: she says it out loud, wherever she is.
TG_SAY = re.compile(r"^\s*(?:say|speak|announce)\s*[:,-]\s*(.+)$", re.I | re.S)


async def telegram_text(app, chat, login, text, reply_to=None):
    """A message to her on Telegram: as if typed in her page's chat by that
    login; never out loud (they're somewhere else), except "say: ...",
    which she says (as the page's Intercom tab)."""
    user = await asyncio.to_thread(app["users"].user, login)
    if not user or "chat" not in user[1]:
        return
    print("telegram: %s: %r" % (login, text), file=sys.stderr)
    q = app["drives"].question_for_reply(chat, reply_to) if reply_to else None
    if q:
        await tg_send(app, chat, await app["drives"].reply(q["id"], text, user[0]))
        return
    m = TG_SAY.match(text)
    if m:
        if "say" not in user[1]:
            await tg_send(app, chat, "You can't make me say things out loud, sorry.")
            return
        status, d = await tg_call(app, say_post, {"text": m.group(1).strip()[:500]}, user, login)
        await tg_send(app, chat, d.get("error") or "Said it out loud.")
        return
    status, d = await tg_call(app, ask_post, {"text": text[:1000], "quiet": True}, user, login)
    can_ask = bool(d.get("claude"))
    if d.get("similar"):
        tok = tg_offer(app, text=text, id=d["similar"]["id"], claude=can_ask)
        await tg_send(app, chat, "You asked me something like that before: \u201c%s\u201d. Is it the same question?"
                      % d["similar"]["question"], [("Yes", "r:" + tok), ("No", "n:" + tok)])
    elif d.get("error"):
        if can_ask and status == 404:
            await tg_send(app, chat, "I don't know how to answer that. Should I ask Claude?",
                          [("Yes, ask Claude", "c:" + tok_for(app, text)), ("No", "x:")])
        elif status == 404:
            near = d.get("near") or []
            await tg_send(app, chat, "I don't know that one." + (" Did you mean: %s?" % "; ".join(near) if near else ""))
        else:
            await tg_send(app, chat, d["error"])
    else:
        await tg_answer(app, chat, text, d)


async def tg_answer(app, chat, text, d):
    lines = [re.sub(r"\[[^\]]*\]\s*", "", l) for l in d.get("said", [])]
    if d.get("thought"):
        lines.append("(worked out from what I've been told)")
    if d.get("remembered"):
        lines.append("(from memory: I asked about that %s.)" % d["remembered"])
    if d.get("wiki"):
        lines.append("(from Wikipedia: %s)" % d["wiki"]["url"])
    if d.get("fromclaude"):
        lines.append("(I asked Claude; I'll remember that)")
    if d.get("books"):
        lines.append("(from your books: %s)" % "; ".join(d["books"]))
    buttons = None
    if d.get("wiki") and d.get("claude"):
        buttons = [("Ask Claude instead", "c:" + tok_for(app, text))]
    elif d.get("again"):
        buttons = [("Ask Claude again", "c:" + tok_for(app, text))]
    await tg_send(app, chat, "\n".join(lines) or "(done: %s)" % d.get("action", "?"), buttons)


async def telegram_button(app, chat, login, data):
    """A button under one of her messages: "c:tok" ask Claude, "r:tok" yes,
    the same question, "n:tok" no it isn't, "x:" no."""
    kind, _, tok = data.partition(":")
    if kind == "x":
        await tg_send(app, chat, "Okay.")
        return
    what = app["tgasks"].pop(tok, None)
    user = await asyncio.to_thread(app["users"].user, login)
    if not what or not user:
        await tg_send(app, chat, "Too late for that one, sorry: ask me again?")
        return
    if kind == "n":
        if what.get("claude"):
            await tg_send(app, chat, "Should I ask Claude?",
                          [("Yes, ask Claude", "c:" + tok_for(app, what["text"])), ("No", "x:")])
        else:
            await tg_send(app, chat, "Then I don't know how to answer that.")
    elif kind == "r":
        status, d = await tg_call(app, recall_post, {"id": what["id"], "quiet": True}, user, login)
        if status == 200:
            await tg_answer(app, chat, what["text"], d)
        else:
            await tg_send(app, chat, "I've lost that one.")
    elif kind == "c":
        await tg_send(app, chat, "Asking Claude\u2026")
        status, d = await tg_call(app, claude_post, {"text": what["text"], "quiet": True}, user, login)
        if d.get("error"):
            await tg_send(app, chat, d["error"])
        else:
            await tg_send(app, chat, "\n".join(d["said"]) + "\n(from Claude; I'll remember that)")


PHOTO_SIDE = 1600       # photos sent to her are shrunk to this (longest side) before she looks


def photo_people(app, jpeg):
    """A photo sent to her: (as a JPEG, shrunk), and who her face recognition
    finds in it (names, best first). (None, []) if it isn't a picture."""
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, []
    h, w = img.shape[:2]
    k = PHOTO_SIDE / max(h, w)
    if k < 1:
        img = cv2.resize(img, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    names = []
    who = app["who"]["faces"]
    if hasattr(who, "identify"):
        found = faces.detector(w, h).detect(img)[1]       # (its own detector: the camera's may be busy)
        for f in found if found is not None else ():
            name, _ = who.identify(img, f)
            if name and name not in names:
                names.append(name)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return (buf.tobytes() if ok else None), names


async def telegram_photo(app, chat, login, jpeg, caption=""):
    """A photo sent to her on Telegram: who's in it (her own face
    recognition), what it is (Claude on friday, claude.see), what she says
    back, and a line remembered about it. The photo itself isn't kept."""
    user = await asyncio.to_thread(app["users"].user, login)
    if not user or "chat" not in user[1]:
        return
    who = user[0]
    print("telegram: %s: a photo%s" % (login, (" (%r)" % caption) if caption else ""), file=sys.stderr)
    if "claude" not in user[1] or not claude.ready():
        await tg_send(app, chat, "I can't look at photos for you yet, sorry. Tell me about it?")
        return
    img, people = await asyncio.to_thread(photo_people, app, jpeg)
    if img is None:
        await tg_send(app, chat, "I couldn't open that one, sorry.")
        return
    await tg_send(app, chat, "Ooh, let me look\u2026")
    try:
        r = await asyncio.to_thread(claude.see, img, who, caption, people, (weather.place() or {}).get("name"))
    except claude.Unavailable as e:
        print("see: no answer (%s)" % e, file=sys.stderr)
        await tg_send(app, chat, "I couldn't see it properly just now. Try me again in a bit?")
        return
    print("see: %s sent %r -> %r" % (who, r["about"], r["reply"]), file=sys.stderr)
    app["limbic"].felt("spoken_to", who)
    app["memory"].said(who, "[photo] " + (caption or r["about"])[:300], "photo")
    if r["about"]:
        q = "What was in the photo %s sent Patti on %s?" % (who or "someone", time.strftime("%d %B %Y"))
        try:
            vec = await asyncio.to_thread(claude.embed, [q])
        except claude.Unavailable:
            vec = None
        app["memory"].learned(who, q, r["about"], "photo", True, vec[0] if vec else None)
    chat_turn(app, who, "(sent Patti a photo%s)" % ((": " + caption) if caption else ""), r["reply"])
    await tg_send(app, chat, r["reply"] or "Thanks for that!")


# A spoken question ("Patti, I've got a question"): she listens for up to
# QUESTION_MAX_S, till QUESTION_QUIET_S of quiet, and friday writes it down
# (Whisper; her own Vosk if friday's off). Then as in the chat: one of her
# commands, she does it; something she's been told, she says; else she
# offers to ask Claude. Claude, and private answers, only for people she
# knows by voice.
QUESTION_WAIT_S = 8.0
QUESTION_MAX_S = 20.0
QUESTION_QUIET_S = 1.5


FREE_NAME = re.compile(r"^\W*(?:hey\s+|oi\s+|ok(?:ay)?\s+)?(?:patti|patty|pattie|paddy|pat)\b\W*", re.I)
FREE_NAME_END = re.compile(r"\W+(?:patti|patty|pattie|paddy)\W*$", re.I)      # "who's Josh, Patti?"
FREE_SAME = 0.6         # friday's words this like hers (after her name): the same sentence, her name lost off the front


async def spoken_free(app, audio, speaker):
    """"Patti, <anything>" (voice.py: her name, then not a command): friday
    writes it down; if her name really starts it, it's taken as typed in
    her chat by whoever said it (a command, a question, or something to
    make sense of), and she answers out loud."""
    m, v = app["meet"], app["voice"]
    if m.busy or not audio or not claude.ready():
        return
    heard = [w for w in (v.heard or "").split() if w not in voicecmds.NAMES + list(voicecmds.GREETS)] if v else []
                                                            # (what she made of it herself, less her name)
    try:
        hint = ", ".join(sorted(known_people(app)) + app["memory"].names())
        with app["hat"].thinking():
            text = (await asyncio.to_thread(claude.transcribe, audio, hint)).strip()
    except claude.Unavailable as e:
        print("free: friday's not writing it down (%s)" % e, file=sys.stderr)
        return
    who = await speaker_name(speaker)
    n, e = FREE_NAME.match(text), FREE_NAME_END.search(text)
    if n and text[n.end():].strip():
        text = text[n.end():].strip()
    elif e and text[:e.start()].strip():
        text = text[:e.start()].strip() + ("?" if text.rstrip().endswith("?") else "")
    elif heard and FREE_SAME <= difflib.SequenceMatcher(
            None, " ".join(heard), " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split())).ratio():
        pass        # (friday lost her name off the front, but it's what she heard after it: still to her)
    else:
        print("free: %r (not to her)" % text, file=sys.stderr)
        return
    print("free: %s said %r" % (who or "?", text), file=sys.stderr)
    lead = re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()
    for p in sorted(voicecmds.COMMANDS, key=len, reverse=True):      # ("this is my cleaner, Alex": introduce)
        if lead[:len(p.split())] == p.split() and voicecmds.COMMANDS[p][0] in ("introduce", "meet", "forget",
                                                                             "whosthat", "question"):
            await voice_action(app, voicecmds.COMMANDS[p][0], voicecmds.COMMANDS[p][1], speaker)
            return
    v.heard, v.heard_at = text, time.monotonic()
    perms = set(users.PERMS) if who in known_people(app) else {"chat"}
    login = app["users"].login_for(who) if who else None
    user = (app["users"].user(login) if login else None) or (who or "", perms)
    status, d = await tg_call(app, ask_post, {"text": text, "quiet": True}, user, login or "")
    for line in d.get("said") or []:
        await m.say_text(re.sub(r"\[[^\]]*\]\s*", "", line) if "[" in line and "]" in line else line)
    if status == 404 and d.get("claude") and who in known_people(app):
        if await m.yes("question_claude"):
            await ask_claude(app, text, who, False)
        else:
            await m.say_line("question_ok")
    elif status == 404:
        await m.say_line("question_dunno")
    elif d.get("similar"):
        a = app["memory"].answer(d["similar"]["id"])
        if a and await m.yes("question_same", question=a["question"]):
            await m.say_text(recalled_line(a))


async def spoken_question(app, speaker):
    m, v = app["meet"], app["voice"]
    if m.busy or not v:
        say(app, replies.pick("meet_busy"))
        return
    m.busy = True
    try:
        who = await speaker_name(speaker)
        await m.say_line("question_go")
        audio = await asyncio.to_thread(v.utterance, QUESTION_WAIT_S, QUESTION_MAX_S, QUESTION_QUIET_S)
        if not audio:
            await m.say_line("question_nothing")
            return
        try:
            hint = ", ".join(sorted(known_people(app)) + app["memory"].names())
            with app["hat"].thinking():
                text = await asyncio.to_thread(claude.transcribe, audio, hint)
        except claude.Unavailable as e:
            print("question: friday's not writing it down (%s): Vosk" % e, file=sys.stderr)
            text = await asyncio.to_thread(v.words, audio)
        text = text.strip()
        print("question: %s asked %r" % (who or "?", text), file=sys.stderr)
        app["limbic"].manners(text, who)
        if not text:
            await m.say_line("question_nothing")
            return
        v.heard, v.heard_at = text, time.monotonic()
        v.speaker = (v.heard_at, who)
        phrase, act = voicecmds.typed(text)
        if act and act[0] != "question":
            done = concurrent.futures.Future()
            done.set_result(who)
            m.busy = False      # (the command may want her: "remember me")
            await voice_action(app, act[0], act[1], done)
            return
        trusted = who in known_people(app)
        if trusted and (statement(text) or waiting_for(app, who)):
            line = await understand_said(app, text, who)
            if line:
                await m.say_text(line)
                return
        if (line := own_answer(text)):
            app["memory"].said(who, text, "own")
            chat_turn(app, who, text, line)
            await m.say_text(line)
            return
        a = await recall(app, text, trusted, who)
        if a and a["match"] == "maybe" and not await m.yes("question_same", question=a["question"]):
            a = None
        if a:
            app["memory"].said(who, text, "recall")
            chat_turn(app, who, text, a["answer"], a["private"])
            await m.say_text(recalled_line(a))
            if a["lasting"] or not trusted or not claude.ready() or not await m.yes("question_again"):
                return
        elif (w := await wikipedia(app, text, who)):
            await m.say_text(w["line"])
            return
        elif (line := await think_over(app, text, trusted, who)):
            await m.say_text(line)
            return
        elif trusted and personal(app, text):
            await m.say_text(ask_to_be_told(app, text, who))
            return
        elif not trusted or not claude.ready():
            await m.say_line("question_dunno")
            return
        elif not await m.yes("question_claude"):
            await m.say_line("question_ok")
            return
        await ask_claude(app, text, who, False)
    except Exception as e:      # keep the brain up whatever happens here
        print("question: %r" % e, file=sys.stderr)
    finally:
        m.busy = False


async def music_action(app, what):
    mu = app["music"]
    song = await asyncio.to_thread(mu.now)
    if what == "what":
        if not song:
            say(app, replies.pick("nothing_playing"))
        elif song["artist"]:
            say(app, replies.pick("song", song=song["title"], artist=song["artist"]))
        else:
            say(app, replies.pick("song_only", song=song["title"]))
    elif what in ("up", "down"):
        await asyncio.to_thread(mu.volume, 0.1 if what == "up" else -0.1)
    elif not song:
        say(app, replies.pick("nothing_playing"))
    else:
        await asyncio.to_thread(mu.control, what, song)


async def who_spoke(app, audio, floor_db, heard_at=None):
    """Who said a command, from the audio just before it (voice.py's `said`):
    their name, or None (a stranger, too little speech, or no voices yet).
    Only the parts louder than the room count, so pauses don't dilute it.
    The chest shows the name with what was heard (voice.speaker, tagged with
    voice.heard_at, so a late answer can't label a newer line)."""
    name, best = await _who_spoke(app, audio, floor_db)
    talking = app["memory"].talking()
    others = [n for n in faces_seen(app, FACE_HELP_S) if n != talking]
    stranger = time.monotonic() - app["who"]["stranger_at"] < FACE_HELP_S   # (someone she doesn't know: could be them)
    if not name and talking and not others and not stranger and (best is None or best >= CONVO_VOICE):
        # Not sure whose voice, mid-conversation, but somewhat like theirs and
        # nobody else she knows in view: theirs. (Not when someone else is in
        # front of her: Sam saying "learn my voice" just after Josh spoke.)
        print("voice: taken as %s's (talking with them)" % talking, file=sys.stderr)
        name = talking
    if name:
        app["memory"].heard(name)
    app["who"]["last_voice"] = (heard_at, name, best)
    if app["voice"] and heard_at is not None:
        app["voice"].speaker = (heard_at, name)
    return name


async def _who_spoke(app, audio, floor_db):
    """(name or None, best voice score or None if it couldn't tell)."""
    vs = app["who"]["voices"]
    if not vs or not audio:
        return None, None
    x = np.frombuffer(audio, np.int16).astype(np.float32) / 32768
    n = 400                                             # 25 ms frames, as voices.frames_db
    db = voices.frames_db(x, n)
    keep = np.repeat(db > floor_db + SPEAKER_DB, n)
    x = x[:len(keep)][keep]
    if len(x) < SPEAKER_MIN_S * voices.RATE:
        print("voice: too little speech to tell who (%.1f s)" % (len(x) / voices.RATE), file=sys.stderr)
        return None, None
    name, score = await asyncio.to_thread(vs.identify, x)
    how = "voice"
    if not name and vs.people:
        # Not sure from the voice alone: does the face she can see help?
        s = await asyncio.to_thread(lambda: vs.scores(vs.voiceprint(x)))
        best, who = s[0]
        runner = s[1][0] if len(s) > 1 else -1.0
        seen = app["who"]["face"].get(who)
        if (seen is not None and time.monotonic() - seen < FACE_HELP_S
                and best >= VOICE_WITH_FACE and best - runner >= voices.MARGIN):
            name, score, how = who, best, "voice + face"
    print("voice: said by %s (%.2f, %s)" % (name or "?", score, how), file=sys.stderr)
    if name:
        app["who"]["present"][name] = time.monotonic()
    return name, score


def faces_seen(app, within):
    """People she's recognised by face in the last `within` s, latest first."""
    now, f = time.monotonic(), app["who"]["face"]
    return sorted((n for n, t in f.items() if now - t < within), key=lambda n: -f[n])


async def maybe_meet(app, speaker, heard_at):
    """After a command: offer to remember whoever said it, if they're new
    (see STRANGER_VOICE and the rest)."""
    w, m = app["who"], app["meet"]
    if app["place"].away:
        return                  # (out and about: not offering to remember everyone in the café)
    if m.busy or not w["faces"] or not w["voices"] or time.monotonic() - w["asked_at"] < ASK_AGAIN_S:
        return
    if await speaker_name(speaker):
        return
    lv = w["last_voice"]
    if not lv or lv[0] != heard_at or lv[2] is None or lv[2] >= STRANGER_VOICE:
        return                  # (a voice like someone she knows, or too short to tell)
    no_voice = [n for n in faces_seen(app, STRANGER_SEEN_S) if n not in w["voices"].people]
    if not no_voice and time.monotonic() - w["stranger_at"] > STRANGER_SEEN_S:
        return                  # (nobody new in view: the telly, or someone out of sight)
    # Let her answer the command first.
    await asyncio.sleep(1.0)
    sp = app["speech"]
    while sp and sp.speaking:
        await asyncio.sleep(0.1)
    print("meet: offering (%s)" % ("voice of " + no_voice[0] if no_voice else "a stranger"), file=sys.stderr)
    await m.meet(name=no_voice[0] if no_voice else None)


async def speaker_name(speaker):
    """Who said it (a who_spoke future), waiting up to SPEAKER_WAIT_S; or None."""
    if not speaker:
        return None
    try:
        return await asyncio.wait_for(asyncio.wrap_future(speaker), SPEAKER_WAIT_S)
    except Exception:           # (timed out, or who_spoke failed: answer without a name)
        return None


async def remember_said(app, text, action, speaker):
    """Into her memory: what was said, and by whom (once she's worked it out)."""
    app["memory"].said(await speaker_name(speaker), text, action)


def last_seen(app, name):
    """What she says to "when did you last see <name>?" (memory.py)."""
    r = app["memory"].last(name)
    if not r:
        return replies.pick("lastseen_cat" if name in memory.CATS else "lastseen_never", name=name)
    at, how, where = r
    if time.time() - at < memory.HERE_S:
        return replies.pick("lastseen_here_" + how, name=name)
    where = ", " + where if where and where != app["place"].words else ""
    return replies.pick("lastseen_" + how, name=name, when=memory.when(at), where=where)


def last_seen_me(app, name):
    """What she says to "when did you last see me?", from <name>: the time
    before this one."""
    r = app["memory"].before(name)
    if not r:
        return replies.pick("lastseen_me_never", name=name)
    at, how, where = r
    where = ", " + where if where and where != app["place"].words else ""
    return replies.pick("lastseen_me_" + how, name=name, when=memory.when(at), where=where)


async def voice_action(app, action, arg, speaker=None):
    """A spoken command (from voice.py's thread, via run_coroutine_threadsafe).
    speaker: a future of who said it (who_spoke), or None."""
    poke(app, "voice command")
    m, cam, g = app["motors"], app["camera"], app["gimbal"]
    if _eyes:
        _eyes.blink()       # "heard you"
    who = speaker.result() if speaker and speaker.done() and not speaker.exception() else None
    if action != "stop":
        app["limbic"].felt(action if action in ("praise", "thanks") else "spoken_to", who)
    if action == "how":
        how, why = app["limbic"].how()
        say(app, replies.pick("how_" + how) + (" " + replies.pick("why_" + why) if why else ""))
    elif action == "thanks" and arg == "es":
        say(app, replies.pick("thanks_es"))
    elif action in SPOKEN:
        say(app, replies.pick(action))
    if action in ("stop", "unfollow", "look", "centre", "relax"):     # takes the gimbal back
        gaze(app, None)
    if action in ("stop", "look", "centre", "relax"):
        hands_off(app)
    if action == "stop":
        m.stop()
        g.look(0, 0)
        if app["timer"]:
            app["timer"].dismiss()
    elif action == "seek":
        gaze(app, look_at_me)
    elif action == "follow":
        gaze(app, follow_me)
    elif action == "watch":
        set_curious(app, arg)
        say(app, replies.pick("watch_on" if arg else "watch_off"))
    elif action == "look":
        g.aim(g.pan + arg[0], g.tilt + arg[1])
    elif action == "centre":
        g.centre()
    elif action == "relax":
        g.relax()
    elif action == "eyes":
        if _eyes:
            _eyes.reset()
    elif action == "sleep":
        doze(app, "asleep")
    elif action == "mood":
        if _eyes:
            _eyes.set_mood(arg)
            app["feel"]["mood_set"] = time.monotonic()
    elif action == "record":
        if arg:
            cam.start_recording()
        else:
            await asyncio.to_thread(cam.stop_recording)
    elif action == "table":
        m.set_table(arg)
    elif action == "hello":
        name = await speaker_name(speaker)
        if name:
            key = (arg or "hello") + "_name"
            say(app, replies.pick(key if key in replies.REPLIES else "hello_name", name=name))
        else:
            say(app, replies.pick(arg or "hello"))
    elif action == "meet":
        # "Remember me": a stranger gets the lot; someone she knows by face
        # but not by voice, just the voice; someone she knows, nothing to do.
        m, w = app["meet"], app["who"]
        if m.busy:
            say(app, replies.pick("meet_busy"))
            return
        name = await speaker_name(speaker)
        seen = faces_seen(app, FACE_HELP_S)
        if name:
            say(app, replies.pick("meet_already", name=name))
        elif seen and w["voices"] and seen[0] not in w["voices"].people:
            await m.meet(name=seen[0])
        else:
            await m.meet(asked=True)
    elif action == "forget":
        seen = faces_seen(app, FACE_HELP_S)
        await app["meet"].forget(await speaker_name(speaker) or (seen[0] if seen else None))
    elif action == "introduce":
        seen = faces_seen(app, FACE_HELP_S)
        sid = stranger_here(app)
        if not sid and seen and seen[0] != await speaker_name(speaker):
            say(app, replies.pick("meet_already", name=seen[0]))
        else:
            await app["meet"].introduce(sid)
    elif action == "whosthat":
        seen = faces_seen(app, FACE_HELP_S)
        sid = stranger_here(app)
        if sid:
            say(app, replies.pick("whosthat_new"))
            await app["meet"].introduce(sid)
        elif seen:
            say(app, replies.pick("whosthat_known", name=seen[0]))
        else:
            say(app, replies.pick("whosthat_nobody"))
    elif action == "question":
        await spoken_question(app, speaker)
    elif action == "free":
        await spoken_free(app, arg, speaker)
    elif action == "lastseen":
        if arg:
            say(app, last_seen(app, arg))
        else:
            me = await speaker_name(speaker)
            say(app, last_seen_me(app, me) if me else replies.pick("lastseen_me_who"))
    elif action == "where":
        p = app["place"]
        say(app, replies.pick("where", words=p.words) if p.words else
            replies.pick("where_new", ssid=p.ssid) if p.ssid else replies.pick("where_none"))
    elif action == "whoami":
        # Her ears say who spoke; her eyes, who's been in view the last few seconds.
        name = await speaker_name(speaker)
        seen = faces_seen(app, FACE_HELP_S)
        if name and name in seen:
            say(app, replies.pick("whoami_both", name=name))
        elif name:
            say(app, replies.pick("whoami_voice", name=name))
        elif seen:
            say(app, replies.pick("whoami_face", name=seen[0]))
        else:
            say(app, replies.pick("whoami_unknown"))
    elif action == "music":
        await music_action(app, arg)
    elif action == "mute":
        app["chest"].set_listening(False)
    elif action in ("dnd", "dnd_off"):
        app["drives"].dnd(action == "dnd")
    elif action == "hatidle":
        app["chest"].set_hat_idle(arg)
    elif action == "voiceengine":
        app["chest"].set_voice(arg)
    elif action == "timer":
        app["timer"].start(arg)
        say(app, replies.pick("timer_set", length=timers.spoken(arg)))
    elif action == "timer_cancel":
        say(app, replies.pick("timer_cancelled" if app["timer"].cancel() else "no_timer"))
    elif action == "timer_left":
        left = app["timer"].left()
        say(app, replies.pick("no_timer") if left is None else replies.pick("timer_left", left=timers.spoken(left)))
    elif action == "pressure":
        say(app, app["barometer"].spoken() or replies.pick("no_pressure"))
    elif action == "note":
        if app["chest"].take_note():
            say(app, replies.pick("note"))
    elif action == "time":
        say(app, spoken_time())
    elif action == "weather":
        say(app, await asyncio.to_thread(weather.spoken) or replies.pick("no_weather"))
    elif action == "news":
        said_news(app, await speaker_name(speaker) if speaker else None)
    elif action == "voicevol" and app["speech"]:
        v = app["speech"].volume
        if app["speech"].step_volume(arg) == v:
            say(app, replies.pick("loudest" if arg > 0 else "quietest"))
        else:
            say(app, replies.pick("louder" if arg > 0 else "quieter"))


async def ws_control(request):
    """Messages from the page: {"t":"drive","l":..,"r":..}, {"t":"stop"},
    {"t":"look","x":..,"y":..} (rates, -1..1), {"t":"centre"}, {"t":"relax"}, {"t":"eyes"},
    {"t":"mood","mood":..} (eyes.MOODS), {"t":"doze","state":"drowsy"|"asleep"},
    {"t":"record","on":true|false}, {"t":"table","on":true|false},
    {"t":"follow","on":true|false}, {"t":"ears","on":true|false} (her listening for
    commands, as the chest's mic icon). Sends back
    status once a second, and straight away after a table-mode change."""
    ws = web.WebSocketResponse(heartbeat=5)
    await ws.prepare(request)
    me, perms = request["user"]
    online = request.app["online"]
    if me:
        online[me] = online.get(me, 0) + 1
    m, cam, g = request.app["motors"], request.app["camera"], request.app["gimbal"]

    async def send_status():
        v = request.app["voice"]
        await ws.send_json({"motors": m.ok, "pico": m.said, "table": m.table,
                            "camera": cam.error or "ok", "gimbal": g.ok,
                            "pan": round(g.pan), "tilt": round(g.tilt),
                            "rec": cam.recording(), "follow": following(request.app),
                            "voice": v.status() if v else None,
                            "eyes": _eyes.state if _eyes else None,
                            "mood": _eyes.mood if _eyes else None,
                            "moods": list(sys.modules["eyes"].MOODS) if _eyes else None,
                            "https": request.app["https"],
                            "who": present(request.app),
                            "place": request.app["place"].status(),
                            "talking": request.app["memory"].talking(),
                            "me": me, "perms": sorted(perms), "users": request.app["users"].any(),
                            "online": sorted(online)})

    async def status():
        while not ws.closed:
            if me:
                request.app["memory"].online(me)
            await send_status()
            await asyncio.sleep(1)

    task = asyncio.create_task(status())
    poke(request.app, "page opened")
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                cmd = json.loads(msg.data)
                need = WS_NEEDS.get(cmd.get("t"))
                if need and need not in perms:
                    continue
                # (The page sends stop and a zero look by itself, when it loses focus.)
                if (cmd.get("t") not in ("stop", "doze")
                        and not (cmd.get("t") == "look" and not (cmd["x"] or cmd["y"]))):
                    poke(request.app, "page")
                if cmd.get("t") == "drive":
                    m.drive(cmd["l"], cmd["r"])
                elif cmd.get("t") == "stop":
                    # Also sent when the page loses focus, so it leaves a
                    # follow running; the STOP button sends follow off too.
                    m.stop()
                elif cmd.get("t") == "follow":
                    gaze(request.app, follow_me if cmd.get("on") else None)
                elif cmd.get("t") == "table":
                    m.set_table(cmd.get("on"))
                    await send_status()
                elif cmd.get("t") == "ears":
                    if request.app.get("chest"):
                        request.app["chest"].set_listening(bool(cmd.get("on")))
                    elif request.app["voice"]:
                        request.app["voice"].muted = not cmd.get("on")
                    await send_status()
                elif cmd.get("t") == "look":
                    if cmd["x"] or cmd["y"]:
                        gaze(request.app, None)
                    g.look(cmd["x"], cmd["y"])
                elif cmd.get("t") == "centre":
                    gaze(request.app, None)
                    hands_off(request.app)
                    g.centre()
                elif cmd.get("t") == "relax":
                    gaze(request.app, None)
                    hands_off(request.app)
                    g.relax()
                elif cmd.get("t") == "mood":
                    if _eyes:
                        _eyes.set_mood(cmd["mood"])
                        request.app["feel"]["mood_set"] = time.monotonic()
                elif cmd.get("t") == "doze":
                    doze(request.app, cmd.get("state", "drowsy"))
                elif cmd.get("t") == "eyes":
                    if _eyes:
                        _eyes.reset()
                elif cmd.get("t") == "record":
                    if cmd.get("on"):
                        cam.start_recording()
                    else:
                        await asyncio.to_thread(cam.stop_recording)
            except (ValueError, KeyError, TypeError):
                pass
    finally:
        task.cancel()
        if me:
            online[me] -= 1
            if not online[me]:
                del online[me]
        m.stop()
        g.look(0, 0)
    return ws


# ---------------------------------------------------------------- main

def tailscale_ip():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=5).stdout.split()
        return out[0] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def tailscale_https():
    """Her https:// address on the tailnet (tailscale serve), or None."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                             text=True, timeout=5).stdout
        name = json.loads(out)["Self"]["DNSName"].rstrip(".")
        return "https://%s/" % name if name else None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def set_password():
    pw = getpass.getpass("New web password: ")
    if len(pw) < 6 or pw != getpass.getpass("Again: "):
        sys.exit("Passwords didn't match, or shorter than 6 characters.")
    os.makedirs(CONF, mode=0o700, exist_ok=True)
    fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pw + "\n")
    print("Saved to", PASSWORD_FILE)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--set-password", action="store_true", help="set the web password and exit")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", help="address to listen on (default: this machine's Tailscale IP)")
    ap.add_argument("--motors", default=os.environ.get("PORT", MOTOR_PORT), help="motor Pico serial port")
    ap.add_argument("--gimbal", default=GIMBAL_PORT, help="gimbal Pico serial port")
    ap.add_argument("--camera", default="/dev/video0")
    ap.add_argument("--size", default="640x480", help="camera size, e.g. 320x240 for a slow link")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--quality", type=int, default=50, help="JPEG quality, 1-100 (lower = lighter on a slow link)")
    ap.add_argument("--rec-size", default="1280x720", help="camera size while recording (the stream stays at --size)")
    ap.add_argument("--rec-dir", default=REC_DIR, help="where recordings go (e.g. a USB drive)")
    ap.add_argument("--audio", default=MIC, help="ALSA mic for recordings ('' for none)")
    ap.add_argument("--mic", default=MIC, help="ALSA mic for voice commands ('' for none)")
    ap.add_argument("--no-eyes", action="store_true", help="leave the eye panels alone")
    ap.add_argument("--pi-eyes", action="store_true", help="eye panels on the Pi's SPI6, not the gimbal Pico")
    ap.add_argument("--no-speech", action="store_true", help="don't talk")
    ap.add_argument("--cyd", default=cyd.PORT, help="chest screen (CYD) serial port")
    args = ap.parse_args()

    if args.set_password:
        set_password()
        return
    pw = read_password()
    if not pw:
        sys.exit("No web password yet. Set one with: python3 %s --set-password" % sys.argv[0])
    bind = args.bind or tailscale_ip()
    if not bind:
        sys.exit("No Tailscale address found; pass --bind (e.g. 127.0.0.1).")
    w, h = (int(v) for v in args.size.lower().split("x"))

    app = web.Application(middlewares=[require_login])
    app["token"] = session_token(pw)
    app["users"] = users.Users()
    app["chats"] = {}       # who -> recent questions and answers (chat_turn)
    app["clarify"] = {}     # who -> (what she didn't understand, when): understand_said
    app["telegram"] = None  # (start_watch: telegram.Telegram, if she has a bot key)
    app["tgasks"] = {}      # token -> what a Telegram button is about (tg_offer)
    app["online"] = {}      # name -> pages open: who's logged in on her page now
    app["motors"] = Motors(args.motors)
    app["gimbal"] = Gimbal(args.gimbal)
    rw, rh = (int(v) for v in args.rec_size.lower().split("x"))
    app["camera"] = Camera(args.camera, (w, h), args.fps, args.quality,
                           (rw, rh), os.path.expanduser(args.rec_dir), args.audio)
    app.router.add_get("/", index)
    app.router.add_static("/static/fonts", os.path.join(HERE, "static", "fonts"))     # the page's icons
    app.router.add_get("/login", login_get)
    app.router.add_post("/login", login_post)
    app.router.add_get("/ws", ws_control)
    app.router.add_get("/video.mjpg", video)
    app.router.add_get("/recordings.json", recordings)
    app.router.add_get("/rec/{name}", recording_get)
    app.router.add_delete("/recordings/{name}", recording_delete)
    app.router.add_post("/say", say_post)
    app.router.add_post("/ask", ask_post)
    app.router.add_post("/merge", merge_post)
    app.router.add_post("/eyes", eyes_post)
    app.router.add_get("/logout", logout)
    app.router.add_post("/password", password_post)
    app.router.add_get("/me", me_get)
    app.router.add_post("/me", me_post)
    app.router.add_get("/users.json", users_get)
    app.router.add_post("/users", users_add)
    app.router.add_post("/users/{name}", users_change)
    app.router.add_delete("/users/{name}", users_change)
    app.router.add_post("/claude", claude_post)
    app.router.add_post("/hush", hush_post)
    app.router.add_post("/recall", recall_post)
    app.router.add_get("/people.json", people_get)
    app.router.add_get("/limbic.json", limbic_get)
    app.router.add_get("/log.json", log_get)
    app.router.add_get("/people/{id}.jpg", stranger_snap)
    app.router.add_post("/people/{id}", stranger_name)
    app.router.add_delete("/people/{id}", stranger_forget)
    app["intercom"] = ic = intercom.Intercom(args.mic)
    app["https"] = tailscale_https()    # the page points there to talk (browsers want https for a mic)
    app.router.add_get("/listen", ic.listen)
    app.router.add_get("/talk", ic.talk)
    app["echo"] = {"lines": None, "quiet": False}   # what she says while /ask runs a command (and whether aloud)

    app["gaze"] = {"task": None, "job": None}  # what the camera's doing by itself
    app["curious"] = {"on": not os.path.exists(WATCH_OFF_FILE), "quiet_until": 0.0}
    try:
        with open(GREETED_FILE) as f:
            greeted = dict(json.load(f))
    except (OSError, ValueError, TypeError):
        greeted = {}
    # faces: faces.Who once loaded (load_who); voices: voices.Voices, likewise;
    # last: last name logged; face: name -> when last recognised by face;
    # stranger_at: last saw a face like nobody she knows; asked_at: last offered
    # to remember someone; last_voice: (heard_at, name, best score); present:
    # name -> when last seen; seen: name -> recent sightings; greeted: name -> her_day()
    app["meet"] = meet.Meet(app, {"say": lambda text: say(app, text), "detect_faces": detect_faces,
                                  "greeted": lambda name: met(app, name),
                                  "forgot": lambda name: forgot(app, name),
                                  "named": lambda sid, name: named(app, sid, name)})
    app["who"] = {"faces": None, "voices": None, "last": None, "present": {}, "face": {}, "seen": {},
                  "greeted": greeted, "stranger_at": -1e9, "asked_at": -1e9, "last_voice": None,
                  "strangers": None, "stranger": None, "voice_asked": {}}
    app["voice"] = voice = Voice(args.mic) if args.mic else None
    app["speech"] = None if args.no_speech else Speech()
    app["music"] = music.Music()
    app["eyes"] = None if args.no_eyes else start_eyes(None if args.pi_eyes else app["gimbal"])
    app["place"] = places.Place()
    app["memory"] = memory.Memory()
    app["limbic"] = limbic.Limbic(app["memory"])
    app["drives"] = drives.Drives(app, drive_ctx(app))
    app["news"] = news.News(app["memory"])
    app["feel"] = {"mood_set": -1e9,   # when someone last chose her eyes' mood (limbic leaves them be for MOOD_HOLD_S)
                   "looked": 0.0, "told": 0.0}     # when unhappy_check last looked, and last told anyone
    app["attn"] = {"at": time.monotonic()}     # last time anyone did anything
    app["hat"] = hat.Hat()
    app["hat"].speech = app["speech"]
    app["hat"].intercom = ic
    app["timer"] = None             # (made on the loop, in start_watch)
    app["barometer"] = barometer.Barometer()
    app["cyd"] = cyd.Cyd(args.cyd)

    async def start_watch(app):
        loop = asyncio.get_running_loop()
        if voice:
            def loud(db, floor):
                poke(app, "a sound (%.0f dBFS, room %.0f)" % (db, floor))
                app["limbic"].felt("loud", why="%.0f dBFS" % db)
            voice.on_loud = lambda db, floor: loop.call_soon_threadsafe(loud, db, floor)
            # Deaf while she talks, or someone talks through her (the intercom).
            sp = app["speech"]
            voice.deaf = lambda: (sp is not None and sp.speaking) or ic.talking
            if sp is not None:          # "stop" while she talks (voice.py, barge)
                voice.barge = lambda word: (word not in (sp.saying or "").lower()) and (sp.hush() or True)
            def heard(action, arg, text):       # (voice.py's thread)
                # Who said it, alongside (never holding up "stop").
                speaker = None if action == "stop" else asyncio.run_coroutine_threadsafe(
                    who_spoke(app, voice.said, voice.floor_db, voice.heard_at), loop)
                asyncio.run_coroutine_threadsafe(voice_action(app, action, arg, speaker), loop)
                asyncio.run_coroutine_threadsafe(remember_said(app, text, action, speaker), loop)
                if speaker and action not in ("meet", "forget", "introduce", "whosthat", "question", "free"):
                    asyncio.run_coroutine_threadsafe(maybe_meet(app, speaker, voice.heard_at), loop)
            voice.start(heard)
        app["attention"] = asyncio.create_task(attention_watch(app))
        app["watch"] = asyncio.create_task(motor_watch(app))
        app["gwatch"] = asyncio.create_task(gimbal_watch(app))
        app["curiosity"] = asyncio.create_task(curiosity(app))
        app["whoload"] = asyncio.create_task(asyncio.to_thread(load_who, app))
        app["rwatch"] = asyncio.create_task(rec_watch(app))
        app["timer"] = app["hat"].timer = timers.Timer(lambda key, **f: say(app, replies.pick(key, **f)))
        app["timerrun"] = asyncio.create_task(app["timer"].run())
        app["storm"] = asyncio.create_task(storm_watch(app))
        app["placewatch"] = asyncio.create_task(place_watch(app))
        app["outwatch"] = asyncio.create_task(output_watch(app))
        app["memwatch"] = asyncio.create_task(memory_watch(app))
        app["hatshow"] = asyncio.create_task(app["hat"].run())
        app["drivesrun"] = asyncio.create_task(app["drives"].run())
        app["newsrun"] = asyncio.create_task(news_watch(app))
        app["readrun"] = asyncio.create_task(reading_watch(app))
        app["pad"] = macropad.Pad(lambda n, pressed: loop.call_soon_threadsafe(pad_key, app, n, pressed),
                                  lambda knob, clicks: loop.call_soon_threadsafe(pad_turn, app, knob, clicks))
        app["pad"].start()
        if telegram.ready():
            app["telegram"] = telegram.Telegram(
                lambda chat, login, text, reply_to: asyncio.run_coroutine_threadsafe(
                    telegram_text(app, chat, login, text, reply_to), loop),
                lambda chat, login, data: asyncio.run_coroutine_threadsafe(telegram_button(app, chat, login, data), loop),
                lambda chat, login, jpeg, caption: asyncio.run_coroutine_threadsafe(
                    telegram_photo(app, chat, login, jpeg, caption), loop))
            app["telegram"].start()
        app["chest"] = chest.Chest(app, app["cyd"], app["hat"], app["voice"], _eyes, app["motors"], {
            "speech": app["speech"],
            "music": app["music"],
            "intercom": ic,
            "poke": lambda why: poke(app, why),
            "following": lambda: following(app),
            "tailscale_ip": tailscale_ip,
            "eyes": lambda: _eyes,
            "timer": app["timer"],
            "barometer": app["barometer"],
            "idle": lambda: time.monotonic() - app["attn"]["at"]})
        app["chest"].start()
        if app["speech"]:
            # Her stock replies, then (unless each line costs) every time of
            # day from now on, made ahead in the background so they're instant.
            now = time.time() // 60 * 60
            times = [] if app["speech"].costly else [spoken_time(now + 60 * m) for m in range(24 * 60)]
            app["speech"].prepare(replies.fixed_lines() + times)

    async def cleanup(app):
        app["hatshow"].cancel()
        await asyncio.gather(app["hatshow"], return_exceptions=True)
        app["hat"].close()
        app["chest"].stop()
        app["cyd"].close()
        if app["voice"]:
            app["voice"].close()
        if app["speech"]:
            app["speech"].close()
        app["music"].duck(False)
        if app["eyes"]:
            app["eyes"].close()
        gaze(app, None)
        app["watch"].cancel()
        app["gwatch"].cancel()
        app["rwatch"].cancel()
        app["attention"].cancel()
        app["placewatch"].cancel()
        app["outwatch"].cancel()
        app["memwatch"].cancel()
        app["drivesrun"].cancel()
        app["newsrun"].cancel()
        app["readrun"].cancel()
        app["memory"].close()
        app["timerrun"].cancel()
        app["storm"].cancel()
        await asyncio.to_thread(app["camera"].stop_recording)
        app["motors"].close()

    app.on_startup.append(start_watch)
    app.on_cleanup.append(cleanup)
    print("Patti: http://%s:%d/  (motors %s, gimbal %s)"
          % (bind, args.port, ("online" if app["motors"].ok else "OFFLINE")
             + (", TABLE MODE" if app["motors"].table else ""),
             "online" if app["gimbal"].ok else "OFFLINE"))
    # The live video and control sockets never finish by themselves, so
    # don't wait the default 60 s for them when stopping.
    # and on 127.0.0.1, for `say` and `ask` on the Pi (talk.py)
    hosts = [bind] if bind in ("0.0.0.0", "::") + LOCAL else [bind, "127.0.0.1"]
    web.run_app(app, host=hosts, port=args.port, print=None, shutdown_timeout=2)


if __name__ == "__main__":
    main()
