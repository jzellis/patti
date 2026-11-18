#!/usr/bin/env python3
"""Teach Patti faces, and watch her name them.

    python3 host/who.py enroll Josh --stream 8080   # look at the camera, move your head around
    python3 host/who.py enroll Josh --photos ~/Pictures/josh/
    python3 host/who.py watch --stream 8080         # names over faces, live
    python3 host/who.py list
    python3 host/who.py forget Josh

enroll from the camera saves a crop of your face every --every seconds until
it has --count of them. It skips a crop that's nearly the same as one it
already has, so keep moving: turn your head, tilt it, glasses on and off,
nearer and farther. Only one face may be in view, so it can't enrol the wrong
person. Running enroll again for the same name adds to what's there.

enroll --photos takes image files or folders (phone photos are fine). In a
photo with more than one face it takes the one most like the person's photos
with only them in (and anyone already enrolled under that name), and skips
the photo if none is. Close-ups often score just under the detector's usual
threshold, so a photo with no face found gets a second, looser look
(LOOSE_SCORE); a face found that way has to look like the person too. Afterwards it names any crop that doesn't look like the
rest, to check by eye and delete if it's someone else.

--stream PORT serves the picture at http://<tailscale ip>:PORT/ (the Pi has no
screen); with a display and no --stream it opens a window instead. Ctrl-C or
q quits. Nothing here moves the gimbal.

Faces are kept in ~/.local/share/robotpattinson/faces/<name>/ (see faces.py).
"""

import argparse
import glob
import os
import shutil
import signal
import sys
import time

import cv2
import numpy as np

import faces
from track import Streamer, open_camera, tailscale_ip

CAMERA = "/dev/v4l/by-id/usb-UGREEN_Camera_UGREEN_Camera_SN0001-video-index0"
# Smallest face (pixels wide) worth enrolling: smaller crops are mostly blur.
MIN_FACE = 80
# A new crop this similar to one already saved adds nothing.
SAME = 0.92
# A photo with no face at the detector's usual score gets a second look at this.
LOOSE_SCORE = 0.6
GREEN, RED, AMBER = (0, 255, 0), (0, 0, 255), (0, 190, 255)


def person_dir(folder, name):
    if not name or name != os.path.basename(name) or name.startswith("."):
        raise SystemExit("Bad name: %r" % name)
    return os.path.join(folder, name)


def next_index(d):
    nums = [int(os.path.splitext(f)[0]) for f in os.listdir(d) if os.path.splitext(f)[0].isdigit()]
    return max(nums, default=0) + 1


