#!/usr/bin/env python3
"""Voice notes, into Joplin: "Patti, take a note".

She records from the mic voice.py is already reading (its tap, so Vosk isn't
listening for commands meanwhile) until you stop talking, transcribes it, and
chest.py shows it for a few seconds before it's saved (or discarded). Each
note goes into the "Voice Notes" notebook with its audio attached, and Joplin
syncs it to Joplin Cloud. A copy of each (text and audio) is kept in NOTES_DIR
whatever happens, so a failed save or a misheard note isn't lost; every
SYNC_S, any that didn't get into Joplin go in, and it syncs.

Transcribing is either of:

    whisper        whisper.cpp on the Pi (base.en): offline and private, but
                   slow on a Pi 4: about 1.7 s for each second of talking
    whisper tiny   the same with tiny.en: about twice as quick, a bit less
                   accurate
    scribe         ElevenLabs' Scribe, on her ElevenLabs key: about a second,
                   and accurate, but it's their server that hears it

Names and words it should know ("Patti", not "patty") go in WORDS,
one a line (or several, comma-separated); whisper is given them as its
prompt, which steers it to those spellings. Read afresh for every note.

Needs:
    the Joplin CLI (npm install -g joplin, with npm's prefix ~/.local), logged
        in to Joplin Cloud by hand: joplin config sync.target 10, then
        joplin config sync.10.username / sync.10.password, then joplin sync
    whisper.cpp built in ~/src/whisper.cpp, with its base.en and tiny.en
        models in ~/.local/share/robotpattinson/whisper/
    ffmpeg, for the audio (Opus, small)

    python3 notes.py test.wav [whisper|scribe]    # transcribe a file
    python3 notes.py --save "words"               # save a note (no audio)
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave

import numpy as np

RATE = 16000                    # voice.py's
JOPLIN = os.path.expanduser("~/.local/bin/joplin")
NOTEBOOK = "Voice Notes"
WHISPER = os.path.expanduser("~/src/whisper.cpp/build/bin/whisper-cli")
WHISPER_MODELS = os.path.expanduser("~/.local/share/robotpattinson/whisper/ggml-%s.bin")
ELEVEN_KEY = os.path.expanduser("~/.config/robotpattinson/elevenlabs.json")
SCRIBE_MODEL = "scribe_v1"
NOTES_DIR = os.path.expanduser("~/.local/share/robotpattinson/notes")
WORDS = os.path.expanduser("~/.config/robotpattinson/note-words.txt")
DEFAULT_WORDS = ["Patti", "Joplin"]
ENGINES = ["whisper", "whisper tiny", "scribe"]
SYNC_S = 30 * 60                # sync this often (and put in any notes that missed Joplin)

# when to stop recording
SPEECH_DB = 8                   # louder than the room's background by this much: talking
START_WAIT_S = 8.0              # nothing said for this long: give up
END_QUIET_S = 2.5               # quiet this long after talking: done
MAX_S = 120.0


class Recording:
    """Fed 0.1 s chunks from voice.py's thread (as its tap); done when the
    talking's over, or end() is called."""

    def __init__(self, floor_db):
        self.floor_db = floor_db
        self.chunks = []
        self.started = time.monotonic()
        self.talked_at = None       # the last chunk that sounded like talking
        self.level = 0.0            # 0-1, for the screen
        self.done = threading.Event()

    def feed(self, data):
        if self.done.is_set():
            return
        self.chunks.append(data)
        x = np.frombuffer(data, np.int16).astype(np.float32)
        db = 20 * np.log10(np.sqrt(float((x * x).mean())) / 32768 + 1e-9)
        self.level = max(0.0, min(1.0, (db - self.floor_db - 3) / 30))
        now = time.monotonic()
        if db > self.floor_db + SPEECH_DB:
            self.talked_at = now
        if (self.talked_at is None and now - self.started > START_WAIT_S) or \
                (self.talked_at is not None and now - self.talked_at > END_QUIET_S) or \
                now - self.started > MAX_S:
            self.done.set()

    def end(self):
        self.done.set()

    @property
    def heard(self):
        return self.talked_at is not None

    @property
    def seconds(self):
        return len(self.chunks) / 10

    def wav(self, path):
        audio = b"".join(self.chunks)
        if self.talked_at is not None:          # drop the quiet at the end, but for half a second
            keep = int((self.talked_at - self.started + 0.5) * RATE) * 2
            audio = audio[:max(keep, RATE * 2)]
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(audio)


