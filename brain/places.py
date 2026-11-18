#!/usr/bin/env python3
"""Where she is, from the wifi she's on: a short list of known networks, each
a place. For the brain (server.py), which acts differently out of the house.

    python3 places.py                          # the wifi she's on, and where that is
    python3 places.py --set home               # the wifi she's on now is home
    python3 places.py --set cafe --words "at the café"
    python3 places.py --set hotspot --words "out and about" --ssid "Josh's phone"
    python3 places.py --list
    python3 places.py --forget "Some Wifi"

Kept in ~/.config/robotpattinson/places.json as {ssid: {"place", "words"}}.
"words" is how she says it ("We're {words}."), "at <place>" if not given.
Only "home" is home: anywhere else (and any wifi she doesn't know) is away.
With no wifi at all she keeps the last place she knew (a dropout at home
isn't a trip out).
"""
import argparse
import json
import os
import subprocess
import sys

CONF = os.path.expanduser("~/.config/robotpattinson/places.json")
HOME = "home"
CHECK_S = 30        # how often the brain looks


def ssid():
    """The wifi she's connected to, or None."""
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi", "list", "--rescan", "no"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.startswith("yes:"):
            return line[4:].replace("\\:", ":").strip() or None
    return None


def load():
    try:
        with open(CONF) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(known):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        json.dump(known, f, indent=1, ensure_ascii=False)
    os.replace(tmp, CONF)


class Place:
    """Where she is. check() (blocking: run it in a thread) looks again and
    says whether anything changed."""

    def __init__(self):
        self.ssid = None
        self.place = None       # a name from places.json; None: wifi she doesn't know (or none yet)
        self.words = None

    @property
    def away(self):
        return self.place != HOME

    def check(self):
        now = ssid()
        if now is None or now == self.ssid:
            return False        # (no wifi: keep what she knew)
        known = load().get(now)
        self.ssid = now
        self.place = known["place"] if known else None
        self.words = (known.get("words") or "at " + known["place"]) if known else None
        return True

    def status(self):
        return {"place": self.place, "ssid": self.ssid, "away": self.away}

    def __str__(self):
        return "%s (%s)" % (self.place or "somewhere new", self.ssid or "no wifi")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", metavar="PLACE", help="name the wifi (--ssid, else the one she's on)")
    ap.add_argument("--words", help='how she says it: "at the café"')
    ap.add_argument("--ssid", help="a wifi other than the one she's on")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--forget", metavar="SSID")
    a = ap.parse_args()
    known = load()
    if a.set:
        net = a.ssid or ssid()
        if not net:
            sys.exit("Not on any wifi: give --ssid.")
        known[net] = {"place": a.set.lower(), **({"words": a.words} if a.words else {})}
        save(known)
        print("%s is %s" % (net, a.set.lower()))
    elif a.forget:
        if known.pop(a.forget, None) is None:
            sys.exit("Don't know %s." % a.forget)
        save(known)
    elif a.list:
        for net, p in known.items():
            print("%-30s %-10s %s" % (net, p["place"], p.get("words", "")))
    else:
        p = Place()
        p.check()
        print("%s, %s" % (p, "away" if p.away else "home"))


if __name__ == "__main__":
    main()
