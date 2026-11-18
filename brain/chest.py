"""Her chest: the CYD's screens, driven from the brain (server.py).

The CYD (cyd/, through cyd.py) has an icon bar for seven screens: home
(clock, weather, status), music, system, heard, mood, forecast and settings.
They can be picked by tapping the bar or with the Sense HAT joystick, which
is either on the bar (its icon filled in) or in a screen (its icon outlined):

  on the bar
    up / down       previous / next screen
    right, press    into the screen (mood, settings, music)
  in a screen
    up / down       move through the list; on music, the previous / next song
    press           pick it, or change it (tapping a line does the same); on
                    music, play or pause (so does tapping it)
    left            back out to the bar

Settings can open a terminal (term.py): the whole screen, in colour, showing
the Linux console (type on a USB keyboard plugged into the Pi), htop or her
logs. A tap, or the joystick left or right, leaves it.

When she hears speech or talks, the CYD switches to the voice screen by itself
(two waveforms, heard and said, and the words as they're recognised), and
goes back a few seconds after it's quiet again. When a new song starts
playing through her (music.py), it switches to the music screen. While she
talks, the music is turned down.

The backlight follows her: full while she's in use, dimmed after IDLE_S with
nothing happening or once her eyes are drowsy or asleep, and (with "Screen
off" on) off after SLEEP_OFF_S of that; anything that wakes her brings it
back. "Auto bright" also turns it down in a dark room (the CYD's light sensor).

"Take a note" puts the note screen up (notes.py): she listens till you stop
talking (press or tap to finish sooner, left to cancel), shows what she heard
for NOTE_REVIEW_S, and saves it to Joplin (press to save now; left or a tap
throws it away).

Settings (theme, scanlines, brightness, auto bright, screen off, the Sense
HAT's idle display) are kept in
CONF (her voice's volume and engine are speech.py's own); reboot and shut down need picking twice. Holding the joystick in for 10 s still shuts her down
(pi/sensehat-shutdown), in case the brain isn't answering.
"""
import asyncio
import glob
import json
import os
import shutil
import struct
import tempfile
import textwrap
import subprocess
import sys
import threading
import time

import hat
import notes
import replies
import term
import themes
import weather

CONF = os.path.expanduser("~/.config/robotpattinson/chest.json")
SCREENS = ["home", "music", "system", "heard", "mood", "forecast", "settings"]   # the icon bar, in order
LINES = 12
WEATHER_S = 15 * 60
FORECAST_S = 30 * 60
VOICE_HOLD_S = 4.0              # stay on the voice screen this long after the last speech
VOICE_HZ = 15
MUSIC_S = 1.0                   # how often to look at what's playing
TERM_S = 0.1                    # how often to look at the terminal for changes
TERMINALS = ["console", "htop", "logs"]
MUSIC_SYNC_S = 10.0             # resend the song's position this often (the CYD counts on by itself)
CONFIRM_S = 5.0
NOTE_REVIEW_S = 8               # a note's on screen this long before it's saved
NOTE_COLS = 33                  # characters across a list line
BRIGHTNESS = [60, 100, 140, 180, 220, 255]
IDLE_S = 5 * 60                 # dim after this long with nothing happening
SLEEP_OFF_S = 15 * 60           # off after this long drowsy or asleep (if "Screen off" is on)
DIM = 0.25                      # dimmed, as a fraction of the brightness setting
LIGHT = (100, 1500)             # the CYD's light sensor, raw, in a lit room and a dark one
NAMES = ("patti", "patty", "pattie")    # as voice.NAMES

# the Sense HAT joystick, as a Linux input device
EVENT = struct.Struct("llHHi")
EV_KEY = 1
KEYS = {103: "up", 108: "down", 105: "left", 106: "right", 28: "press"}
DIRS = ["up", "right", "down", "left"]


def turned_keys():
    """KEYS for the HAT as it's mounted (ROTATE, as the matrix uses): turned
    a quarter anticlockwise for each 90 degrees, so up is still up."""
    turns = hat.settings()[0] // 90
    return {code: DIRS[(DIRS.index(k) - turns) % 4] if k in DIRS else k for code, k in KEYS.items()}


def load_settings():
    s = {"theme": "orange inverse", "font": "Terminus", "scan": "on", "bright": 140, "auto": "on", "sleep": "on", "listen": "on", "notes": "whisper",
         "term": "console", "hat": hat.IDLES[0]}
    try:
        with open(CONF) as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    s["theme"] = themes.OLD.get(s["theme"], s["theme"])
    return s


