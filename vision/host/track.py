#!/usr/bin/env python3
"""Face tracking test: keep the biggest face in the middle of the frame.

    host/.venv/bin/python host/track.py              # defaults: /dev/video4, /dev/ttyACM0
    host/.venv/bin/python host/track.py --dry-run    # camera + detection only, gimbal untouched

    host/.venv/bin/python host/track.py --record ~/Videos/track.mp4
    python3 host/track.py --stream 8080          # on the Pi: watch in a browser

--stream PORT serves the overlaid video as MJPEG at http://<host>:PORT/ instead
of opening a window (the Pi has no screen). It binds to this machine's
Tailscale address by default, so it's reachable from your other devices but
not from the LAN or the internet; --stream-bind 0.0.0.0 opens it wider. There
is no password yet, so don't widen it on a network you don't trust. Without a
window there are no keys: Ctrl-C quits (recording still saves cleanly).

In the preview window:  q quit   c centre   space pause/resume tracking   f font

--record saves what the preview shows (boxes and all; --record-clean for the
plain camera picture) plus sound, by piping frames into ffmpeg, which records
the microphone itself. The mic defaults to the gimbal camera's own (UGREEN),
falling back to the system default; --audio-source picks another (names from
`pactl list sources short`), --no-audio records silent video.

Detection is OpenCV's YuNet (models/face_detection_yunet_2023mar.onnx).

Control is continuous: every frame updates where the gimbal is heading, and
the Pico eases toward it (accelerating and braking, see pico/keyctl.py Axis).

The catch with correcting every frame is that the camera rides on the gimbal
and lags, so a frame grabbed mid-move shows the face relative to where the
camera *was*, not where it is. Adding that error to the current aim stacks up
corrections and overshoots. So we work in gimbal angles, not image offsets:
we ask the Pico where the servos actually are ("P" -> "AT pan tilt") every
frame, look up where they were when the frame was taken (now - --latency),
and add the face's offset in that frame. That gives where the face is in the
world, independent of how the camera was moving. That estimate is smoothed
(--smooth) to stop detection jitter turning into servo twitch, and becomes
the new target.

Deadband with hysteresis: the gimbal rests while the face is within
--deadband degrees of where it's aimed, then follows continuously until it
has the face centred again.

Pixel error is turned into degrees using the camera's field of view (--hfov).
The default is a guess; if it overshoots, lower --hfov, and if it
undershoots, raise it. If it wobbles while you move, adjust --latency.
"""

import argparse
import collections
import http.server
import os
import re
import signal
import socketserver
import subprocess
import threading
import time

import cv2
import numpy as np

import faces
import picolink

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "models", "face_detection_yunet_2023mar.onnx")

# Must match pico: gimbal.PAN_LIMITS / TILT_LIMITS.
PAN_LIM = (-90.0, 90.0)
TILT_LIM = (-19.0, 40.0)
# Once following, keep going until the face is this close to centre (degrees).
SETTLE_DEG = 0.75
# A face missing longer than this is a new sighting: don't smooth from the old one.
LOST_S = 0.5


def clamp(v, lim):
    return max(lim[0], min(lim[1], v))


def pose_at(history, t):
    """Gimbal (pan, tilt) at time t, interpolated from (time, pan, tilt)
    samples. Before the first sample or after the last, the nearest one."""
    history = list(history)  # a deque: can't be sliced
    if t <= history[0][0]:
        return history[0][1:]
    for (t0, p0, q0), (t1, p1, q1) in zip(history, history[1:]):
        if t0 <= t <= t1:
            f = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
            return p0 + f * (p1 - p0), q0 + f * (q1 - q0)
    return history[-1][1:]


# OSD fonts, cycled with 'f'. OpenCV's own Hershey fonts always work; the
# rest are installed fonts looked up by family name (via fc-list) and drawn
# with Pillow, and are just skipped if they or Pillow aren't there.
HERSHEY = [("Hershey Simplex", cv2.FONT_HERSHEY_SIMPLEX),
           ("Hershey Triplex", cv2.FONT_HERSHEY_TRIPLEX),
           ("Hershey Script", cv2.FONT_HERSHEY_SCRIPT_COMPLEX)]