class View:
    """A window, or an MJPEG stream when there's no screen (or --stream)."""

    def __init__(self, args, title):
        self.title = title
        self.streamer = None
        if args.stream:
            bind = args.stream_bind or tailscale_ip() or "127.0.0.1"
            self.streamer = Streamer(bind, args.stream)
            print("watch at http://%s:%d/" % (bind, args.stream))
        elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            cv2.namedWindow(title, cv2.WINDOW_NORMAL)
        else:
            raise SystemExit("No display here: use --stream PORT to watch in a browser")
        self.stop = []
        signal.signal(signal.SIGINT, lambda *_: self.stop.append(True))

    def show(self, frame):
        """False once it's time to quit."""
        if self.streamer:
            self.streamer.push(frame)
        else:
            cv2.imshow(self.title, frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                return False
        return not self.stop

    def close(self):
        if self.streamer:
            self.streamer.close()
        else:
            cv2.destroyAllWindows()


def caption(frame, text, org, bgr):
    x, y = org
    cv2.putText(frame, text, (x + 1, y + 1), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, bgr, 1, cv2.LINE_AA)


def box(frame, f, bgr):
    x, y, w, h = f[:4].astype(int)
    cv2.rectangle(frame, (x, y), (x + w, y + h), bgr, 2)
    return x, y


def enroll_camera(args, who, d):
    cap = open_camera(args.camera, 640, 480)
    ok, frame = cap.read()
    if not ok:
        raise SystemExit("Camera gave no frame")
    h, w = frame.shape[:2]
    det = faces.detector(w, h)
    have = [who.fingerprint(cv2.resize(cv2.imread(f), (112, 112)))
            for f in glob.glob(os.path.join(d, "*.jpg"))]
    saved, last = 0, 0.0
    view = View(args, "enroll")
    print("Enrolling %s: look at the camera and keep moving your head." % args.name)
    try:
        while saved < args.count:
            ok, frame = cap.read()
            if not ok:
                break
            now = time.monotonic()
            _, fs = det.detect(frame)
            fs = [] if fs is None else list(fs)
            note, colour = "", AMBER
            if len(fs) > 1:
                note = "only one face, please"
                for f in fs:
                    box(frame, f, RED)
            elif fs:
                f = fs[0]
                x, y = box(frame, f, AMBER)
                if f[2] < MIN_FACE:
                    note = "come closer"
                elif now - last >= args.every:
                    crop = who.crop(frame, f)
                    fp = who.fingerprint(crop)
                    if have and max(float(fp @ h_) for h_ in have) > SAME:
                        note = "move a bit (too like the last ones)"
                    else:
                        cv2.imwrite(os.path.join(d, "%04d.jpg" % next_index(d)), crop)
                        have.append(fp)
                        saved += 1
                        last = now
                        colour = GREEN
                        box(frame, f, GREEN)
                        print("  %d/%d" % (saved, args.count))
            else:
                note = "no face"
            caption(frame, "%s  %d/%d  %s" % (args.name, saved, args.count, note), (10, 25), colour)
            if not view.show(frame):
                break
    finally:
        view.close()
        cap.release()
    return saved


def enroll_photos(args, who, d):
    paths = []
    for p in args.photos:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            for ext in ("jpg", "jpeg", "png", "JPG", "JPEG", "PNG"):
                paths += glob.glob(os.path.join(p, "*." + ext))
        else:
            paths.append(p)
    found = []                      # (path, image, faces)
    for p in sorted(set(paths)):
        img = cv2.imread(p)
        if img is None:
            print("  skip %s: can't read it" % p)
            continue
        # Phone photos are huge; YuNet does better at around 1280 wide.
        s = min(1.0, 1280 / max(img.shape[:2]))
        if s < 1.0:
            img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        _, fs = faces.detector(img.shape[1], img.shape[0]).detect(img)
        loose = fs is None or not len(fs)
        if loose:
            _, fs = faces.detector(img.shape[1], img.shape[0], LOOSE_SCORE).detect(img)
        if fs is None or not len(fs):
            print("  skip %s: no face found" % os.path.basename(p))
            continue
        found.append((p, img, fs, loose))
    # What this person looks like: their photos with only them in, and
    # anything already enrolled under the name.
    ref = [who.fingerprint(who.crop(img, fs[0])) for _, img, fs, loose in found if len(fs) == 1 and not loose]
    ref += list(who.people.get(os.path.basename(d), []))
    centre = faces.unit(np.mean(ref, axis=0)) if ref else None
    saved = []                      # (file saved, photo, fingerprint)
    for p, img, fs, loose in found:
        note = ""
        what = "a face found on a second look" if loose else "%d faces" % len(fs)
        if len(fs) == 1 and not loose:
            f = fs[0]
        elif centre is None:
            print("  skip %s: %s, and no photo of just them to check it against" % (os.path.basename(p), what))
            continue
        else:
            sims = [float(who.fingerprint(who.crop(img, f)) @ centre) for f in fs]
            i = int(np.argmax(sims))
            if sims[i] < faces.MATCH:
                print("  skip %s: %s, none like the others" % (os.path.basename(p), what))
                continue
            f, note = fs[i], "  (%s, the one most like the others, %.2f)" % (what, sims[i])
        crop = who.crop(img, f)
        out = os.path.join(d, "%04d.jpg" % next_index(d))
        cv2.imwrite(out, crop)
        saved.append((out, p, who.fingerprint(crop)))
        print("  %s%s" % (os.path.basename(p), note))
    # Odd ones out: probably someone else, or a bad crop.
    if len(saved) > 2:
        mean = faces.unit(np.mean([fp for _, _, fp in saved], axis=0))
        for out, p, fp in saved:
            if float(fp @ mean) < faces.MATCH:
                print("  CHECK %s (from %s): doesn't look like the rest (%.2f)"
                      % (os.path.basename(out), os.path.basename(p), float(fp @ mean)))
    return len(saved)


def watch(args, who):
    if not who.people:
        print("Nobody enrolled yet, so everyone is '?'.")
    else:
        print("Knows: " + ", ".join("%s (%d)" % (n, len(f)) for n, f in who.people.items()))
    cap = open_camera(args.camera, 640, 480)
    ok, frame = cap.read()
    if not ok:
        raise SystemExit("Camera gave no frame")
    h, w = frame.shape[:2]
    det = faces.detector(w, h)
    lab = faces.Labeller(who, args.check_every)
    view = View(args, "who")
    said = None
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            _, fs = det.detect(frame)
            names = lab.names(frame, fs, time.monotonic())
            for f, (name, score) in zip(fs if fs is not None else [], names):
                x, y = box(frame, f, GREEN if name else AMBER)
                caption(frame, faces.label_text(name, score), (x, max(20, y - 8)), GREEN if name else AMBER)
            now_names = sorted(n or "?" for n, _ in names)
            if now_names != said:
                print("sees: " + (", ".join(now_names) or "nobody"))
                said = now_names
            if not view.show(frame):
                break
    finally:
        view.close()
        cap.release()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--faces", default=faces.FACES_DIR, help="where enrolled faces live")
    ap.add_argument("--camera", default=CAMERA)
    ap.add_argument("--stream", type=int, metavar="PORT", help="serve the picture as MJPEG instead of a window")
    ap.add_argument("--stream-bind", metavar="ADDR", help="default: this machine's Tailscale address")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enroll", help="add someone (from the camera, or --photos)")
    e.add_argument("name")
    e.add_argument("--photos", nargs="+", metavar="FILE_OR_DIR")
    e.add_argument("--count", type=int, default=20, help="crops to take from the camera")
    e.add_argument("--every", type=float, default=0.7, help="seconds between camera crops")
    w = sub.add_parser("watch", help="put names on faces, live")
    w.add_argument("--check-every", type=float, default=1.0, help="seconds between recognitions")
    sub.add_parser("list", help="who's enrolled")
    f = sub.add_parser("forget", help="delete someone's crops")
    f.add_argument("name")
    # Let the --stream etc. options go after the subcommand too.
    for p in (e, w):
        p.add_argument("--stream", type=int, metavar="PORT", default=argparse.SUPPRESS)
        p.add_argument("--stream-bind", metavar="ADDR", default=argparse.SUPPRESS)
        p.add_argument("--camera", default=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.cmd == "list":
        names = sorted(n for n in os.listdir(args.faces)) if os.path.isdir(args.faces) else []
        for n in names:
            print("%-20s %d crops" % (n, len(os.listdir(os.path.join(args.faces, n)))))
        if not names:
            print("Nobody yet (%s)" % args.faces)
        return
    if args.cmd == "forget":
        d = person_dir(args.faces, args.name)
        if not os.path.isdir(d):
            raise SystemExit("Don't know %s" % args.name)
        shutil.rmtree(d)
        print("Forgot %s" % args.name)
        return

    who = faces.Who(args.faces)
    if args.cmd == "watch":
        watch(args, who)
        return

    d = person_dir(args.faces, args.name)
    os.makedirs(d, exist_ok=True)
    n = enroll_photos(args, who, d) if args.photos else enroll_camera(args, who, d)
    total = len(os.listdir(d))
    if not total:
        os.rmdir(d)
    print("Saved %d new crops of %s (%d in all)." % (n, args.name, total))


if __name__ == "__main__":
    sys.exit(main())