def wifi_name():
    """The wifi connection she's on, from NetworkManager ("" if none)."""
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        typ, _, rest = line.partition(":")
        state, _, name = rest.partition(":")
        if typ == "wifi" and state == "connected":
            return name.replace("\\:", ":")
    return ""


def wifi_ip():
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "wlan0"], capture_output=True, text=True, timeout=5).stdout
        return out.split()[3].split("/")[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def throttled():
    """The Pi's power and heat warnings, from vcgencmd ("" if all's well)."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5).stdout
        v = int(out.strip().split("=")[1], 16)
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return "?"
    if v & 0x1:
        return "UNDERVOLTAGE NOW"
    if v & 0x4:
        return "THROTTLED NOW"
    if v & 0x10000:
        return "undervoltage earlier"
    if v & 0x40000:
        return "throttled earlier"
    return ""


def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except (OSError, ValueError):
        return 0


def ago(seconds):
    m = int(seconds // 60)
    return "%dD %dH" % (m // 1440, m // 60 % 24) if m >= 1440 else "%dH %dM" % (m // 60, m % 60) if m >= 60 else "%dM" % m


def heard_line(text, name=None):
    """'patty what time is it' -> 'Josh: Patti, what time is it' (her name as
    she spells it; who said it, when the brain knows from their voice)."""
    words = text.split()
    if words and words[0] in NAMES:
        words = ["Patti,"] + words[1:]
    line = " ".join(words)
    return "%s: %s" % (name, line) if name else line


def spoken_command(text):
    """'patti do happy' -> 'do happy'."""
    words = text.split()
    if len(words) > 1 and words[0] in NAMES:
        words = words[1:]
    return " ".join(words)


class Chest:
    def __init__(self, app, cyd, hat, voice, eyes, motors, ctx):
        """ctx: functions from server.py: poke(why), following(), tailscale_ip(), eyes() -> the Eyes or None."""
        self.app, self.cyd, self.hat, self.voice, self.motors, self.ctx = app, cyd, hat, voice, motors, ctx
        self.screen = "home"
        self.focus = "bar"              # where the joystick is: "bar" or "page" (in the screen)
        self.back = "home"              # where to go after the voice screen
        self.sel = {"mood": 0, "settings": 0}
        self.top = {"mood": 0}          # first item shown, when a list is longer than the screen
        self.confirm = (None, 0.0)      # (what, until)
        self.settings = load_settings()
        self.started = time.monotonic()
        self.forecast = None
        self.heard_seen = 0.0
        self.voice_until = 0.0
        self.said = self.cmd = None
        self.loop = None
        self.system = []
        self.drowsy_since = None
        self.speech, self.music = ctx.get("speech"), ctx.get("music")
        self.song = None
        self.term = None                # (term.Term, the task feeding it to the CYD) while it's open
        self.note = None                # a voice note being taken: {"lines": [...], "say": ..., ...}

    # ---------- hooks (cyd.py's and the joystick's threads hand over to the loop) ----------
    def start(self):
        self.loop = asyncio.get_running_loop()
        self.cyd.on_event = lambda words: self.loop.call_soon_threadsafe(self.event, words)
        self.cyd.on_touch = lambda kind, x, y: self.loop.call_soon_threadsafe(self.touched, kind, x, y)
        threading.Thread(target=self._joystick, name="joystick", daemon=True).start()
        self.apply_settings()
        self.tasks = [asyncio.create_task(self.watch()), asyncio.create_task(self.voice_watch()),
                      asyncio.create_task(self.music_watch()), asyncio.create_task(self.notes_watch())]

    def stop(self):
        for t in getattr(self, "tasks", []):
            t.cancel()

    def event(self, words):
        if words == ["tap", "term"]:
            self.close_term()
        elif words == ["tap", "mic"]:             # the mic icon, bottom right
            self.set_listening(self.settings["listen"] != "on")
            self.refresh()
        elif words[0] == "hello":                 # the CYD rebooted: it's back on home
            self.close_term(go=False)
            self.screen = "home"
            self.set_focus("bar")
        elif words[0] == "screen" and len(words) == 2:
            self.screen = words[1]
            self.ctx["poke"]("chest screen")
            self.set_focus("bar")
        elif words[0] == "tap" and len(words) == 3 and words[1] == "note":
            self.note_key("tap")
        elif words[0] == "tap" and len(words) == 3 and words[1] == "music":
            self.ctx["poke"]("chest tapped")
            self.control("toggle")
        elif words[0] == "tap" and len(words) == 3 and words[1] == self.screen:
            n = int(words[2]) + self.top.get(self.screen, 0)
            self.sel[self.screen] = n
            self.set_focus("page")
            self.pick(n)

    def touched(self, kind, x, y):
        if kind != "touch":
            return
        self.ctx["poke"]("chest touched")
        sp = self.ctx.get("speech")
        if sp and sp.speaking:          # a tap while she talks: hush
            sp.hush()
            return
        if self.ctx.get("timer") and self.ctx["timer"].dismiss():
            return
        if self.screen == "home" and x > 40:    # a pat on the chest (not the icon bar)
            e = self.ctx["eyes"]()
            if e:
                e.blink()
            if self.hat.m:
                asyncio.create_task(self.hat.pop("heart", 2))

    def _joystick(self):
        while True:
            dev = None
            for path in glob.glob("/sys/class/input/event*/device/name"):
                with open(path) as f:
                    if "Sense HAT Joystick" in f.read():
                        dev = "/dev/input/" + path.split("/")[4]
            if not dev:
                time.sleep(5)
                continue
            keys = turned_keys()
            try:
                with open(dev, "rb") as f:
                    while True:
                        data = f.read(EVENT.size)
                        _, _, typ, code, value = EVENT.unpack(data)
                        # 1 = pressed, 2 = held and repeating (only for moving through lists)
                        if typ == EV_KEY and code in keys and (value == 1 or (value == 2 and keys[code] in ("up", "down"))):
                            self.loop.call_soon_threadsafe(self.stick, keys[code])
            except OSError as e:
                print("chest: joystick lost (%s)" % e, file=sys.stderr)
                time.sleep(5)

    def stick(self, key):
        self.ctx["poke"]("joystick")
        if self.ctx.get("timer") and self.ctx["timer"].dismiss():    # a ringing timer: that's all it does
            return
        if self.screen == "term":
            if key in ("left", "right"):
                self.close_term()
            return
        if self.screen == "note":
            self.note_key(key)
        elif self.screen == "voice":
            if key != "press":
                self.voice_until = 0
                self.go(self.back)
        elif self.focus == "bar":
            if key in ("up", "down"):
                i = SCREENS.index(self.screen) if self.screen in SCREENS else 0
                self.go(SCREENS[(i + (1 if key == "down" else -1)) % len(SCREENS)])
            elif key in ("right", "press") and (self.screen in self.sel or self.screen == "music"):
                self.set_focus("page")
        elif key == "left":
            self.set_focus("bar")
        elif self.screen == "music" and key in ("up", "down", "press"):
            self.control({"up": "previous", "down": "next", "press": "toggle"}[key])
        elif key in ("up", "down") and self.screen in self.sel:
            items = self.items()
            n = self.sel[self.screen]
            step = 1 if key == "down" else -1
            for _ in range(len(items)):
                n = (n + step) % len(items)
                if items[n][1]:                  # skip the blank ones
                    break
            self.sel[self.screen] = n
            self.refresh()
        elif key == "press" and self.screen in self.sel:
            self.pick(self.sel[self.screen])

    def go(self, screen, focus="bar"):
        self.screen = screen
        self.cyd.send("screen " + screen)
        self.set_focus(focus)

    def set_focus(self, focus):
        """The joystick on the bar or in the screen: the CYD fills in or
        outlines the screen's icon, and a list's line is only lit in it."""
        self.focus = focus
        self.cyd.keep("focus", "focus " + focus)
        self.refresh()

    # ---------- the list screens ----------
    def items(self):
        """The current screen's lines: [(colour, text)], any length."""
        s = self.screen
        if s == "mood":
            e = self.ctx["eyes"]()
            moods = list(sys.modules["eyes"].MOODS) if e else []
            return [("white", ("* " if e.mood == m else "  ") + m) for m in moods] or [("grey", "No eyes")]
        if s == "settings":
            st = self.settings
            what, until = self.confirm
            sure = lambda w: time.monotonic() < until and what == w
            light = self.cyd.light
            return [("white", "Theme        " + st["theme"]),
                    ("white", "Font         " + st["font"]),
                    ("white", "Scanlines    " + st["scan"]),
                    ("white", "Brightness   %d" % st["bright"]),
                    ("white", "Auto bright  " + st["auto"] + ("" if light is None else "  (light %d)" % light)),
                    ("white", "Screen off   " + ("after 15 min asleep" if st["sleep"] == "on" else "never")),
                    ("white", "Table mode   " + ("on" if self.motors.table else "off")),
                    ("white" if self.speech else "grey",
                     "Voice volume  " + ("%d%%" % round(self.speech.volume * 100) if self.speech else "off")),
                    ("white" if self.voice else "grey", "Listening    " + (self.settings["listen"] if self.voice else "no mic")),
                    ("white", "Terminal     " + st["term"]),
                    ("white", "Open terminal"),
                    ("white", "Notes by     " + st["notes"]),
                    ("white", "HAT idle     " + st["hat"]),
                    ("white" if self.speech else "grey", "Voice        " + (self.speech.mode if self.speech else "off")),
                    ("white", ""),
                    ("red" if sure("reboot") else "white", "Reboot? Pick again" if sure("reboot") else "Reboot"),
                    ("red" if sure("shutdown") else "white", "Shut down? Pick again" if sure("shutdown") else "Shut down")]
        if s == "heard":
            log = list(self.voice.log)[::-1] if self.voice else []
            if not log:
                return [("grey", "Nothing yet" if self.voice and self.voice.ok else "Not listening")]
            return [("white" if act else "grey", time.strftime("%H:%M ", time.localtime(t)) + text) for t, text, act in log]
        if s == "forecast":
            if not self.forecast:
                return [("grey", "No forecast")]
            p = weather.place() or {}
            out = [("grey", p.get("name", ""))]
            b = self.ctx.get("barometer")
            pressure = b and b.words()
            if pressure:
                falling_fast = (b.tendency() or ("", "", 0))[2] <= -3.6
                out.append(("amber" if falling_fast else "white", "Pressure " + pressure))
            for day, words, hi, lo, rain in self.forecast:
                out.append(("white", "%-6s %3s/%-3s %3s%%" % (day, hi, lo, rain)))
                out.append(("grey", "       " + words))
            return out
        if s == "system":
            return self.system
        if s == "note":
            return self.note["lines"] if self.note else []
        return []

    def refresh(self):
        """Send the current list screen's lines (only changed ones go)."""
        s = self.screen
        if s in ("home", "voice", "music", "term"):
            self.cyd.flush()
            return
        items = self.items()
        top = 0
        if s in self.sel:
            n = self.sel[s] = min(self.sel[s], len(items) - 1)
            top = self.top.get(s, 0)
            top = max(min(top, n), n - LINES + 1)
            self.top[s] = top
            self.cyd.keep("sel " + s, "sel %s %d" % (s, n - top if self.focus == "page" else -1))
        for i in range(LINES):
            c, t = items[top + i] if top + i < len(items) else ("white", "")
            self.cyd.line(s, i, c, t)
        self.cyd.flush()

    def pick(self, n):
        s = self.screen
        items = self.items()
        if not 0 <= n < len(items) or not items[n][1]:
            return
        if s == "mood":
            e = self.ctx["eyes"]()
            if e:
                e.set_mood(list(sys.modules["eyes"].MOODS)[n])
        elif s == "settings":
            st = self.settings
            if n == 0:
                st["theme"] = themes.next_in(themes.THEMES, st["theme"])
            elif n == 1:
                st["font"] = themes.next_in(themes.FONTS, st["font"])
            elif n == 2:
                st["scan"] = "off" if st["scan"] == "on" else "on"
            elif n == 3:
                i = BRIGHTNESS.index(st["bright"]) if st["bright"] in BRIGHTNESS else 0
                st["bright"] = BRIGHTNESS[(i + 1) % len(BRIGHTNESS)]
            elif n == 4:
                st["auto"] = "off" if st["auto"] == "on" else "on"
            elif n == 5:
                st["sleep"] = "off" if st["sleep"] == "on" else "on"
            elif n == 6:
                self.motors.set_table(not self.motors.table)
            elif n == 7 and self.speech:
                v = self.speech.volume
                vols = sys.modules["speech"].VOLUMES
                self.speech.set_volume(vols[(vols.index(v) + 1) % len(vols)] if v in vols else vols[-1])
                self.speech.say(replies.pick("louder"))
            elif n == 8:
                self.set_listening(st["listen"] != "on")
            elif n == 9:
                st["term"] = TERMINALS[(TERMINALS.index(st["term"]) + 1) % len(TERMINALS)] \
                    if st["term"] in TERMINALS else TERMINALS[0]
                self.save_settings()
            elif n == 10:
                self.open_term()
                return
            elif n == 11:
                st["notes"] = themes.next_in(notes.ENGINES, st["notes"])
                self.save_settings()
            elif n == 12:
                self.set_hat_idle(themes.next_in(hat.IDLES, st["hat"]))
            elif n == 13 and self.speech:
                self.set_voice(themes.next_in(sys.modules["speech"].MODES, self.speech.mode))
            elif n in (15, 16):
                what = "reboot" if n == 15 else "shutdown"
                if self.confirm[0] == what and time.monotonic() < self.confirm[1]:
                    self.cyd.text("Rebooting" if what == "reboot" else "Shutting down")
                    print("chest: %s, from the settings screen" % what, file=sys.stderr)
                    subprocess.Popen(["sudo", "-n", "systemctl", "reboot" if what == "reboot" else "poweroff"])
                    return
                self.confirm = (what, time.monotonic() + CONFIRM_S)
            if n in (0, 1, 2, 3, 4, 5):
                self.save_settings()
                self.apply_settings()
        self.refresh()

    def set_hat_idle(self, idle):
        """What the Sense HAT shows when she's idle (hat.IDLES: Settings, "game
        of life"); kept."""
        self.settings["hat"] = idle
        print("chest: HAT idle %s" % idle, file=sys.stderr)
        self.save_settings()
        self.apply_settings()

    def set_voice(self, mode):
        """Her voice engine (speech.MODES: Settings, "use your free voice"),
        kept; she says so in the new one."""
        if not self.speech:
            return
        self.ctx["poke"]("voice " + mode)
        self.speech.set_engine(mode, replies.fixed_lines())
        self.speech.say(replies.pick("voice_" + mode))

    def set_listening(self, on):
        """Turn her ears on or off (the mic icon, Settings, "stop listening"); kept."""
        self.settings["listen"] = "on" if on else "off"
        print("chest: listening %s" % self.settings["listen"], file=sys.stderr)
        self.ctx["poke"]("listening " + self.settings["listen"])
        self.save_settings()
        self.apply_settings()

    def save_settings(self):
        try:
            os.makedirs(os.path.dirname(CONF), exist_ok=True)
            with open(CONF, "w") as f:
                json.dump(self.settings, f)
        except OSError as e:
            print("chest: couldn't save settings (%s)" % e, file=sys.stderr)

    def apply_settings(self):
        st = self.settings
        self.cyd.keep("theme", themes.palette_command(st["theme"]))
        self.cyd.keep("font", themes.font_command(st["font"]))
        self.cyd.keep("scan", "scan " + st["scan"])
        self.cyd.keep("auto", "auto %s %d %d" % ((st["auto"],) + LIGHT))
        self.cyd.keep("mic", "mic " + st["listen"])
        self.hat.idle = st["hat"] if st["hat"] in hat.IDLES else hat.IDLES[0]
        self.hat.face = themes.face_colour(st["theme"])     # her face, in the screen's colours
        if self.voice:
            self.voice.muted = st["listen"] != "on"
        self.backlight()

    def backlight(self):
        """Full while she's in use, dim when she's idle or sleepy, off after a
        long sleep (if Screen off is on)."""
        st = self.settings
        e = self.ctx["eyes"]()
        now = time.monotonic()
        sleepy = e and e.state in ("drowsy", "asleep")
        if not sleepy:
            self.drowsy_since = None
        elif self.drowsy_since is None:
            self.drowsy_since = now
        if sleepy and st["sleep"] == "on" and now - self.drowsy_since > SLEEP_OFF_S:
            level = 0
        elif sleepy or self.ctx["idle"]() > IDLE_S:
            level = max(20, int(st["bright"] * DIM))
        else:
            level = st["bright"]
        self.cyd.keep("bright", "bright %d" % level)
        # the blinkenlights follow her being in use, idle or asleep (not the
        # room's light); hat.py keeps them from going out
        self.hat.frac = max(0.0, min(1.0, level / max(1, st["bright"])))

    # ---------- keeping it up to date ----------
    async def watch(self):
        """The home screen's lines once a second, the clock every hour, the
        weather and forecast now and then, and the list screen on show."""
        c, m, v = self.cyd, self.motors, self.voice
        cam, g = self.app["camera"], self.app["gimbal"]
        net, net_at = ("", "", ""), -1e9
        clock_at = weather_at = forecast_at = -1e9
        self.system = []
        while True:
            now = time.monotonic()
            if now - clock_at > 3600:
                c.keep("time", lambda: "time %d %d" % (int(time.time()), time.localtime().tm_gmtoff))
                clock_at = now
            if now - weather_at > WEATHER_S:
                line = await asyncio.to_thread(weather.now)
                if line or "weather" not in c.kept:
                    c.keep("weather", line or "weather - - - none")
                weather_at = now
            if now - forecast_at > FORECAST_S:
                self.forecast = await asyncio.to_thread(weather.forecast) or self.forecast
                forecast_at = now
            if now - net_at > 30:
                net = (await asyncio.to_thread(wifi_name), await asyncio.to_thread(self.ctx["tailscale_ip"]),
                       await asyncio.to_thread(wifi_ip))
                net_at = now
            e = self.ctx["eyes"]()
            state = (e.state if e else "awake").capitalize()
            if e and e.mood != "normal":
                state += ", " + e.mood
            c.row(0, "blue", state)
            heard = v.status()["heard"] if v else ""
            c.row(1, "white", 'Heard "%s"' % heard if heard else "")
            c.row(2, "white" if m.ok else "grey",
                  "Motors   " + ("table mode" if m.table and m.ok else "online" if m.ok else "offline"))
            c.row(3, "white" if g.ok else "grey",
                  "Gimbal   " + (("following" if self.ctx["following"]() else "online") if g.ok else "offline"))
            c.row(4, "red" if cam.rec else "amber" if cam.error else "white",
                  "Camera   " + ("RECORDING" if cam.rec else cam.error or "ok"))
            c.row(5, "grey" if not v or not v.ok else "amber" if v.muted else "white",
                  "Voice    " + ("off" if not v else "not listening" if v.muted else "listening" if v.ok else
                                 "no mic" if "No such device" in (v.error or "") else (v.error or "offline")[:20]))
            c.row(6, "red" if not net[0] else "white" if net[1] else "amber",
                  ("Wifi     " + net[0][:20] if net[0] else "No wifi") + ("" if net[1] else ", no Tailscale"))
            temp = cpu_temp()
            c.row(7, "red" if temp > 75 else "grey", "CPU      %.0fC" % temp)
            if self.screen == "system":
                self.system = await asyncio.to_thread(self.system_lines, net, temp)
            self.backlight()
            if self.confirm[0] and now > self.confirm[1]:
                self.confirm = (None, 0.0)
            self.refresh()
            await asyncio.sleep(1)

    def system_lines(self, net, temp):
        mem = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":")
                    mem[k] = int(v.split()[0]) // 1024
            with open("/proc/uptime") as f:
                up = float(f.read().split()[0])
        except (OSError, ValueError):
            up = 0
        disk = shutil.disk_usage("/")
        power = throttled()
        return [("white", "CPU        %3.0f%%  %.0fC" % (self.hat.load * 100, temp)),
                ("white", "Memory     %d/%d MB" % (mem.get("MemTotal", 0) - mem.get("MemAvailable", 0), mem.get("MemTotal", 0))),
                ("white", "Disk       %.1f/%.0f GB" % (disk.used / 1e9, disk.total / 1e9)),
                ("red" if "NOW" in power else "amber" if power else "white", "Power      " + (power or "ok")),
                ("white", "Up         " + ago(up)),
                ("white", "Brain up   " + ago(time.monotonic() - self.started)),
                ("white", ""),
                ("white" if net[0] else "grey", "Wifi       " + (net[0] or "none")[:20]),
                ("white" if net[2] else "grey", "IP         " + (net[2] or "-")),
                ("white" if net[1] else "grey", "Tailscale  " + (net[1] or "-"))]

    async def voice_watch(self):
        """Switch to the voice screen when she hears speech or talks, and feed it."""
        v, sp = self.voice, self.speech
        while True:
            await asyncio.sleep(1 / VOICE_HZ)
            talking = bool(sp and sp.speaking)
            ic = self.ctx.get("intercom")
            if self.music:
                await asyncio.to_thread(self.music.duck, talking or bool(ic and ic.talking))
            if not talking and (not v or not v.ok):
                continue
            now = time.monotonic()
            speaking = bool(v and v.ok and now - v.speech_at < 0.6)
            new = bool(v and v.heard_at > self.heard_seen)
            if (speaking or new or talking) and self.screen not in ("term", "note") and not self.note:
                self.voice_until = now + VOICE_HOLD_S
                if self.screen != "voice":
                    self.back = self.screen
                    self.said = self.cmd = None
                    self.go("voice")
            if self.screen != "voice":
                continue
            if now > self.voice_until:
                self.cyd.send("said")
                self.cyd.send("cmd")
                self.go(self.back)
                continue
            lvl = max(0.0, min(1.0, (v.level_db - v.floor_db - 3) / 30)) if v and v.ok and not talking else 0.0
            out = sp.level() if sp else 0.0
            self.cyd.send("lvl %d %d" % (lvl * 100, out * 100))
            if not v:
                continue
            # Who said it comes a moment after the words (server.py, who_spoke).
            spk = getattr(v, "speaker", None)
            name = spk[1] if spk and spk[0] == v.heard_at else None
            if new:
                self.heard_seen = v.heard_at
                last = v.log[-1] if v.log else None
                said = heard_line(v.heard, name)
                cmd = spoken_command(last[1]) if last and last[1] == v.heard and last[2] else ""
            elif not v.partial and name and v.heard_at == self.heard_seen:
                said, cmd = heard_line(v.heard, name), self.cmd or ""
            elif v.partial:
                said = v.partial.replace("[unk]", "...")
                cmd = "" if speaking else self.cmd or ""
            else:
                said, cmd = self.said or "", self.cmd or ""
            if said != self.said:
                self.cyd.send("said " + said)
                self.said = said
            if cmd != self.cmd:
                self.cyd.send("cmd " + cmd)
                self.cmd = cmd

    # ---------- music ----------
    def control(self, what):
        if self.music:
            song = self.song
            asyncio.get_running_loop().run_in_executor(None, self.music.control, what, song)

    async def music_watch(self):
        """Keep the music screen up to date, and show it when a new song starts."""
        while self.music:
            try:
                await self._music_watch()
            except Exception as e:      # keep the screen going whatever happens here
                print("chest: music: %r" % e, file=sys.stderr)
                await asyncio.sleep(5)

    async def _music_watch(self):
        c = self.cyd
        last_key, synced = None, -1e9
        while True:
            await asyncio.sleep(MUSIC_S)
            song = self.song = await asyncio.to_thread(self.music.now)
            if not song:
                if last_key is not None:
                    c.keep("np", "np none")
                    for k in ("np song", "np artist", "np album", "np from", "np at"):
                        c.kept.pop(k, None)
                last_key = None
                continue
            key = (song["title"], song["artist"], song["album"])
            now = time.monotonic()
            if key != last_key:
                print("music: %s - %s (%s)" % (song["artist"], song["title"], song["from"]), file=sys.stderr)
                c.keep("np song", "np song " + song["title"])
                c.keep("np artist", "np artist " + song["artist"])
                c.keep("np album", "np album " + song["album"])
                c.keep("np from", "np from " + song["from"])
                c.kept.pop("np", None)
                if song["playing"]:
                    self.ctx["poke"]("a new song")
                    if self.screen == "voice":
                        self.back = "music"
                    elif self.screen not in ("music", "term"):
                        self.go("music")
                last_key = key
                synced = -1e9
            at = "np at %d %d %d" % (song["playing"], song["pos"] or 0, song["len"] or 0)
            sent = c.kept.get("np at", "")
            if sent.split()[:3] != at.split()[:3] or now - synced > MUSIC_SYNC_S:
                c.keep("np at", at)
                synced = now

    # ---------- voice notes ----------
    def take_note(self):
        """Start one (False if she can't: no mic, or one's going already)."""
        if self.note or not self.voice or not self.voice.ok or self.voice.muted:
            return False
        self.note = {"lines": [], "key": None, "rec": None}
        asyncio.get_running_loop().create_task(self._note())
        return True

    def note_key(self, key):
        """The joystick or a tap on the note screen."""
        if not self.note:
            if key == "left":
                self.go(self.back if self.back in SCREENS else "home")
            return
        rec = self.note["rec"]
        if rec and not rec.done.is_set():
            if key in ("press", "tap"):
                rec.end()
            elif key == "left":
                self.note["key"] = "cancel"
                rec.end()
        elif key == "press":
            self.note["key"] = "save"
        elif key in ("left", "tap"):
            self.note["key"] = "discard"

    def note_show(self, *lines):
        """[(colour, text)], or strings (white)."""
        if self.note:
            self.note["lines"] = [ln if isinstance(ln, tuple) else ("white", ln) for ln in lines]
            if self.screen == "note":
                self.refresh()

    async def _note(self):
        n, v, sp = self.note, self.voice, self.speech
        if self.screen not in ("note", "voice", "term"):
            self.back = self.screen
        self.go("note", "page")
        wav = None
        try:
            self.note_show("Listening...")
            await asyncio.sleep(0.3)
            while sp and sp.speaking:               # her "go ahead" first
                await asyncio.sleep(0.1)
            rec = n["rec"] = notes.Recording(v.floor_db)
            v.tap = rec.feed
            while not rec.done.is_set():
                self.note_show("Listening...  %d s" % rec.seconds,
                               ("white", "=" * int(rec.level * NOTE_COLS)),
                               "",
                               ("grey", "Stop talking to finish, or"),
                               ("grey", "press: done   left: cancel"))
                await asyncio.sleep(0.2)
            v.tap = None
            if n["key"] == "cancel":
                return
            if not rec.heard:
                self.note_show(("grey", "Didn't hear anything"))
                self.say("note_empty")
                await asyncio.sleep(3)
                return
            fd, wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            rec.wav(wav)
            text, errors = "", []
            first = self.settings["notes"]
            for engine in [first] + [e for e in notes.ENGINES if e != first]:
                self.note_show("Writing it down...", ("grey", "(%s, %d s of talking)" % (engine, rec.seconds)))
                try:
                    text = await asyncio.to_thread(notes.transcribe, wav, engine)
                    break
                except (OSError, ValueError, KeyError) as e:
                    print("chest: note: %s" % e, file=sys.stderr)
                    errors.append(str(e))
            if not text:
                self.note_show(("grey", "Couldn't make that out") if not errors else ("red", "Couldn't transcribe it"),
                               *[("grey", ln) for e in errors for ln in textwrap.wrap(e, NOTE_COLS)][:6])
                self.say("note_empty")
                await asyncio.sleep(4)
                return
            body = textwrap.wrap(text, NOTE_COLS)
            if len(body) > 9:
                body = body[:8] + ["..."]
            end = time.monotonic() + NOTE_REVIEW_S
            while n["key"] not in ("save", "discard") and time.monotonic() < end:
                self.note_show(*body, "", ("grey", "Saving in %d   press: save" % (end - time.monotonic() + 1)),
                               ("grey", "left or tap: throw it away"))
                await asyncio.sleep(0.25)
            if n["key"] == "discard":
                self.note_show(("grey", "Thrown away"))
                await asyncio.sleep(1.5)
                return
            self.note_show(*body, "", ("grey", "Saving..."))
            err = await asyncio.to_thread(notes.save, text, wav)
            if err:
                self.note_show(*body, "", ("amber", "Not in Joplin: kept on the Pi"),
                               *[("grey", ln) for ln in textwrap.wrap(err, NOTE_COLS)][:2])
                self.say("note_failed")
            else:
                self.note_show(*body, "", ("green", "Saved to Joplin"))
                self.say("noted")
            await asyncio.sleep(3)
        finally:
            v.tap = None
            self.note = None
            if wav and os.path.exists(wav):
                os.remove(wav)
            if self.screen == "note":
                self.go(self.back if self.back in SCREENS else "home")

    async def notes_watch(self):
        """Joplin synced every so often, with any notes that missed it."""
        while True:
            await asyncio.sleep(notes.SYNC_S)
            if os.path.exists(notes.JOPLIN):
                await asyncio.to_thread(notes.catch_up)

    def say(self, key):
        if self.speech:
            self.speech.say(replies.pick(key))

    # ---------- the terminal ----------
    def open_term(self):
        """The whole screen as a terminal (Settings' choice), till a tap or the joystick."""
        self.close_term(go=False)
        try:
            t = term.Term(self.settings["term"])
        except Exception as e:
            print("chest: terminal: %r" % e, file=sys.stderr)
            return
        print("chest: terminal (%s)" % self.settings["term"], file=sys.stderr)
        self.back = self.screen
        self.cyd.send("tsize %d %d" % (term.ROWS, term.COLS))
        self.go("term")
        self.term = (t, asyncio.get_running_loop().create_task(self._feed_term(t)))

    def close_term(self, go=True):
        if not self.term:
            return
        t, task = self.term
        self.term = None
        task.cancel()
        threading.Thread(target=t.close, daemon=True).start()
        if go:
            self.go(self.back if self.back in SCREENS else "home", "page")

    async def _feed_term(self, t):
        """Send the CYD what's changed, ten times a second."""
        last, cursor = None, None
        while True:
            try:
                rows, cols, cells, cur = await asyncio.to_thread(t.screen)
            except Exception as e:
                print("chest: terminal: %r" % e, file=sys.stderr)
                await asyncio.sleep(2)
                continue
            if last is None or len(last) != len(cells):
                self.cyd.send("tsize %d %d" % (rows, cols))
                last = None
            for r, c, at, text in term.diff(last, cells):
                self.cyd.send_raw(b"T %d %d %x%x %s|" % (r, c, at & 15, at >> 4, text))
            last = cells
            if cur != cursor:
                self.cyd.send("TC %d %d" % (cur if cur else (-1, -1)))
                cursor = cur
            self.ctx["poke"]("terminal")
            await asyncio.sleep(TERM_S)
