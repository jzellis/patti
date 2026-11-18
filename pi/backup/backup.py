#!/usr/bin/env python3
"""Patti's data, copied to friday (friday/ask_claude.py, /backup): run hourly
by rp-backup.timer.

A snapshot of everything that's hers and can't be downloaded again: her
memory (memory.db, copied with SQLite's online backup, so it's consistent
while she's using it), the logins (users.db), the faces and voices she
knows, the strangers she's met, her notes, her feelings (limbic.json), and
her settings and keys (~/.config/robotpattinson). Not the speech and
recognition models (downloadable). Then any recordings friday hasn't got.

    python3 backup.py           # now (the timer's way)
    python3 backup.py --dry     # what would go, without sending
"""
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
DATA = os.path.join(HOME, ".local/share/robotpattinson")
CONFIG = os.path.join(HOME, ".config/robotpattinson")
RECORDINGS = os.path.join(HOME, "Videos/robotpattinson")
KEEP = ["faces", "voices", "strangers", "notes", "limbic.json", "now.json", "pressure.json"]
DATABASES = [os.path.join(DATA, "memory.db"), os.path.join(CONFIG, "users.db")]
URL = os.environ.get("RP_ASK_CLAUDE", "http://100.64.0.10:8765/ask").rsplit("/", 1)[0] + "/backup"
KEY_FILE = os.path.join(CONFIG, "claude-key")
TIMEOUT_S = 600


def post(query, body=b"", size=None):
    """body: bytes, or a file (streamed: size is its length)."""
    with open(KEY_FILE) as f:
        key = f.read().strip()
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/octet-stream"}
    if size is not None:
        headers["Content-Length"] = str(size)
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(query), data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.load(r)


def snapshot():
    """The tar.gz, in memory (a few MB)."""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for db in DATABASES:
            if os.path.exists(db):
                copy = os.path.join(tmp, os.path.basename(db))
                src, dst = sqlite3.connect("file:%s?mode=ro" % db, uri=True), sqlite3.connect(copy)
                src.backup(dst)
                src.close()
                dst.close()
                tar.add(copy, arcname="databases/" + os.path.basename(db))
        for name in KEEP:
            p = os.path.join(DATA, name)
            if os.path.exists(p):
                tar.add(p, arcname="data/" + name)
        tar.add(CONFIG, arcname="config", filter=lambda t: None if t.name.endswith(".db") else t)
    return buf.getvalue()


def main():
    dry = "--dry" in sys.argv
    snap = snapshot()
    print("snapshot: %.1f MB" % (len(snap) / 1e6))
    if not dry:
        print(post({"name": "data.tar.gz"}, snap)["saved"])
    recs = sorted(f for f in os.listdir(RECORDINGS) if not f.startswith(".")) if os.path.isdir(RECORDINGS) else []
    have = set() if dry else set(post({"list": "rec"})["have"])
    for f in recs:
        if f in have or f.endswith(".part"):
            continue
        p = os.path.join(RECORDINGS, f)
        print("recording: %s (%.1f MB)" % (f, os.path.getsize(p) / 1e6))
        if not dry:
            with open(p, "rb") as fh:
                post({"rec": f}, fh, os.path.getsize(p))


if __name__ == "__main__":
    main()
