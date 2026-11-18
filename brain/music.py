#!/usr/bin/env python3
"""What's playing through her, and play/pause/skip, for the chest's music
screen and her voice. Two ways in (pi/music):

    AirPlay   shairport-sync (from an iPhone), read over the session D-Bus
    DLNA      upmpdcli handing songs to MPD (BubbleUPnP on Android, Qobuz
              through upmpdcli's plugin), read over MPD's socket

Music().now() is the song (a dict) or None; it blocks for a moment, so the
brain calls it from a thread. duck(True) turns everything but her own voice
down while she talks.

    python3 music.py            # what's playing
    python3 music.py next       # or toggle, play, pause, previous
"""
import json
import re
import socket
import subprocess
import sys
import time

SPS = ["org.gnome.ShairportSync", "/org/gnome/ShairportSync", "org.gnome.ShairportSync.RemoteControl"]
MPD = ("127.0.0.1", 6600)
AIRPLAY_RATE = 44100            # AirPlay's progress is in frames at this rate
VOICE = "Voice"                 # speech.py's client name, which duck() leaves alone
DUCK = 0.3                      # music volume while she talks

# "Astradyne (2008 Remaster)" -> "Astradyne"
TIDY = re.compile(r"\s*(?:[(\[][^)\]]*(?:remaster|edition|version|mono|stereo)[^)\]]*[)\]]"
                  r"|-\s*(?:\d{4}\s*)?remaster(?:ed)?(?:\s*\d{4})?.*)$", re.I)


def tidy(s):
    return TIDY.sub("", s or "").strip() or (s or "")


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


class Music:
    def __init__(self):
        self.clock = None           # AirPlay: ((progress string, title), position, when, playing then)
        self.ducked = None          # sink input -> volume before ducking

    # ---------- what's playing ----------
    def airplay(self):
        out = run("busctl", "--user", "-j", "get-property", *SPS, "PlayerState", "ProgressString", "Metadata",
                  "Available")
        try:
            state, prog, meta, remote = (json.loads(line)["data"] for line in out.splitlines()[:4])
        except (ValueError, KeyError):
            return None
        title = meta.get("xesam:title", {}).get("data", "")
        # Paused from the phone (or by us) shows as "Stopped"; while the
        # phone's still connected (its remote control is available), that's
        # a pause: the song can be played again.
        if not title or not (state == "Playing" or remote):
            return None
        artist = meta.get("xesam:artist", {}).get("data", [])
        song = {"from": "airplay", "playing": state == "Playing", "title": tidy(title),
                "artist": ", ".join(artist) if isinstance(artist, list) else artist,
                "album": tidy(meta.get("xesam:album", {}).get("data", "")),
                "pos": None, "len": None}
        # start/now/end, in frames. The phone only sends it now and then (a
        # new song, a seek, playing again), so keep our own clock: take its
        # position when it changes while playing, count on while playing,
        # and stand still while paused (what it sends on a pause is no use).
        try:
            a, b, c = (int(v) for v in prog.split("/"))
        except ValueError:
            return song
        now, playing = time.monotonic(), song["playing"]
        last, pos, at, was = self.clock or (None, 0.0, now, False)
        if was:
            pos += now - at
        if last is None or last[1] != title or (prog != last[0] and playing):
            pos = (b - a) / AIRPLAY_RATE
        self.clock = ((prog, title), pos, now, playing)
        song["len"] = (c - a) / AIRPLAY_RATE
        song["pos"] = max(0.0, min(pos, song["len"]))
        return song

    def mpd(self, *cmds):
        """MPD's answers to cmds, as a dict (later keys win)."""
        try:
            with socket.create_connection(MPD, timeout=2) as s:
                f = s.makefile("rw", encoding="utf-8", errors="replace")
                f.readline()                            # OK MPD x.y.z
                for c in cmds:
                    f.write(c + "\n")
                f.write("close\n")
                f.flush()
                out = {}
                for line in f:
                    k, _, v = line.rstrip("\n").partition(": ")
                    if v:
                        out[k] = v
                return out
        except OSError:
            return {}

    def dlna(self):
        st = self.mpd("status", "currentsong")
        if st.get("state") not in ("play", "pause"):
            return None
        title = st.get("Title") or st.get("Name") or st.get("file", "").rsplit("/", 1)[-1]
        num = lambda k: float(st[k]) if k in st else None
        return {"from": "dlna", "playing": st["state"] == "play", "title": tidy(title),
                "artist": st.get("Artist", ""), "album": tidy(st.get("Album", "")),
                "pos": num("elapsed"), "len": num("duration")}

    def now(self):
        """The song that's playing (or else paused), or None."""
        songs = [s for s in (self.airplay(), self.dlna()) if s]
        songs.sort(key=lambda s: not s["playing"])
        return songs[0] if songs else None

    # ---------- doing something about it ----------
    def control(self, what, song=None):
        """what: toggle, play, pause, next or previous."""
        song = song or self.now()
        if not song:
            return False
        if song["from"] == "airplay":
            method = {"toggle": "PlayPause", "play": "Play", "pause": "Pause",
                      "next": "Next", "previous": "Previous"}[what]
            run("busctl", "--user", "call", *SPS, method)
        else:
            cmd = {"toggle": "pause %d" % song["playing"], "play": "pause 0", "pause": "pause 1",
                   "next": "next", "previous": "previous"}[what]
            self.mpd(cmd)
        return True

    def volume(self, step):
        """The speaker's volume up or down by step (a fraction), as a whole."""
        run("pactl", "set-sink-volume", "@DEFAULT_SINK@", "%+d%%" % round(step * 100))

    def duck(self, on):
        """Turn everything that isn't her voice down (on) or back (off)."""
        if on == (self.ducked is not None):
            return
        try:
            inputs = json.loads(run("pactl", "-f", "json", "list", "sink-inputs") or "[]")
        except ValueError:
            inputs = []
        if on:
            self.ducked = {}
            for i in inputs:
                if i.get("properties", {}).get("application.name") == VOICE:
                    continue
                vol = [int(ch["value"]) for ch in i.get("volume", {}).values()]
                if vol:
                    self.ducked[i["index"]] = max(vol)
                    run("pactl", "set-sink-input-volume", str(i["index"]), str(int(max(vol) * DUCK)))
        else:
            for idx, vol in self.ducked.items():
                run("pactl", "set-sink-input-volume", str(idx), str(vol))
            self.ducked = None


if __name__ == "__main__":
    m = Music()
    if len(sys.argv) > 1:
        sys.exit(0 if m.control(sys.argv[1]) else "Nothing playing")
    s = m.now()
    if not s:
        sys.exit("Nothing playing")
    t = lambda v: "-" if v is None else "%d:%02d" % (v // 60, v % 60)
    print("%s (%s)  %s - %s  [%s]  %s / %s" % ("Playing" if s["playing"] else "Paused", s["from"],
                                            s["artist"], s["title"], s["album"], t(s["pos"]), t(s["len"])))
