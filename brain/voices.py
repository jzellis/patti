#!/usr/bin/env python3
"""Who's talking: speaker identification from her mic, the voice side of
vision/host/faces.py. For knowing who said something to her, never as proof
of who someone is (a recording would fool it).

    python3 voices.py enroll Josh       # read SCRIPT aloud, then keep chatting
    python3 voices.py test              # says who it hears, every CLIP_S
    python3 voices.py list
    python3 voices.py forget Josh

A speaker model (NVIDIA's TitaNet small; run by sherpa-onnx) turns a few
seconds of speech into 192 numbers, a voiceprint: the same voice gives similar
ones. (CAM++ and WeSpeaker ResNet34 from the same download page scored
different people, and plain noise, as alike as one person here: 0.7-0.9.
TitaNet: the same person 0.7-0.8, different people 0.06-0.32, noise 0.07.)
Enrolling records from her own mic (the room and the mic matter as much as
the voice), cuts it into CLIP_S clips, about as long as a command to her,
keeps the ones with speech in them, and saves them. Like faces, nothing is
trained: a voice is matched against everyone's saved clips.

Clips live in ~/.local/share/robotpattinson/voices/<name>/ (RP_VOICES to
override). They're recordings of the household, so they stay out of the repo.

Setup on the Pi (done 2026-11-01):
    python3 -m pip install --user --break-system-packages sherpa-onnx
    curl -L -o ~/.local/share/robotpattinson/models/nemo_en_titanet_small.onnx \\
      https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import time
import wave

import numpy as np

RATE = 16000
MIC = "rpmic"
MODEL = os.path.expanduser("~/.local/share/robotpattinson/models/nemo_en_titanet_small.onnx")
VOICES_DIR = os.path.expanduser(os.environ.get("RP_VOICES", "~/.local/share/robotpattinson/voices"))
CLIP_S = 3.0
ENROLL_S = 75
# A clip counts as speech if this much of it is louder than the quietest
# tenth of the recording by SPEECH_DB (the room's own noise).
SPEECH_FRAC = 0.5
SPEECH_DB = 12.0
ROOM_DB = -45.0     # the room's noise to assume before hearing a quiet moment
# Cosine similarity at or above this = the same person. Measured 2026-11-01
# against Josh's clips: his own speech cut to 0.8 / 1 / 1.5 s scored at least
# 0.43 / 0.52 / 0.63 nine times in ten, strangers (sr-data, and a video of
# Adam Savage held to her mic) never above 0.24. To check again once more
# than one voice is enrolled.
MATCH = 0.35
MARGIN = 0.05       # runner-up this close: not sure
TOP_K = 5           # a person's score: the mean of their best few clips

SCRIPT = """\
On a grey Sunday morning in Bristol, I wandered down to the café for a
coffee and a slice of toast. The rain had just cleared, and the pavements
were shining like wet slate. A small dog in a bright yellow raincoat barked
at a pigeon, which ignored it completely.

Back home, my workbench is covered in wires, screws, solder and half-printed
parts. Somewhere under all of that there's a robot who likes to watch the
world go by and say hello. Tomorrow I'd like to sort out the power supply,
tidy the cables, and maybe finally find the box where I put all my USB
sticks. Then I'll sit back with a cup of tea, put a record on, and enjoy a
quiet evening with a good book.

Now keep talking for a bit, about anything at all: what you had for lunch,
the weather, what's on telly. Normal voice, the way you'd talk to her."""


def unit(v):
    v = np.asarray(v, np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def frames_db(x, n=400):
    """Loudness of each 25 ms frame, dB (x is float -1..1)."""
    k = len(x) // n
    f = x[:k * n].reshape(k, n)
    return 10 * np.log10((f ** 2).mean(1) + 1e-10)


def speechy(x, floor_db):
    return (frames_db(x) > floor_db + SPEECH_DB).mean() >= SPEECH_FRAC


def read_wav(path):
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def write_wav(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def record(seconds, mic=MIC):
    """seconds of her mic, float -1..1."""
    raw = subprocess.run(["arecord", "-q", "-D", mic, "-f", "S16_LE", "-r", str(RATE), "-c", "1",
                          "-t", "raw", "-d", str(int(seconds))], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


class Voices:
    """The enrolled voices, and matching a clip against them."""

    def __init__(self, folder=VOICES_DIR, threads=2):
        import sherpa_onnx
        if not os.path.exists(MODEL):
            raise SystemExit("Missing %s (see the top of voices.py)." % MODEL)
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=threads)
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.folder = folder
        self.load()

    def load(self):
        """(Re)read everyone: swapped in whole at the end, so the brain can
        reload while it's listening."""
        people = {}         # name -> (n, 192) array of unit voiceprints
        for d in sorted(glob.glob(os.path.join(self.folder, "*", ""))):
            name = os.path.basename(os.path.dirname(d))
            prints = [self.voiceprint(read_wav(f)) for f in sorted(glob.glob(os.path.join(d, "*.wav")))]
            if prints:
                people[name] = np.vstack(prints)
        self.people = people

    def voiceprint(self, x):
        s = self.ex.create_stream()
        s.accept_waveform(RATE, np.asarray(x, np.float32))
        s.input_finished()
        return unit(self.ex.compute(s))

    def scores(self, vp):
        """[(score, name)], best first."""
        out = []
        for name, prints in self.people.items():
            out.append((float(np.sort(prints @ vp)[-TOP_K:].mean()), name))
        return sorted(out, reverse=True)

    def identify(self, x):
        """(name, score) for speech x; name is None for a stranger, or too close to call."""
        if not self.people:
            return None, 0.0
        s = self.scores(self.voiceprint(x))
        best, name = s[0]
        runner = s[1][0] if len(s) > 1 else -1.0
        if best < MATCH or best - runner < MARGIN:
            return None, best
        return name, best