# ---------- transcribing ----------
def transcribe(wav, engine="whisper"):
    """The words in a 16 kHz mono WAV ("" if none). Raises OSError if it can't."""
    if engine == "scribe":
        return scribe(wav)
    return whisper(wav, "tiny.en" if engine == "whisper tiny" else "base.en")


def whisper(wav, model="base.en"):
    path = WHISPER_MODELS % model
    if not os.path.exists(WHISPER) or not os.path.exists(path):
        raise OSError("whisper.cpp or its %s model isn't installed" % model)
    r = subprocess.run([WHISPER, "-m", path, "-f", wav, "-l", "en", "-t", "4", "-nt", "-np",
                        "--prompt", prompt()],
                       capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise OSError("whisper: " + (r.stderr.strip().splitlines() or ["failed"])[-1])
    return tidy(" ".join(r.stdout.split()))


def words():
    """WORDS, made with the defaults if it isn't there yet."""
    try:
        with open(WORDS) as f:
            text = f.read()
    except FileNotFoundError:
        try:
            os.makedirs(os.path.dirname(WORDS), exist_ok=True)
            with open(WORDS, "w") as f:
                f.write("# Names and words for her to spell right in notes (notes.py), one a line\n"
                        + "\n".join(DEFAULT_WORDS) + "\n")
        except OSError:
            pass
        return list(DEFAULT_WORDS)
    except OSError:
        return list(DEFAULT_WORDS)
    out = []
    for line in text.splitlines():
        if not line.strip().startswith("#"):
            out += [w.strip() for w in line.split(",") if w.strip()]
    return out


def prompt():
    """Whisper's prompt: the words, as if they'd just been said."""
    return "A note for Patti. Names: " + ", ".join(words()) + "."


def scribe(wav):
    with open(ELEVEN_KEY) as f:
        key = json.load(f)["api_key"]
    boundary = uuid.uuid4().hex
    with open(wav, "rb") as f:
        audio = f.read()
    parts = []
    for name, value in (("model_id", SCRIBE_MODEL), ("language_code", "en"), ("tag_audio_events", "false")):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (boundary, name, value)).encode())
    parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="note.wav"\r\n'
                  'Content-Type: audio/wav\r\n\r\n' % boundary).encode() + audio + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    req = urllib.request.Request("https://api.elevenlabs.io/v1/speech-to-text", data=b"".join(parts),
                                 headers={"xi-api-key": key,
                                          "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return tidy(json.load(r).get("text", ""))
    except urllib.error.HTTPError as e:
        raise OSError("scribe: HTTP %d" % e.code)
    except (urllib.error.URLError, TimeoutError) as e:
        raise OSError("scribe: %s" % getattr(e, "reason", e))


def tidy(text):
    """Whisper's "[BLANK_AUDIO]", "(wind blowing)" and the like out."""
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)
    return " ".join(text.split())


# ---------- saving ----------
def title_for(text, when):
    words = re.sub(r"[^\w' -]", "", text).split()
    first = " ".join(words[:6]) + ("..." if len(words) > 6 else "")
    return "%s %s" % (when.strftime("%Y-%m-%d %H%M"), first or "note")


def keep(text, wav=None):
    """Save it in NOTES_DIR (always, before Joplin): (folder, title, audio file or None)."""
    when = datetime.datetime.now()
    title = title_for(text, when)
    folder = os.path.join(NOTES_DIR, when.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(folder, exist_ok=True)
    audio = None
    if wav:
        audio = os.path.join(folder, "audio.ogg")
        r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", wav, "-c:a", "libopus", "-b:a", "24k", audio],
                           capture_output=True, timeout=60)
        if r.returncode:
            audio = os.path.join(folder, "audio.wav")
            os.replace(wav, audio)
    body = text + "\n\n*Dictated to Patti, %s*\n" % when.strftime("%A %-d %B %Y, %H:%M")
    with open(os.path.join(folder, "note.json"), "w") as f:
        json.dump({"title": title, "body": body, "audio": audio, "saved": False}, f)
    return folder


def joplin(*args, timeout=120):
    r = subprocess.run([JOPLIN] + list(args), capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise OSError("joplin %s: %s" % (args[0], (r.stderr.strip() or r.stdout.strip() or "failed").splitlines()[-1]))
    return r.stdout


# one Joplin at a time (a sync and an import together trip over its locks,
# and catch_up() mustn't import a note save() is importing)
_lock = threading.Lock()


def to_joplin(folder):
    """The note kept in folder, into Joplin (imported as Markdown, so the audio
    comes in as an attachment), once. Raises OSError if Joplin can't."""
    with _lock:
        _to_joplin(folder)


def _to_joplin(folder):
    if not os.path.exists(JOPLIN):
        raise OSError("the Joplin CLI isn't installed")
    with open(os.path.join(folder, "note.json")) as f:
        note = json.load(f)
    if note["saved"]:
        return
    with tempfile.TemporaryDirectory() as tmp:
        md = os.path.join(tmp, note["title"] + ".md")
        body = note["body"]
        if note["audio"]:
            name = os.path.basename(note["audio"])
            os.symlink(note["audio"], os.path.join(tmp, name))
            body += "\n[Audio](%s)\n" % name
        with open(md, "w") as f:
            f.write(body)
        if NOTEBOOK not in joplin("ls", "/").split("\n"):
            joplin("mkbook", NOTEBOOK)
        joplin("import", md, NOTEBOOK, "--format", "md")
    note["saved"] = True
    with open(os.path.join(folder, "note.json"), "w") as f:
        json.dump(note, f)


def sync():
    """Joplin, synced. Raises OSError if it fails."""
    with _lock:
        joplin("sync", timeout=600)


def catch_up():
    """Any kept notes that didn't make it into Joplin, in; then a sync (every
    SYNC_S, from chest.py). Returns how many went in."""
    n = 0
    try:
        folders = sorted(os.listdir(NOTES_DIR))
    except OSError:
        folders = []
    try:
        for name in folders:
            path = os.path.join(NOTES_DIR, name, "note.json")
            try:
                with open(path) as f:
                    if json.load(f).get("saved"):
                        continue
            except (OSError, ValueError):
                continue
            to_joplin(os.path.dirname(path))
            n += 1
            print("notes: caught up %s" % name, file=sys.stderr)
        sync()
    except (OSError, subprocess.SubprocessError) as e:
        print("notes: catching up: %s" % e, file=sys.stderr)
    return n


def save(text, wav=None):
    """Keep it, put it in Joplin and sync: returns "" or what went wrong (the
    note's kept in NOTES_DIR either way)."""
    folder = keep(text, wav)
    try:
        to_joplin(folder)
    except (OSError, subprocess.SubprocessError) as e:
        print("notes: %s (kept in %s)" % (e, folder), file=sys.stderr)
        return str(e)
    threading.Thread(target=_sync_quietly, daemon=True).start()
    print("notes: saved %s" % folder, file=sys.stderr)
    return ""


def _sync_quietly():
    try:
        sync()
    except (OSError, subprocess.SubprocessError) as e:
        print("notes: sync: %s" % e, file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--save"]:
        print(save(" ".join(sys.argv[2:])) or "saved")
    else:
        t = time.monotonic()
        print(transcribe(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "whisper"))
        print("(%.1f s)" % (time.monotonic() - t))
