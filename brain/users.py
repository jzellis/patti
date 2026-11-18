#!/usr/bin/env python3
"""Logins for her web page: who's who, and what each may do.

    python3 users.py add Josh --role admin      # asks for a password (typed here, never shown)
    python3 users.py add Sam --role family
    python3 users.py passwd Sam
    python3 users.py role Alex guest
    python3 users.py person jzellis Josh        # this login is Josh, to her
    python3 users.py list
    python3 users.py remove Alex

Each login has a profile too (PROFILE: first and last name, nicknames,
email, location), filled in on the page. She calls them by who she knows
them as, else their first name, else the login; and knows them by any of
those names, their full name and their nicknames (aliases(): "when did you
last see Joshua?" is Josh).

Kept in ~/.config/robotpattinson/users.db (SQLite, only readable by her
user): passwords as scrypt hashes, and the sessions of whoever's logged in
(SESSION_S, then log in again). Each login can be someone she knows (their
name in faces/ and voices/: a login with that very name is them anyway).
Then she knows it's them: chat from the page counts as them talking, and
memory.py notes when they were on the page.

Until anyone is added, her old shared password (server.py --set-password)
still works, with every permission; after that it doesn't.

What each permission covers:
  watch    the camera and her status
  drive    wheels, head, eyes, table mode, follow, moods
  record   recordings: start, stop, watch, delete
  listen   hear her mic (intercom)
  talk     talk through her speaker (intercom)
  say      have her say something
  chat     talk to her by typing: small talk, the time, the weather. Typed
           commands that do more need more: moving her, music, timers and
           the like need drive; who's been around and where she is need
           people; recording needs record; and her answers are only said
           out loud as well with say.
  claude   have her ask Claude what she can't answer (claude.py: it's on
           Josh's Claude subscription)
  people   who she's seen and where she is; name or forget people
  users    (nothing on the page yet: this script, on the Pi)
"""
import argparse
import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import threading
import time

DB = os.path.expanduser("~/.config/robotpattinson/users.db")
SESSION_S = 30 * 86400
PERMS = ("watch", "drive", "record", "listen", "talk", "say", "chat", "claude", "people", "users")
ROLES = {
    "admin": set(PERMS),
    "family": set(PERMS) - {"users"},
    "guest": {"watch", "say", "chat"},
    "chat": {"chat"},           # just talk to her: no camera, no moving her, nothing out loud
}
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
HER_NAMES = ("patti", "patty", "pattie")    # hers (voice.py's NAMES): nobody else's name or nickname
PROFILE = ("first", "last", "nicknames", "email", "location")     # nicknames: "Robert, Bob"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY COLLATE NOCASE, hash BLOB, salt BLOB, role TEXT,
                                  person TEXT);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, name TEXT, expires REAL);
