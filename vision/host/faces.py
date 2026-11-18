"""Who's that? Face recognition on top of the YuNet face detector.

SFace (models/face_recognition_sface_2021dec.onnx, from opencv_zoo) turns a
face into 128 numbers, a fingerprint: photos of the same person give similar
fingerprints. Nothing is trained here. Enrolling someone just saves crops of
their face, and loading turns those into fingerprints to compare against.

People live in a folder, one subfolder per person, holding their aligned
112x112 face crops (who.py enroll makes them; any .jpg/.png crops work):

    ~/.local/share/robotpattinson/faces/Josh/0001.jpg ...

Override the folder with --faces or RP_FACES. It holds photos of the
household, so it stays out of the repo.
"""

import glob
import os

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
YUNET = os.path.join(HERE, "models", "face_detection_yunet_2023mar.onnx")
SFACE = os.path.join(HERE, "models", "face_recognition_sface_2021dec.onnx")
FACES_DIR = os.path.expanduser(os.environ.get("RP_FACES", "~/.local/share/robotpattinson/faces"))

# Cosine similarity at or above this = same person (OpenCV's suggested
# threshold for SFace).
MATCH = 0.363
# If the runner-up is this close to the best match, say "not sure" rather
# than guess.
MARGIN = 0.05
# Each person's score is the mean of their best few samples, so one odd
# crop neither makes nor breaks a match.
TOP_K = 3


def detector(w, h, score=0.8):
    return cv2.FaceDetectorYN.create(YUNET, "", (w, h), score_threshold=score)


def unit(v):
    v = v.flatten().astype(np.float32)
    return v / (np.linalg.norm(v) or 1.0)


class Who:
    """The enrolled people, and matching a face against them."""

    def __init__(self, folder=FACES_DIR):
        if not os.path.exists(SFACE):
            raise SystemExit("Missing %s. Run tools/get_models.sh first." % SFACE)
        self.folder = folder
        self.rec = cv2.FaceRecognizerSF.create(SFACE, "")
        self.load()

    def load(self):
        """(Re)read everyone: swapped in whole at the end, so the brain can
        reload while it's recognising."""
        people = {}         # name -> (n, 128) array of unit fingerprints
        for d in sorted(glob.glob(os.path.join(self.folder, "*", ""))):
            name = os.path.basename(os.path.dirname(d))
            feats = []
            for f in sorted(glob.glob(os.path.join(d, "*.jpg")) + glob.glob(os.path.join(d, "*.png"))):
                img = cv2.imread(f)
                if img is not None:
                    feats.append(self.fingerprint(cv2.resize(img, (112, 112))))
            if feats:
                people[name] = np.vstack(feats)
        self.people = people

    def crop(self, frame, face):
        """The aligned 112x112 crop SFace wants (uses YuNet's eye/nose/mouth points)."""
        return self.rec.alignCrop(frame, face)

    def fingerprint(self, crop):
        return unit(self.rec.feature(crop))

    def scores(self, fp):
        """[(score, name)], best first."""
        out = []
        for name, feats in self.people.items():
            sims = np.sort(feats @ fp)[-TOP_K:]
            out.append((float(sims.mean()), name))
        return sorted(out, reverse=True)

    def identify(self, frame, face):
        """(name, score). name is None for a stranger, or when it's too close to call."""
        return self.identify_fp(self.fingerprint(self.crop(frame, face)))

    def identify_fp(self, fp):
        """identify, for a fingerprint already made."""
        if not self.people:
            return None, 0.0
        s = self.scores(fp)
        best, name = s[0]
        runner = s[1][0] if len(s) > 1 else -1.0
        if best < MATCH or best - runner < MARGIN:
            return None, best
        return name, best


class Labeller:
    """Names for the faces in a video. SFace is too slow to run on every frame
    on the Pi, so faces are re-checked every `every` seconds; in between, each
    face keeps the name of the nearest face from the last check."""

    def __init__(self, who, every=1.0):
        self.who = who
        self.every = every
        self.next = 0.0
        self.known = []     # [(cx, cy, (name, score))] from the last check

    def names(self, frame, faces, now):
        """One (name, score) per face, in the same order."""
        if faces is None or not len(faces):
            self.known = []
            return []
        if now >= self.next:
            self.known = [(f[0] + f[2] / 2, f[1] + f[3] / 2, self.who.identify(frame, f)) for f in faces]
            self.next = now + self.every
        out = []
        for f in faces:
            cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
            near = [(np.hypot(cx - kx, cy - ky), lab) for kx, ky, lab in self.known]
            d, lab = min(near, key=lambda n: n[0]) if near else (None, None)
            # Moved more than a face width since the check: probably someone else.
            out.append(lab if d is not None and d < f[2] else (None, 0.0))
        return out


def label_text(name, score):
    return "%s %.2f" % (name, score) if name else "? %.2f" % score
