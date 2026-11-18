"""Grab one still from a V4L2 webcam with ffmpeg, for `facectl snap`.

ffmpeg rather than OpenCV so facectl keeps its tiny dependency list (PIL and
pyserial). We let the camera run for a few frames before keeping one, because
UVC webcams start dark and settle their auto-exposure over the first ~half
second.
"""

import glob
import os
import subprocess
import tempfile

PREFERRED = ("UGREEN",)   # the gimbal camera, when it's plugged in
WARMUP_FRAMES = 15


def find_camera():
    """Pick a capture device: the gimbal camera if present, else the first
    camera. Each UVC camera exposes two nodes; index 0 is the capture one."""
    found = []
    for d in sorted(glob.glob("/sys/class/video4linux/video*")):
        try:
            name = open(os.path.join(d, "name")).read().strip()
            index = open(os.path.join(d, "index")).read().strip()
        except OSError:
            continue
        if index == "0":
            found.append(("/dev/" + os.path.basename(d), name))
    if not found:
        raise RuntimeError("no camera found under /sys/class/video4linux")
    for dev, name in found:
        if any(p in name for p in PREFERRED):
            return dev
    return found[0][0]


def snap(device=None):
    """Return the path of a JPEG grabbed from the camera (caller deletes it)."""
    device = device or find_camera()
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="facesnap-")
    os.close(fd)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "v4l2", "-i", device,
           "-vf", "select=gte(n\\,%d)" % WARMUP_FRAMES, "-fps_mode", "vfr",
           "-frames:v", "1", path]
    try:
        subprocess.run(cmd, check=True, timeout=20, capture_output=True)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not installed")
    except subprocess.CalledProcessError as e:
        os.unlink(path)
        raise RuntimeError("camera %s: %s" % (device, e.stderr.decode(errors="replace").strip()
                                              or "capture failed (in use by another app?)"))
    except subprocess.TimeoutExpired:
        os.unlink(path)
        raise RuntimeError("camera %s timed out" % device)
    return path