def enroll(args):
    print(__doc__.split("\n\n")[0])
    print("\nRead this aloud, in your normal voice, from where you'd usually talk to her:\n")
    print(SCRIPT)
    print("\nRecording for %d seconds, starting now..." % args.seconds, flush=True)
    x = record(args.seconds, args.mic)
    floor = np.percentile(frames_db(x), 10)
    n = int(CLIP_S * RATE)
    clips = [x[i:i + n] for i in range(0, len(x) - n + 1, n)]
    keep = [c for c in clips if speechy(c, floor)]
    d = os.path.join(args.voices, args.name)
    os.makedirs(d, exist_ok=True)
    first = len(glob.glob(os.path.join(d, "*.wav")))
    for i, c in enumerate(keep):
        write_wav(os.path.join(d, "%04d.wav" % (first + i + 1)), c)
    print("Kept %d of %d clips with speech in them (room noise %.0f dB); %d for %s in all."
          % (len(keep), len(clips), floor, first + len(keep), args.name))
    if len(keep) < 8:
        print("That's not many: was the mic picking you up? Try again, a bit closer or louder.")
    # How alike the new clips are (same voice: well above MATCH).
    v = Voices(args.voices)
    if len(v.people.get(args.name, [])) > 1:
        p = v.people[args.name]
        sims = (p @ p.T)[np.triu_indices(len(p), 1)]
        print("Clips of %s match each other at %.2f on average (%.2f at worst)." % (args.name, sims.mean(), sims.min()))


def test(args):
    v = Voices(args.voices)
    print("Knows: " + (", ".join("%s (%d)" % (n, len(p)) for n, p in v.people.items()) or "nobody"))
    print("Listening; talk to her. Ctrl-C to stop.", flush=True)
    end = time.time() + args.seconds
    floor = ROOM_DB
    try:
        while time.time() < end:
            x = record(CLIP_S, args.mic)
            # The room's noise: the quietest moments heard, drifting up slowly
            # (starting from a guess, since a clip full of talk has no quiet).
            floor = min(floor + 0.1, np.percentile(frames_db(x), 10))
            if not speechy(x, floor):
                print("(not talk: %.0f dB, room %.0f dB)" % (np.percentile(frames_db(x), 90), floor), flush=True)
                continue
            name, best = v.identify(x)
            s = v.scores(v.voiceprint(x))
            print("%-9s %s  (%.0f dB)" % (name or "?", ", ".join("%s %.2f" % (n, sc) for sc, n in s),
                                          np.percentile(frames_db(x), 90)), flush=True)
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--voices", default=VOICES_DIR, help="where enrolled voices live")
    ap.add_argument("--mic", default=MIC, help="ALSA mic")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enroll")
    e.add_argument("name")
    e.add_argument("--seconds", type=int, default=ENROLL_S)
    t = sub.add_parser("test")
    t.add_argument("--seconds", type=int, default=60)
    sub.add_parser("list")
    f = sub.add_parser("forget")
    f.add_argument("name")
    args = ap.parse_args()
    if args.cmd == "enroll":
        enroll(args)
    elif args.cmd == "test":
        test(args)
    elif args.cmd == "list":
        names = sorted(os.listdir(args.voices)) if os.path.isdir(args.voices) else []
        for n in names:
            print("%-20s %d clips" % (n, len(glob.glob(os.path.join(args.voices, n, "*.wav")))))
        if not names:
            print("Nobody yet (%s)" % args.voices)
    elif args.cmd == "forget":
        d = os.path.join(args.voices, args.name)
        if not os.path.isdir(d):
            sys.exit("No %s." % args.name)
        shutil.rmtree(d)
        print("Forgot %s" % args.name)


if __name__ == "__main__":
    main()
