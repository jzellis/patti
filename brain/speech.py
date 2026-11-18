#!/usr/bin/env python3
"""Her voice, played through PipeWire, so it comes out of whatever speaker
the Pi is using (Bluetooth now, the 40 mm speaker on its I2S amp later).

    Speech().say("Hello")      # queued; returns at once

Three engines. The local two each run in a helper process from its own venv
(so the brain's Python needs neither):

    elevenlabs  ElevenLabs, over the internet: the best, but each new line
                costs characters from the account's monthly allowance; when it
                can't be had (out of characters, offline), FALLBACK says that
                line instead (and after running out, it isn't asked again for
                RETRY_S)
    kokoro      Kokoro (kokoro-onnx), voice bf_emma: natural, but about four
                times slower than real time on the Pi 4
    piper       Piper, voice en_GB-alba-medium: flatter, but three times
                faster than real time

Every sentence made is kept in CACHE, so anything said before plays at once;
prepare(lines) makes them ahead, in a second, low-priority helper, while she's
idle (the brain gives it her stock replies and every time of day). While she
talks, `speaking` is True (and stays True for a moment after, TAIL_S, for the
echo), so voice.py can ignore what the mic hears; and level() is how loud she
is right now, 0-1, for the chest screen's OUT trace.

Set up:

    python3 -m venv ~/.venvs/kokoro && ~/.venvs/kokoro/bin/pip install kokoro-onnx
    (model) kokoro-v1.0.int8.onnx and voices-v1.0.bin in KOKORO, from
            https://github.com/thewh1teagle/kokoro-onnx/releases (model-files-v1.0)
    python3 -m venv ~/.venvs/piper && ~/.venvs/piper/bin/pip install piper-tts
    ~/.venvs/piper/bin/python -m piper.download_voices --download-dir PIPER en_GB-alba-medium

Lines can carry stage directions for ElevenLabs, in square brackets:
"[cheeky] Y'alright?" (those lines use its v3 model, which acts them out,
and cost a little more); the local voices leave them out.

~/.config/robotpattinson/voice.json chooses: {"engine": "elevenlabs",
"kokoro", "piper" or "auto", "voice": ..., "speed": 1.0, "volume": 0-1} (and
"speaker": n for a multi-speaker Piper voice; an ElevenLabs voice is its
voice ID). "auto" is the first of AUTO; for now that's ElevenLabs, with Piper
standing in when it can't, and a Kokoro on another machine will go first once
there is one. Each engine remembers its own voice ("voices"), and
set_engine() switches between them while she's running (the chest's
Settings, "use your free voice"), keeping every engine's cache. ElevenLabs' key goes in ELEVEN_KEY, {"api_key": "..."}. The volume is her voice's alone (the music has
its own); set_volume() changes and keeps it. It's kept for each output
("volumes", by PipeWire sink name: friday's speakers want her louder than the
Bluetooth speaker), and "volume" is for any output not in there yet.

    python3 speech.py "Words"            # say it
    python3 speech.py --wav x.wav "Words"  # or just write it to a file
"""
import argparse
import collections
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import numpy as np

CONF = os.path.expanduser("~/.config/robotpattinson/voice.json")
CACHE = os.path.expanduser("~/.cache/robotpattinson/speech")
PIPER = os.path.expanduser("~/.local/share/robotpattinson/piper")
KOKORO = os.path.expanduser("~/.local/share/robotpattinson/kokoro")
ELEVEN_KEY = os.path.expanduser("~/.config/robotpattinson/elevenlabs.json")
ELEVEN_MODEL = "eleven_flash_v2_5"      # fast, and the cheapest in characters
ELEVEN_ACTING = "eleven_v3"             # for lines with stage directions: "[cheeky] Y'alright?"
DIRECTION = re.compile(r"\[[^\]]*\]\s*")  # a stage direction (the local voices leave them out)
FALLBACK = "piper"              # who speaks when ElevenLabs can't
RETRY_S = 3600                  # after it says no (out of characters), leave it this long
VOICES = {"elevenlabs": "3eeY9rFz1akxkQTWoYXs", "kokoro": "bf_emma",     # (Daisy) each engine's voice, unless voice.json says
          "piper": "en_GB-alba-medium"}
DEFAULT = {"engine": "kokoro", "voice": None, "speaker": None, "speed": 1.0, "volume": 1.0, "voices": {},
           "volumes": {}}