# (Danger Zone Warning has no letters and Teenage Sunkies mangles digits, so
# they're left out.)
TTF_FAMILIES = ["Silkscreen", "Super Retro M54", "Bebas Neue", "Plane Crash",
                "Arcane Nine", "Heavitas", "Market Deco", "MesloLGS NF",
                "DejaVu Sans Mono"]


def font_file(family):
    try:
        out = subprocess.run(["fc-list", ":family=" + family, "--format", "%{file}\n"],
                             capture_output=True, text=True, timeout=3).stdout.split("\n")
    except (OSError, subprocess.SubprocessError):
        return None
    files = sorted(f for f in out if f)
    # Prefer a bold face, then any.
    return next((f for f in files if "bold" in f.lower()), files[0] if files else None)


class Osd:
    """Draws the status line in the current font; next() switches font."""

    # Pixel fonts (Silkscreen is drawn on an 8px grid) only stay crisp at whole
    # multiples of their grid, so the size is a multiple of 8.
    def __init__(self, start=None, size=16):
        self.fonts = list(HERSHEY)
        try:
            from PIL import Image, ImageDraw, ImageFont
            self._pil = (Image, ImageDraw)
            self._truetype = ImageFont.truetype
            for fam in TTF_FAMILIES:
                f = font_file(fam)
                if f:
                    try:
                        self.fonts.append((fam, ImageFont.truetype(f, size)))
                    except OSError:
                        pass
        except ImportError:
            self._pil = None
        self._cache = {}
        self.size = size
        self.i = 0
        if start:
            names = [n.lower() for n, _ in self.fonts]
            matches = [i for i, n in enumerate(names) if start.lower() in n]
            if matches:
                self.i = matches[0]
            else:
                # Not in the list: any installed family, by its exact name.
                f = font_file(start) if self._pil else None
                if not f:
                    raise SystemExit("No font %r (fc-list : family shows what's installed). "
                                     "Built in: %s" % (start, ", ".join(n for n, _ in self.fonts)))
                self.fonts.insert(0, (start, self._truetype(f, size)))

    @property
    def name(self):
        return self.fonts[self.i][0]

    @property
    def line_height(self):
        """Pixels from one line to the next, with a little leading."""
        font = self.fonts[self.i][1]
        if isinstance(font, int):
            return 18
        ascent, descent = font.getmetrics()
        return ascent + descent + max(1, self.size // 8)

    def next(self):
        self.i = (self.i + 1) % len(self.fonts)
        return self.name

    def text(self, frame, text, org, bgr):
        self.lines(frame, [(text, org)], bgr)

    def lines(self, frame, items, bgr):
        """Draw several (text, (x, y)) at once: one Pillow round trip, not one each."""
        font = self.fonts[self.i][1]
        if isinstance(font, int):
            for text, (x, y) in items:
                cv2.putText(frame, text, (x, y + 14), font, 0.6, bgr, 2, cv2.LINE_8)
            return
        fh, fw = frame.shape[:2]
        for text, (x, y) in items:
            mask = self._mask(font, text)
            h, w = mask.shape
            h, w = min(h, fh - y), min(w, fw - x)  # clip at the frame edge
            if h > 0 and w > 0:
                frame[y:y + h, x:x + w][mask[:h, :w]] = bgr

    def _mask(self, font, text):
        """Text rendered once to a 1-bit mask and cached: Pillow re-rendering
        every line on every frame costs ~40ms, most of a frame. Command output
        only changes every few seconds, so nearly everything is a cache hit."""
        key = (id(font), text)
        mask = self._cache.get(key)
        if mask is None:
            Image, ImageDraw = self._pil
            l, t, r, b = font.getbbox(text or " ")
            img = Image.new("1", (max(1, r), max(1, b)))
            draw = ImageDraw.Draw(img)
            draw.fontmode = "1"  # no anti-aliasing: hard pixel edges
            draw.text((0, 0), text, font=font, fill=1)
            mask = np.array(img, dtype=bool)
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[key] = mask
        return mask


def draw_reticle(frame, x, y, w, h, centre, bgr):
    """Face marker: 1px corner brackets round the box and a small crosshair on
    its centre, hard-edged (LINE_8) to match the pixel-font OSD."""
    arm = max(6, min(w, h) // 4)  # bracket arm length
    x2, y2 = x + w, y + h
    for cx, cy, dx, dy in ((x, y, 1, 1), (x2, y, -1, 1), (x, y2, 1, -1), (x2, y2, -1, -1)):
        cv2.line(frame, (cx, cy), (cx + dx * arm, cy), bgr, 1, cv2.LINE_8)
        cv2.line(frame, (cx, cy), (cx, cy + dy * arm), bgr, 1, cv2.LINE_8)
    px, py = centre
    r, gap = 7, 2  # crosshair arm length, and a gap so the centre stays open
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        cv2.line(frame, (px + dx * gap, py + dy * gap), (px + dx * r, py + dy * r),
                 bgr, 1, cv2.LINE_8)


class CommandFeed:
    """Runs a shell command every `every` seconds on a background thread and
    keeps its latest output, so a slow command never holds up tracking."""

    ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

    def __init__(self, cmd, every):
        self.cmd, self.every = cmd, every
        self.out = []
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                r = subprocess.run(self.cmd, shell=True, capture_output=True,
                                   text=True, errors="replace", timeout=max(5, self.every * 2))
                text = r.stdout + r.stderr
            except subprocess.TimeoutExpired:
                text = "(timed out)"
            lines = [self.ANSI.sub("", l).expandtabs(4).rstrip() for l in text.splitlines()]
            with self.lock:
                self.out = lines
            time.sleep(self.every)

    def lines(self):
        with self.lock:
            return list(self.out)


def fit_window(frame, win):
    """The frame scaled by the largest whole number that fits the window,
    each pixel an exact block, centred on black and exactly the window's
    size, so the window shows it 1:1 and pixel fonts stay crisp at any
    window size, fullscreen included."""
    try:
        _, _, ww, wh = cv2.getWindowImageRect(win)
    except cv2.error:
        return frame
    h, w = frame.shape[:2]
    if ww <= 0 or wh <= 0:
        return frame
    k = max(1, min(ww // w, wh // h))
    if k > 1:
        frame = cv2.resize(frame, None, fx=k, fy=k, interpolation=cv2.INTER_NEAREST)
        h, w = h * k, w * k
    if (w, h) == (ww, wh) or w > ww or h > wh:
        return frame
    out = np.zeros((wh, ww, 3), np.uint8)
    y, x = (wh - h) // 2, (ww - w) // 2
    out[y:y + h, x:x + w] = frame
    return out


def open_camera(dev, w, h):
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # we want the newest frame, not a queue
    if not cap.isOpened():
        raise SystemExit("Can't open camera %s" % dev)
    return cap


def find_audio_source(want):
    """A PulseAudio/PipeWire source name: `want` if given, else the first one
    whose name contains "UGREEN" (the gimbal camera's mic), else "default"."""
    if want:
        return want
    try:
        out = subprocess.run(["pactl", "list", "sources", "short"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return "default"
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) > 1 and "UGREEN" in cols[1] and not cols[1].endswith(".monitor"):
            return cols[1]
    return "default"


class Recorder:
    """Pipe BGR frames into ffmpeg, which adds live audio and writes an MP4.

    Both inputs are stamped with wall-clock time, so audio and video stay in
    sync even though the camera's frame rate wanders (~17fps here). Output is
    H.264/AAC at a constant 30fps, which Messenger and phones are happy with.
    """

    def __init__(self, path, w, h, audio_source=None):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-thread_queue_size", "512", "-use_wallclock_as_timestamps", "1",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "%dx%d" % (w, h), "-i", "-"]
        if audio_source:
            cmd += ["-thread_queue_size", "512", "-f", "pulse", "-i", audio_source,
                    "-c:a", "aac", "-b:a", "128k"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", "30",
                "-movflags", "+faststart", "-shortest", path]
        self.path = path
        # Own session: a terminal Ctrl-C goes to the whole foreground process
        # group, and if ffmpeg gets it directly it bails out on its own and
        # loses the tail of the recording. We stop it ourselves in close().
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, start_new_session=True)

    def write(self, frame):
        try:
            self.proc.stdin.write(frame.tobytes())
        except BrokenPipeError:
            raise SystemExit("ffmpeg stopped recording (see its error above)")

    def close(self):
        # End of video + -shortest stops ffmpeg; SIGINT is the fallback,
        # because the live audio input never ends on its own.
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.send_signal(signal.SIGINT)
            self.proc.wait(timeout=5)
        print("saved", self.path)


def tailscale_ip():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=3).stdout.split()
        return out[0] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


class Streamer:
    """Serve the newest frame as MJPEG over HTTP, from a background thread.

    The tracking loop calls push(frame); each HTTP client gets the latest JPEG
    whenever a new one arrives. Slow clients just skip frames -- they never
    hold up tracking.
    """

    PAGE = (b"<!doctype html><title>Patti</title>"
            b"<body style='margin:0;background:#000;display:grid;place-items:center;height:100vh'>"
            b"<img src='/stream' style='max-width:100%;max-height:100vh'>")

    def __init__(self, bind, port, quality=75):
        self.quality = quality
        self.jpeg = None
        self.seq = 0
        self.cond = threading.Condition()
        streamer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/":
                    self._send(200, "text/html", streamer.PAGE)
                elif self.path == "/snap.jpg":
                    jpg = streamer.wait(0)
                    self._send(200, "image/jpeg", jpg) if jpg else self._send(503, "text/plain", b"no frame yet")
                elif self.path == "/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    seen = -1
                    try:
                        while True:
                            jpg, seen = streamer.next_after(seen)
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                            self.wfile.write(jpg + b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
                    self._send(404, "text/plain", b"not found")

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server((bind, port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def push(self, frame):
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if ok:
            with self.cond:
                self.jpeg = buf.tobytes()
                self.seq += 1
                self.cond.notify_all()

    def wait(self, timeout):
        with self.cond:
            return self.jpeg

    def next_after(self, seen):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != seen and self.jpeg is not None)
            return self.jpeg, self.seq

    def close(self):
        self.server.shutdown()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", default="/dev/video4")
    ap.add_argument("--port", default=picolink.DEFAULT_PORT)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--hfov", type=float, default=65.0,
                    help="camera horizontal field of view, degrees")
    ap.add_argument("--smooth", type=float, default=0.35,
                    help="0-1: how much each frame's detection moves the target "
                         "(lower = smoother but laggier)")
    ap.add_argument("--deadband", type=float, default=3.0,
                    help="degrees off-centre we tolerate before starting to follow")
    ap.add_argument("--latency", type=float, default=0.16,
                    help="camera latency: how old a frame is when we get it, seconds")
    ap.add_argument("--window-scale", type=int, default=2,
                    help="initial preview window size, as a multiple of the camera "
                         "picture; it can be resized or fullscreened after")
    ap.add_argument("--font", help="OSD font to start with, by (part of) its name; "
                                   "'f' in the window cycles through them")
    ap.add_argument("--font-size", type=int, default=16,
                    help="OSD text size in px; keep pixel fonts like Silkscreen "
                         "to multiples of 8")
    ap.add_argument("--overlay", metavar="CMD",
                    help="shell command whose output is drawn bottom-left, rerun "
                         "every --overlay-every seconds")
    ap.add_argument("--overlay-every", type=float, default=2.0, metavar="SECS")
    ap.add_argument("--mirror", action="store_true",
                    help="the camera image is mirrored (pans the wrong way)")
    ap.add_argument("--who", action="store_true",
                    help="put names on faces (people enrolled with who.py)")
    ap.add_argument("--dry-run", action="store_true",
                    help="don't talk to the Pico")
    ap.add_argument("--record", metavar="FILE.mp4",
                    help="save the session as an MP4 with sound")
    ap.add_argument("--record-clean", action="store_true",
                    help="record the plain camera picture, without the overlay")
    ap.add_argument("--audio-source", help="PulseAudio source name to record")
    ap.add_argument("--no-audio", action="store_true", help="record without sound")
    ap.add_argument("--stream", type=int, metavar="PORT",
                    help="serve MJPEG at http://<host>:PORT/ instead of a window")
    ap.add_argument("--stream-bind", metavar="ADDR",
                    help="address to serve on (default: this host's Tailscale IP)")
    args = ap.parse_args()

    cap = open_camera(args.camera, args.width, args.height)
    ok, frame = cap.read()
    if not ok:
        raise SystemExit("Camera gave no frame")
    h, w = frame.shape[:2]
    vfov = args.hfov * h / w

    osd = Osd(args.font, args.font_size)
    feed = CommandFeed(args.overlay, args.overlay_every) if args.overlay else None
    det = cv2.FaceDetectorYN.create(MODEL, "", (w, h), score_threshold=0.8)
    names = faces.Labeller(faces.Who()) if args.who else None

    rec = None
    if args.record:
        src = None if args.no_audio else find_audio_source(args.audio_source)
        rec = Recorder(os.path.expanduser(args.record), w, h, src)
        print("recording to %s (audio: %s)" % (rec.path, src or "none"))

    streamer = None
    if args.stream:
        bind = args.stream_bind or tailscale_ip() or "127.0.0.1"
        streamer = Streamer(bind, args.stream)
        print("streaming at http://%s:%d/" % (bind, args.stream))
    show_window = streamer is None and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if streamer is None and not show_window:
        raise SystemExit("No display here: use --stream PORT to watch it in a browser")

    ser = pose = None
    if not args.dry_run:
        ser = picolink.open_port(args.port)
        picolink.start(ser)  # centres the gimbal
        pose = picolink.Pose(ser)
    pan = tilt = 0.0        # where we've told the gimbal to go
    history = collections.deque([(time.monotonic(), 0.0, 0.0)], maxlen=64)
    face = None             # smoothed face direction, gimbal degrees
    last_seen = 0.0
    following = False
    hold_until = time.monotonic() + 1.0  # let the start-up centring finish
    paused = False

    # Ctrl-C in the terminal just asks the loop to stop, so it always exits
    # through the same cleanup as 'q', not from wherever the interrupt landed
    # (often inside OpenCV's GUI code, which can swallow it).
    stop = []
    signal.signal(signal.SIGINT, lambda *_: stop.append(True))

    if show_window:
        # A normal resizable window (so a tiling WM can tile and fullscreen
        # it), no toolbar. We never let it scale the picture itself: any
        # factor that isn't whole makes pixel fonts lumpy. See fit_window().
        cv2.namedWindow("track", cv2.WINDOW_NORMAL | cv2.WINDOW_FREERATIO | cv2.WINDOW_GUI_NORMAL)
        cv2.resizeWindow("track", w * args.window_scale, h * args.window_scale)
        print("q quit (in the window, or Ctrl-C here)   c centre   space pause/resume   f font")
    else:
        print("Ctrl-C to quit")
    try:
        while not stop:
            ok, frame = cap.read()
            if not ok:
                break
            if rec is not None and args.record_clean:
                rec.write(frame)
            now = time.monotonic()
            if pose is not None:
                history.extend(pose.poll())  # also drains, or the Pico blocks on print()
                pose.request()
            else:
                history.append((now, pan, tilt))  # dry run: pretend it's where we said

            _, found = det.detect(frame)
            target = None
            if found is not None and len(found):
                # biggest face = probably the nearest person
                x, y, fw, fh = max(found, key=lambda f: f[2] * f[3])[:4].astype(int)
                target = (x + fw // 2, y + fh // 2)
                draw_reticle(frame, x, y, fw, fh, target, (0, 255, 0))
            if names is not None:
                for f, lab in zip(found if found is not None else [], names.names(frame, found, now)):
                    osd.text(frame, faces.label_text(*lab), (int(f[0]), max(0, int(f[1]) - osd.line_height - 2)),
                             (0, 255, 0) if lab[0] else (0, 190, 255))

            if target and not paused and now >= hold_until:
                ex = (target[0] - w / 2) / w * args.hfov  # + = face is right of centre
                ey = (h / 2 - target[1]) / h * vfov       # + = face is above centre
                if args.mirror:
                    ex = -ex
                # Where the face is in gimbal angles: where the camera was
                # pointing when this frame was taken, plus the offset in it.
                cp, ct = pose_at(history, now - args.latency)
                fp, ft = clamp(cp + ex, PAN_LIM), clamp(ct + ey, TILT_LIM)
                if face is None or now - last_seen > LOST_S:
                    face = (fp, ft)
                else:
                    a = args.smooth
                    face = (face[0] + a * (fp - face[0]), face[1] + a * (ft - face[1]))
                last_seen = now
                off = max(abs(face[0] - pan), abs(face[1] - tilt))
                if off > args.deadband:
                    following = True
                if following:
                    pan, tilt = face
                    if ser is not None:
                        picolink.look(ser, pan, tilt)
                    if max(abs(ex), abs(ey)) < SETTLE_DEG:  # centred in the picture
                        following = False
            elif not target:
                following = False

            # overlay: centre cross, deadband box, status
            cx, cy = w // 2, h // 2
            db_x = int(args.deadband / args.hfov * w)
            db_y = int(args.deadband / vfov * h)
            cv2.rectangle(frame, (cx - db_x, cy - db_y), (cx + db_x, cy + db_y), (0, 255, 0), 1, cv2.LINE_8)
            cv2.drawMarker(frame, (cx, cy), (0, 255, 0), cv2.MARKER_CROSS, 16, 1, cv2.LINE_8)
            state = "PAUSED" if paused else ("tracking" if target else "no face")
            if args.dry_run:
                state += " (dry run)"
            items = [("pan %+5.1f  tilt %+5.1f  %s" % (pan, tilt, state), (8, 8))]
            if feed is not None:
                # Last lines that fit in the bottom half, newest at the bottom.
                step = osd.line_height
                rows = feed.lines()[-max(1, (h // 2) // step):]
                y0 = h - 8 - step * len(rows)
                items += [(l, (8, y0 + i * step)) for i, l in enumerate(rows)]
            osd.lines(frame, items, (0, 255, 0))  # same green as the face box
            if rec is not None:
                if not args.record_clean:
                    rec.write(frame)
                cv2.circle(frame, (w - 18, 18), 7, (0, 0, 255), -1)  # REC dot, preview only
            if streamer is not None:
                streamer.push(frame)
            if not show_window:
                continue
            cv2.imshow("track", fit_window(frame, "track"))

            k = cv2.waitKey(1) & 0xFF
            if k == ord("q"):
                break
            if k == ord("f"):
                print("font:", osd.next())
            if k == ord(" "):
                paused = not paused
                following = False
            if k == ord("c"):
                pan = tilt = 0.0
                face, following = None, False
                if ser is not None:
                    picolink.look(ser, pan, tilt)
                hold_until = time.monotonic() + 1.5  # give it time to get there
    except KeyboardInterrupt:
        pass
    finally:
        if ser is not None:
            picolink.stop(ser)
        if rec is not None:
            rec.close()
        if streamer is not None:
            streamer.close()
        cap.release()
        if show_window:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
