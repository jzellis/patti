"""People she's seen but doesn't know: remembered without a name, with a
snapshot, until someone tells her who they are. For the brain (server.py).

    s = strangers.Strangers(who)         # who: faces.Who
    sid = s.saw(frame, face, fp)         # a face she didn't know: "stranger-20261102-1404", or None
    s.name(sid, "John")                  # their crops go to faces/John (she knows them now)
    s.forget(sid)

Each lives in STRANGERS_DIR/<id>/: up to KEEP aligned face crops (0001.jpg...,
as faces/ holds, so naming them is just moving them over) and snap.jpg, a
wider picture from the first good look. A new one is only made once the same
unknown face has been seen CONFIRM times within CONFIRM_S (one odd frame
won't do it); after that, a face like one of theirs (faces.MATCH) is them
again. The brain logs their visits in memory.py under the id, so "someone
new was here at 3pm" is remembered too.

Unnamed ones go after EXPIRE_S unseen, and there are never more than MAX: the
oldest go first. Not made while she's away from home (the brain's choice).
"""
import glob
import os
import shutil
import sys
import threading
import time

import cv2
import numpy as np

import faces

STRANGERS_DIR = os.path.expanduser("~/.local/share/robotpattinson/strangers")
KEEP = 5
SAME = 0.92             # a crop this like one already kept isn't worth keeping (as meet.py)
CONFIRM = 2
CONFIRM_S = 10.0
MIN_PX = 60             # a face narrower than this is too far away to keep
SNAP_PAD = 1.0          # the snapshot: the face plus this much of its size each side
EXPIRE_S = 30 * 86400
MAX = 50


class Strangers:
    def __init__(self, who):
        self.who = who
        self.lock = threading.Lock()
        self.people = {}        # id -> (n, 128) fingerprints
        self.pending = None     # (fp, crop, snap, at, count): a face not yet confirmed
        os.makedirs(STRANGERS_DIR, exist_ok=True)
        self.tidy()
        self.load()

    def load(self):
        people = {}
        for d in sorted(glob.glob(os.path.join(STRANGERS_DIR, "*", ""))):
            feats = [self.who.fingerprint(cv2.resize(img, (112, 112)))
                     for img in (cv2.imread(f) for f in sorted(glob.glob(os.path.join(d, "0*.jpg"))))
                     if img is not None]
            if feats:
                people[os.path.basename(os.path.dirname(d))] = np.vstack(feats)
        self.people = people

    def ids(self):
        """Newest first."""
        return sorted(self.people, reverse=True)

    def match(self, fp):
        best, sid = 0.0, None
        for k, feats in self.people.items():
            s = float(np.max(feats @ fp))
            if s > best:
                best, sid = s, k
        return sid if best >= faces.MATCH else None

    def saw(self, frame, face, fp):
        """A face she doesn't know (blocking: run it in a thread). Their id,
        or None while it's not yet sure they're anyone."""
        if face[2] < MIN_PX:
            return None
        with self.lock:
            sid = self.match(fp)
            if sid:
                self._add(sid, self.who.crop(frame, face), fp)
                os.utime(os.path.join(STRANGERS_DIR, sid))     # (seen: not expiring yet)
                return sid
            now = time.monotonic()
            p = self.pending
            if p and now - p[3] < CONFIRM_S and float(p[0] @ fp) >= faces.MATCH:
                self.pending = p = (p[0], p[1], p[2], now, p[4] + 1)
            else:
                self.pending = p = (fp, self.who.crop(frame, face), snapshot(frame, face), now, 1)
            if p[4] < CONFIRM:
                return None
            self.pending = None
            sid = time.strftime("stranger-%Y%m%d-%H%M%S")
            d = os.path.join(STRANGERS_DIR, sid)
            os.makedirs(d, exist_ok=True)
            cv2.imwrite(os.path.join(d, "snap.jpg"), p[2])
            self.people[sid] = np.empty((0, len(fp)), np.float32)
            self._add(sid, p[1], p[0])
            print("strangers: someone new, %s" % sid, file=sys.stderr)
            self._trim()
            return sid

    def _add(self, sid, crop, fp):
        feats = self.people[sid]
        if len(feats) >= KEEP or (len(feats) and float(np.max(feats @ fp)) > SAME):
            return
        cv2.imwrite(os.path.join(STRANGERS_DIR, sid, "%04d.jpg" % (len(feats) + 1)), crop)
        self.people[sid] = np.vstack([feats, fp[None]])

    def name(self, sid, name):
        """Tell her who they are: their crops join faces/<name> (after any
        already there). True if done."""
        with self.lock:
            d = os.path.join(STRANGERS_DIR, sid)
            if sid not in self.people or not os.path.isdir(d):
                return False
            to = os.path.join(faces.FACES_DIR, name)
            os.makedirs(to, exist_ok=True)
            n = max([int(os.path.splitext(f)[0]) for f in os.listdir(to) if os.path.splitext(f)[0].isdigit()],
                    default=0)
            for f in sorted(glob.glob(os.path.join(d, "0*.jpg"))):
                n += 1
                shutil.move(f, os.path.join(to, "%04d.jpg" % n))
            if os.path.exists(os.path.join(d, "snap.jpg")):
                os.makedirs(os.path.join(STRANGERS_DIR, "named"), exist_ok=True)
                shutil.move(os.path.join(d, "snap.jpg"), os.path.join(STRANGERS_DIR, "named", name + ".jpg"))
            shutil.rmtree(d, ignore_errors=True)
            self.people.pop(sid, None)
        print("strangers: %s is %s" % (sid, name), file=sys.stderr)
        return True

    def forget(self, sid):
        with self.lock:
            if not sid.startswith("stranger-"):
                return False
            shutil.rmtree(os.path.join(STRANGERS_DIR, sid), ignore_errors=True)
            return self.people.pop(sid, None) is not None

    def snap(self, sid):
        """Path of their snapshot, or None."""
        p = os.path.join(STRANGERS_DIR, sid, "snap.jpg")
        return p if sid.startswith("stranger-") and os.path.exists(p) else None

    def tidy(self):
        """Unnamed and unseen for EXPIRE_S: gone."""
        for d in glob.glob(os.path.join(STRANGERS_DIR, "stranger-*", "")):
            if time.time() - os.path.getmtime(d) > EXPIRE_S:
                shutil.rmtree(d, ignore_errors=True)

    def _trim(self):
        for sid in sorted(self.people)[:-MAX]:
            shutil.rmtree(os.path.join(STRANGERS_DIR, sid), ignore_errors=True)
            self.people.pop(sid, None)


def snapshot(frame, face):
    """The face and some of what's around it."""
    x, y, w, h = (int(v) for v in face[:4])
    H, W = frame.shape[:2]
    px, py = int(w * SNAP_PAD), int(h * SNAP_PAD)
    return frame[max(0, y - py):min(H, y + h + py), max(0, x - px):min(W, x + w + px)].copy()