OUTPUT_TTL_S = 2.0              # how long to trust the last look at which output she's on
MODES = ("elevenlabs", "kokoro", "piper", "auto")     # what "engine" can be
AUTO = ("elevenlabs",)          # "auto": the first of these (ElevenLabs falls back to FALLBACK by itself)
RELOAD = object()               # (on the queue: switch engines here)
VOLUMES = [0.2, 0.4, 0.6, 0.8, 1.0]  # the steps the chest screen and "speak up" go through
BLOCK_S = 0.05                  # loudness is measured in blocks this long
TAIL_S = 0.6                    # still "speaking" this long after the sound stops
LEAD_S = 0.3                    # silence first, while a sleeping Bluetooth speaker wakes

# The helpers: read one line of JSON per sentence on stdin, write a 4-byte
# length and that many bytes of 16-bit mono audio back for each (after the
# sample rate, first). "threads" and "nice" make the background one gentle.
HELPERS = {
    "piper": (os.path.expanduser("~/.venvs/piper/bin/python"), r"""
import json, os, struct, sys
from piper import PiperVoice, SynthesisConfig
cfg = json.loads(sys.argv[1])
os.nice(cfg["nice"])
voice = PiperVoice.load(os.path.join(cfg["dir"], cfg["voice"] + ".onnx"))
sys.stdout.buffer.write(struct.pack("<I", voice.config.sample_rate)); sys.stdout.buffer.flush()
syn = SynthesisConfig(speaker_id=cfg.get("speaker"), length_scale=1.0 / (cfg.get("speed") or 1.0))
for line in sys.stdin:
    text = json.loads(line)
    audio = b"".join(c.audio_int16_bytes for c in voice.synthesize(text, syn_config=syn))
    sys.stdout.buffer.write(struct.pack("<I", len(audio)) + audio); sys.stdout.buffer.flush()
"""),
    "kokoro": (os.path.expanduser("~/.venvs/kokoro/bin/python"), r"""
import json, os, struct, sys, warnings
warnings.filterwarnings("ignore")
import numpy as np, onnxruntime as ort
from kokoro_onnx import Kokoro
cfg = json.loads(sys.argv[1])
os.nice(cfg["nice"])
opts = ort.SessionOptions()
if cfg["threads"]:
    opts.intra_op_num_threads = cfg["threads"]
session = ort.InferenceSession(os.path.join(cfg["dir"], "kokoro-v1.0.int8.onnx"), opts)
k = Kokoro.from_session(session, os.path.join(cfg["dir"], "voices-v1.0.bin"))
k.create("Ready.", voice=cfg["voice"], lang="en-gb")      # warm up
sys.stdout.buffer.write(struct.pack("<I", 24000)); sys.stdout.buffer.flush()
for line in sys.stdin:
    text = json.loads(line)
    a, sr = k.create(text, voice=cfg["voice"], speed=cfg.get("speed") or 1.0, lang="en-gb")
    audio = (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()
    sys.stdout.buffer.write(struct.pack("<I", len(audio)) + audio); sys.stdout.buffer.flush()
"""),
}


def settings():
    """voice.json, as saved: "engine" is one of MODES."""
    s = dict(DEFAULT)
    try:
        with open(CONF) as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    if s["engine"] not in MODES:
        s["engine"] = DEFAULT["engine"]
    s["voices"] = dict(s.get("voices") or {})
    s["volumes"] = dict(s.get("volumes") or {})
    return s


def engine_cfg(s):
    """The engine to use now, with its voice, for settings s."""
    e = AUTO[0] if s["engine"] == "auto" else s["engine"]
    v = s["voice"] or ""
    kind = "kokoro" if v[:3] in ("bf_", "bm_", "af_", "am_") else "piper" if "-" in v else "elevenlabs"
    return dict(s, engine=e, voice=s["voices"].get(e) or (v if v and kind == e else VOICES[e]))


def sentences(text):
    """Split into sentences (keeping their punctuation), so the first can play
    while the rest are made."""
    parts = re.split(r"(?<=[.!?;:])\s+", text.strip())
    return [p for p in parts if p]