"""


def _hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)


class Users:
    def __init__(self, path=DB):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        new = not os.path.exists(path)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        if new:
            os.chmod(path, 0o600)
        self.db.executescript(SCHEMA)
        have = [c[1] for c in self.db.execute("PRAGMA table_info(users)")]
        for col in ("person",) + PROFILE:       # (made before logins had these)
            if col not in have:
                self.db.execute("ALTER TABLE users ADD COLUMN %s TEXT" % col)

    def _q(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.fetchall()

    def any(self):
        return bool(self._q("SELECT 1 FROM users LIMIT 1"))

    def names(self):
        """[(login, role, person or None)]."""
        return self._q("SELECT name, role, person FROM users ORDER BY name")

    def add(self, name, password, role, person=None):
        salt = secrets.token_bytes(16)
        self._q("INSERT OR REPLACE INTO users (name, hash, salt, role, person) VALUES (?,?,?,?,?)",
                (name, _hash(password, salt), salt, role, person))

    def profile(self, name):
        row = self._q("SELECT %s FROM users WHERE name=?" % ", ".join(PROFILE), (name,))
        return dict(zip(PROFILE, (v or "" for v in row[0]))) if row else None

    def profiles(self):
        """{login: profile} for everyone."""
        return {r[0]: dict(zip(PROFILE, (v or "" for v in r[1:])))
                for r in self._q("SELECT name, %s FROM users" % ", ".join(PROFILE))}

    def set_profile(self, name, **fields):
        for k, v in fields.items():
            if k in PROFILE:
                self._q("UPDATE users SET %s=? WHERE name=?" % k, (v.strip() or None, name))

    def person(self, name, person):
        self._q("UPDATE users SET person=? WHERE name=?", (person or None, name))

    def passwd(self, name, password):
        salt = secrets.token_bytes(16)
        self._q("UPDATE users SET hash=?, salt=? WHERE name=?", (_hash(password, salt), salt, name))
        self._q("DELETE FROM sessions WHERE name=?", (name,))     # (logged out everywhere)

    def role(self, name, role):
        self._q("UPDATE users SET role=? WHERE name=?", (role, name))

    def remove(self, name):
        self._q("DELETE FROM sessions WHERE name=?", (name,))
        self._q("DELETE FROM users WHERE name=?", (name,))

    def check(self, name, password):
        """Their name as stored (right case), if the password's right; else None."""
        row = self._q("SELECT name, hash, salt FROM users WHERE name=?", (name,))
        if not row:
            _hash(password, b"x" * 16)          # (as slow either way)
            return None
        n, h, salt = row[0]
        return n if hmac.compare_digest(_hash(password, salt), h) else None

    def login(self, name):
        token = secrets.token_urlsafe(32)
        self._q("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        self._q("INSERT INTO sessions VALUES (?,?,?)", (token, name, time.time() + SESSION_S))
        return token

    def logout(self, token):
        self._q("DELETE FROM sessions WHERE token=?", (token,))

    def session(self, token):
        """(login, set of permissions, what she calls them) for a session
        cookie, or None."""
        if not token:
            return None
        row = self._q("SELECT u.name, u.role, u.person, u.first FROM sessions s JOIN users u ON u.name = s.name "
                      "WHERE s.token=? AND s.expires > ?", (token, time.time()))
        if not row:
            return None
        login, role, person, first = row[0]
        return login, ROLES.get(role, set()), person or first or login

    def logins(self):
        """[(login, set of permissions)] for everyone."""
        return [(n, ROLES.get(r, set())) for n, r in self._q("SELECT name, role FROM users")]

    def login_for(self, person):
        """The login of someone she knows (by face or voice, or first name), or None."""
        row = self._q("SELECT name FROM users WHERE person=? OR ((person IS NULL OR person='') AND first=?) "
                      "ORDER BY person IS NULL LIMIT 1", (person, person))
        return row[0][0] if row else None

    def user(self, login):
        """(what she calls them, set of permissions) for a login (one linked
        to their Telegram, say: telegram.py), or None."""
        row = self._q("SELECT role, person, first FROM users WHERE name=?", (login,))
        if not row:
            return None
        role, person, first = row[0]
        return person or first or login, ROLES.get(role, set())


def aliases(path=DB):
    """{name as said, lowercase: who she knows them as} from everyone's
    profile; {} without logins. (For voice.py's phrases, read at start.)"""
    if not os.path.exists(path):
        return {}
    try:
        db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        rows = db.execute("SELECT name, person, first, last, nicknames FROM users").fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for login, person, first, last, nicks in rows:
        who = person or first
        if not who:
            continue
        said = {who, first, " ".join(x for x in (first, last) if x)} | {n.strip() for n in (nicks or "").split(",")}
        for n in said:
            if n and len(n.strip()) > 1 and not set(n.lower().split()) & set(HER_NAMES):
                out.setdefault(n.strip().lower(), who)
    return out


def ask_password():
    pw = getpass.getpass("Password: ")
    if len(pw) < 6 or pw != getpass.getpass("Again: "):
        sys.exit("Passwords didn't match, or shorter than 6 characters.")
    return pw


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--role", choices=ROLES, default="family")
    sub.add_parser("passwd").add_argument("name")
    r = sub.add_parser("role")
    r.add_argument("name")
    r.add_argument("role", choices=ROLES)
    pp = sub.add_parser("person")
    pp.add_argument("name")
    pp.add_argument("person", nargs="?", help="who she knows them as (none: nobody)")
    sub.add_parser("remove").add_argument("name")
    sub.add_parser("list")
    a = ap.parse_args()
    u = Users()
    known = {n.lower() for n, _, _ in u.names()}
    if a.cmd != "add" and a.cmd != "list" and a.name.lower() not in known:
        sys.exit("No one called %s." % a.name)
    if a.cmd == "add":
        if a.name.lower() in known:
            sys.exit("%s is already there: passwd or role." % a.name)
        u.add(a.name, ask_password(), a.role)
        print("Added %s (%s). Restart nothing: it works straight away." % (a.name, a.role))
    elif a.cmd == "passwd":
        u.passwd(a.name, ask_password())
        print("Changed (and logged out everywhere).")
    elif a.cmd == "role":
        u.role(a.name, a.role)
    elif a.cmd == "person":
        u.person(a.name, a.person)
    elif a.cmd == "remove":
        u.remove(a.name)
    else:
        for n, role, person in u.names():
            print("%-15s %-8s %s" % (n, role, person or ""))


if __name__ == "__main__":
    main()
