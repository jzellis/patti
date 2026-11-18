"""Where her sound goes, by where she is (places.py): at home, friday's
speakers (pi/music/pipewire-friday.conf); away, never friday (it's reachable
over Tailscale from anywhere, and would talk to an empty house), but
Bluetooth (earbuds, or a speaker for everyone) if one's connected, else her
fallback output (HDMI: nothing on it, so quiet; the chest still shows what
she says). For the brain (server.py), every CHECK_S.

    outputs.choose(away=True)     # blocking: the output now, set as default if it wasn't

Setting the default moves what's playing too. friday going off at home is
PipeWire's to handle (it falls back by priority, and back again).
"""
import subprocess
import sys

CHECK_S = 5
FRIDAY = "friday"


def _pactl(*args):
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def sinks():
    return [line.split("\t")[1] for line in _pactl("list", "short", "sinks").splitlines() if "\t" in line]


def wanted(away, have):
    if not away and FRIDAY in have:
        return FRIDAY
    bt = [s for s in have if s.startswith("bluez_output.")]
    if bt:
        return bt[0]
    others = [s for s in have if s != FRIDAY]
    return others[0] if others else None


def choose(away):
    have = sinks()
    want = wanted(away, have)
    now = _pactl("get-default-sink").strip()
    if want and want != now:
        _pactl("set-default-sink", want)
        print("outputs: %s (%s)" % (want, "away" if away else "home"), file=sys.stderr)
    return want or now