class Helper:
    """One synthesis process."""

    def __init__(self, cfg, nice=0, threads=0):
        python, code = HELPERS[cfg["engine"]]
        conf = dict(cfg, dir=KOKORO if cfg["engine"] == "kokoro" else PIPER, nice=nice, threads=threads)
        self.proc = subprocess.Popen([python, "-c", code, json.dumps(conf)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        self.rate = int.from_bytes(self._read(4), "little")

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.proc.stdout.read(n - len(data))
            if not chunk:
                raise OSError("the voice stopped")
            data += chunk
        return data

    def synth(self, text):
        text = DIRECTION.sub("", text).strip() or "."
        self.proc.stdin.write((json.dumps(text) + "\n").encode())
        self.proc.stdin.flush()
        return self._read(int.from_bytes(self._read(4), "little"))

    def close(self):
        self.proc.kill()


class ElevenLabs:
    """ElevenLabs, with the same face as Helper. Raises OSError when it can't
    be had (no key, no internet, out of characters)."""
    rate = 24000                # the same as Kokoro's, so it can stand in

    def __init__(self, cfg):
        self.voice, self.speed = cfg["voice"], cfg.get("speed") or 1.0
        try:
            with open(ELEVEN_KEY) as f:
                self.key = json.load(f)["api_key"]
        except (OSError, ValueError, KeyError):
            raise OSError("no ElevenLabs key in %s" % ELEVEN_KEY)

    def synth(self, text):
        body = {"text": text, "model_id": ELEVEN_ACTING if DIRECTION.search(text) else ELEVEN_MODEL}
        if self.speed != 1.0:
            body["voice_settings"] = {"speed": self.speed}
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/text-to-speech/%s?output_format=pcm_24000" % self.voice,
            data=json.dumps(body).encode(), headers={"xi-api-key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise OSError("ElevenLabs: %s %s" % (e.code, e.read(300).decode(errors="replace")))
        except (urllib.error.URLError, TimeoutError) as e:
            raise OSError("ElevenLabs: %s" % e)

    def close(self):
        pass


class Speech:
    def __init__(self):
        self.conf = settings()                  # as saved ("engine" may be "auto")
        self.cfg = engine_cfg(self.conf)        # what's speaking now
        self.reprep = None                      # lines to make ahead once the next engine's up
        self.q = queue.Queue()
        self.ok = False
        self.error = ""
        self.rate = 22050
        self.levels = collections.deque()       # (time it's heard, loudness 0-1) for what's playing
        self.gen = 0                            # hush() goes up: a sentence made before it isn't played
        self.player = None                      # the paplay playing her now
        self.quiet_at = 0.0                     # when the last sound queued will have finished
        self.pending = 0                        # sentences queued or being made
        self.lock = threading.Lock()            # (for pending: two threads change it)
        self.saying = ""                        # the sentence she's on
        self.helper = None
        self.prep = collections.deque()         # sentences to make ahead
        self.stand_in = None                    # FALLBACK, when ElevenLabs can't be had
        self.eleven_off_until = 0.0             # (it said no: don't ask again till then)
        self.stopping = False
        self._output = (None, -OUTPUT_TTL_S)    # (default sink name, when looked)
        self.thread = threading.Thread(target=self._run, name="speech", daemon=True)
        self.thread.start()

    @property
    def speaking(self):
        """True from say() until just after the last of it has played."""
        return self.pending > 0 or time.monotonic() < self.quiet_at + TAIL_S

    def level(self):
        """How loud she is right now, 0-1."""
        now = time.monotonic()
        while len(self.levels) > 1 and self.levels[1][0] <= now:
            self.levels.popleft()
        if not self.levels or now > self.quiet_at:
            return 0.0
        return self.levels[0][1]

    def output(self):
        """The output she's playing on (PipeWire's default sink), or None."""
        now = time.monotonic()
        if now - self._output[1] > OUTPUT_TTL_S:
            try:
                name = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                                      text=True, timeout=2).stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                name = None
            self._output = (name, now)
        return self._output[0]

    @property
    def volume(self):
        """Her voice's volume on the output she's on now."""
        return self.conf["volumes"].get(self.output(), self.conf["volume"])

    @property
    def mode(self):
        """The engine chosen: one of MODES."""
        return self.conf["engine"]

    @property
    def cache(self):
        return os.path.join(CACHE, "%s-%s-%s" % (self.cfg["engine"], self.cfg["voice"], self.cfg["speed"]))

    def _save(self):
        try:
            os.makedirs(os.path.dirname(CONF), exist_ok=True)
            with open(CONF, "w") as f:
                json.dump({k: self.conf[k] for k in DEFAULT}, f)
        except OSError as e:
            print("speech: couldn't save %s (%s)" % (CONF, e), file=sys.stderr)

    def set_volume(self, v):
        """For the output she's on now (or, not knowing it, every other one)."""
        v, out = max(0.0, min(1.0, v)), self.output()
        if out:
            self.conf["volumes"][out] = v
        else:
            self.conf["volume"] = v
        self._save()

    def set_engine(self, mode, lines=()):
        """Switch to one of MODES (kept): after what's already queued, the next
        engine takes over, and makes `lines` ahead if they aren't cached."""
        if mode not in MODES:
            raise ValueError("no voice engine %r" % mode)
        self.conf["voices"][self.cfg["engine"]] = self.cfg["voice"]
        self.conf["engine"] = mode
        self.conf["voice"] = engine_cfg(self.conf)["voice"]
        self._save()
        self.prep.clear()                       # (stop making the old engine's lines)
        self.reprep = list(lines)
        self.q.put(RELOAD)
        print("speech: switching to %s" % mode, file=sys.stderr)

    def step_volume(self, n):
        """Up (n > 0) or down a step of VOLUMES; returns the new volume."""
        i = min(range(len(VOLUMES)), key=lambda k: abs(VOLUMES[k] - self.volume))
        self.set_volume(VOLUMES[max(0, min(len(VOLUMES) - 1, i + n))])
        return self.volume

    def hush(self):
        """Stop talking now: what's playing, the sentence being made, and
        everything queued after it. (The page's hush button, a tap on her
        chest, "stop" while she talks.)"""
        self.gen += 1
        dropped = 0
        while True:
            try:
                t = self.q.get_nowait()
            except queue.Empty:
                break
            if t is None or t is RELOAD:
                self.q.put(t)           # (closing, or switching engines: keep that)
                break
            dropped += 1
        with self.lock:
            self.pending -= dropped
        p = self.player
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass
        self.levels.clear()
        self.quiet_at = time.monotonic()
        print("speech: hushed", file=sys.stderr)

    def say(self, text):
        print("speech: %r" % text, file=sys.stderr)
        for s in sentences(text):
            with self.lock:
                self.pending += 1
            self.q.put(s)

    # ---------- the cache ----------
    def _path(self, text, cache=None):
        return os.path.join(cache or self.cache, hashlib.sha1(text.encode()).hexdigest() + ".raw")

    def cached(self, text):
        try:
            with open(self._path(text), "rb") as f:
                return f.read()
        except OSError:
            return None

    def _keep(self, text, audio, cache=None):
        cache = cache or self.cache
        os.makedirs(cache, exist_ok=True)
        tmp = self._path(text, cache) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(audio)
        os.replace(tmp, self._path(text, cache))

    @property
    def costly(self):
        """Does each new line cost something (characters, not just time)?"""
        return self.cfg["engine"] == "elevenlabs"

    def synth(self, text):
        """16-bit mono audio for one sentence: from the cache, or made (and kept)."""
        audio = self.cached(text)
        if audio is not None:
            return audio
        if self.costly and time.monotonic() < self.eleven_off_until:
            return self._stand_in(text)
        try:
            audio = self.helper.synth(text)
        except OSError as e:
            if not self.costly:
                raise
            # ElevenLabs couldn't: the fallback says it (not kept, so
            # ElevenLabs gets another go at it later)
            print("speech: %s; %s instead" % (e, FALLBACK), file=sys.stderr)
            if " 401 " in str(e) or " 402 " in str(e) or " 429 " in str(e) or "quota" in str(e):
                self.eleven_off_until = time.monotonic() + RETRY_S
            return self._stand_in(text)
        self._keep(text, audio)
        return audio

    def _stand_in(self, text):
        if self.stand_in is None:
            self.stand_in = Helper(dict(self.cfg, engine=FALLBACK, voice=VOICES[FALLBACK]))
        audio = self.stand_in.synth(text)
        if self.stand_in.rate != self.rate:     # (played at ElevenLabs' rate)
            x = np.frombuffer(audio, np.int16).astype(np.float32)
            n = int(len(x) * self.rate / self.stand_in.rate)
            audio = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.int16).tobytes()
        return audio

    def prepare(self, lines):
        """Make these ahead, in the background, so they're instant when said."""
        todo = [s for line in lines for s in sentences(line)]
        todo = [s for s in dict.fromkeys(todo) if not os.path.exists(self._path(s))]
        if todo:
            print("speech: preparing %d sentences in the background" % len(todo), file=sys.stderr)
            self.prep.extend(todo)
            threading.Thread(target=self._prepare, name="speech-prep", daemon=True).start()

    def _prepare(self):
        """A second helper, gentle (two threads, low priority), works through prep."""
        while not self.ok and not self.stopping:
            time.sleep(1)                       # (after the main one's loaded)
        helper, made, t0 = None, 0, time.monotonic()
        cfg = self.cfg                          # (this engine's, even if she switches meanwhile)
        cache = self.cache
        try:
            while self.prep and not self.stopping and self.cfg is cfg:
                s = self.prep.popleft()
                if os.path.exists(self._path(s, cache)):
                    continue
                if helper is None:
                    helper = ElevenLabs(cfg) if cfg["engine"] == "elevenlabs" else Helper(cfg, nice=19, threads=2)
                audio = helper.synth(s)
                if self.cfg is not cfg:
                    break
                self._keep(s, audio, cache)
                made += 1
        except Exception as e:
            print("speech: preparing stopped (%s)" % e, file=sys.stderr)
        finally:
            if helper:
                helper.close()
        print("speech: prepared %d sentences in %.0f min" % (made, (time.monotonic() - t0) / 60), file=sys.stderr)

    # ---------- speaking ----------
    def _run(self):
        while not self.stopping:
            try:
                self.cfg = engine_cfg(self.conf)
                self.eleven_off_until = 0.0
                self.helper = ElevenLabs(self.cfg) if self.costly else Helper(self.cfg)
                self.rate = self.helper.rate
                self.ok, self.error = True, ""
                print("speech: ready (%s %s, %d Hz)" % (self.cfg["engine"], self.cfg["voice"], self.rate),
                      file=sys.stderr)
                if self.reprep:
                    lines, self.reprep = self.reprep, None
                    self.prepare(lines)
                self._speak_forever()
                self.helper.close()             # (switching engines, or closing)
            except Exception as e:      # keep the brain up whatever happens here
                self.ok, self.error = False, str(e)
                print("speech: %s" % e, file=sys.stderr)
                if self.helper:
                    self.helper.close()
                time.sleep(5)

    def _speak_forever(self):
        player = None
        while not self.stopping:
            text = self.q.get()
            if text is None:
                break
            if text is RELOAD:                  # set_engine(): back to _run for the next engine
                if player is not None:
                    player.stdin.close()
                    player.wait()
                return
            self.saying = text
            gen = self.gen
            try:
                audio = self.synth(text)
            except Exception:
                self._done()
                raise
            if gen != self.gen:                 # hushed while it was being made
                self._done()
                player = self.player = None if player is None or player.poll() is not None else player
                continue
            if player is None or player.poll() is not None:
                player = self.player = subprocess.Popen(
                    ["paplay", "--client-name=Voice", "--raw", "--rate=%d" % self.rate, "--channels=1",
                     "--format=s16le", "--latency-msec=100"], stdin=subprocess.PIPE)
                lead = bytes(2 * int(self.rate * LEAD_S))
                player.stdin.write(lead)
                self.quiet_at = max(self.quiet_at, time.monotonic() + 0.1 + LEAD_S)
            # When it'll be heard: after whatever's still playing.
            now = time.monotonic()
            start = max(now + 0.1, self.quiet_at)
            x = np.frombuffer(audio, np.int16).astype(np.float32) / 32768
            n = int(self.rate * BLOCK_S)
            for i in range(0, len(x), n):
                rms = float(np.sqrt((x[i:i + n] ** 2).mean())) if len(x[i:i + n]) else 0.0
                self.levels.append((start + i / self.rate, min(1.0, rms * 5)))
            self.quiet_at = start + len(x) / self.rate
            self._done()
            if self.volume < 1:
                audio = (np.frombuffer(audio, np.int16) * self.volume).astype(np.int16).tobytes()
            try:
                player.stdin.write(audio)
                player.stdin.flush()
                if self.q.empty():
                    # Nothing more to say: let paplay finish and exit, so the
                    # speaker (a Bluetooth one especially) isn't held open.
                    player.stdin.close()
                    player.wait()
                    player = self.player = None
            except (BrokenPipeError, OSError):      # (hushed: paplay killed)
                player = self.player = None

    def _done(self):
        with self.lock:
            self.pending -= 1

    def close(self):
        self.stopping = True
        self.q.put(None)
        for h in (self.helper, self.stand_in):
            if h:
                h.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("words")
    ap.add_argument("--wav", help="write it to this WAV file instead of saying it")
    args = ap.parse_args()
    s = Speech()
    while not s.ok and not s.error:
        time.sleep(0.2)
    if s.error:
        sys.exit(s.error)
    if args.wav:
        import wave
        with wave.open(args.wav, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(s.rate)
            for t in sentences(args.words):
                w.writeframes(s.synth(t))
        return
    s.say(args.words)
    time.sleep(0.5)
    while s.speaking or not s.q.empty():
        time.sleep(0.1)
    s.close()


if __name__ == "__main__":
    main()
