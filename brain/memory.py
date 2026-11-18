#!/usr/bin/env python3
"""Her memory: who she's seen and heard, when and where, what was said to her,
where she's been; and what's going on right now. For the brain (server.py).

    python3 memory.py                   # right now, and the last sighting of everyone
    python3 memory.py --log 20          # the last 20 things said to her
    python3 memory.py --visits Josh     # Josh's recent visits

Kept in SQLite (~/.local/share/robotpattinson/memory.db: one file, no server):

  visits   who, how ("seen": face; "heard": voice; "online": on her page),
           place, first, last.
           A visit runs while she keeps seeing (or hearing) them; a gap of
           VISIT_GAP_S starts a new one. While it runs, `last` is written at
           most every FLUSH_S (and kept exactly in memory meanwhile).
  said     at, who (None: couldn't tell), place, text, action: commands.
  moves    at, place, ssid: each time the wifi says she's somewhere else.
  answers  at, who, question, answer, source, lasting, vec: what she's been
           told (by Claude, claude.py), to answer herself next time
           (recall). lasting: it stays true (else it could change: she says
           when she heard it). vec: what the question means (claude.embed),
           so a question put another way is still found. private: only
           for people who may ask Claude (Kowloon's private notes). owner:
           someone's own business (what they told her about themselves):
           only told back to them.
  moods    at, mood, why: each time her mood changes (her eyes' mood).
  feelings at, cause, who, moved (JSON: hormone -> change), why: each
           push to her feelings (limbic.py).
  chemistry at, each hormone, happy: a reading a minute (limbic.py).
  questions what she wants to ask someone (drives.py): id, at, for_who
           (a login), kind ("stranger": about a face she met, about = its
           id; "you": about them, about = how to remember the answer),
           text, asked (when), how ("voice", "telegram"), answer, answered.

Right now (place, awake or asleep, who's about, who she's talking with) is
kept in now.json next to it, rewritten when it changes: easy to look at.

A conversation: once someone she knows says something, she's talking with
them until CONVO_S passes with nobody saying anything. Meanwhile a line she
can't put a voice to (too short, or unsure) is taken as theirs: see
server.py, who_spoke.

Times are Unix seconds (time.time()). "Forget me" (meet.py) forgets this too.
"""
import argparse
import base64
import difflib
import json
import os
import re
import sqlite3
import threading
import time

import numpy as np

DIR = os.path.expanduser("~/.local/share/robotpattinson")
DB = os.path.join(DIR, "memory.db")
NOW = os.path.join(DIR, "now.json")
VISIT_GAP_S = 300       # this long unseen, and the next sighting is a new visit
FLUSH_S = 30            # a running visit's `last` is written at most this often
CONVO_S = 120           # a conversation lasts this long after the last thing said
HERE_S = 60             # seen or heard this recently: they're here now
SAME_QUESTION = 0.85    # without vecs: a question this like one she's been told about (letter by letter) is the same

# The cats, for "when did you last see Pepper?" before she can tell them apart.
CATS = ("Pepper", "Biscuit", "Ziggy")

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (id INTEGER PRIMARY KEY, who TEXT, how TEXT, place TEXT,
                                   first REAL, last REAL);
CREATE INDEX IF NOT EXISTS visits_who ON visits (who, last);
CREATE TABLE IF NOT EXISTS said (at REAL, who TEXT, place TEXT, text TEXT, action TEXT);
CREATE TABLE IF NOT EXISTS moves (at REAL, place TEXT, ssid TEXT);
CREATE TABLE IF NOT EXISTS moods (at REAL, mood TEXT, why TEXT);
CREATE TABLE IF NOT EXISTS feelings (at REAL, cause TEXT, who TEXT, moved TEXT, why TEXT);
CREATE TABLE IF NOT EXISTS chemistry (at REAL, oxytocin REAL, dopamine REAL, serotonin REAL, cortisol REAL,
                                      adrenaline REAL, melatonin REAL, happy REAL);
CREATE INDEX IF NOT EXISTS chemistry_at ON chemistry (at);
CREATE TABLE IF NOT EXISTS questions (id INTEGER PRIMARY KEY, at REAL, for_who TEXT, kind TEXT, about TEXT,
                                      text TEXT, asked REAL, how TEXT, answer TEXT, answered REAL);
CREATE TABLE IF NOT EXISTS answers (at REAL, who TEXT, question TEXT, answer TEXT, source TEXT,
                                    lasting INTEGER, vec BLOB, private INTEGER);
"""


class Memory:
    def __init__(self, path=DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        have = [c[1] for c in self.db.execute("PRAGMA table_info(answers)")]
        for col, kind in (("lasting", "INTEGER"), ("vec", "BLOB"), ("private", "INTEGER"), ("owner", "TEXT")):
            if col not in have:         # (made before these)
                self.db.execute("ALTER TABLE answers ADD COLUMN %s %s" % (col, kind))
        if "due" not in [c[1] for c in self.db.execute("PRAGMA table_info(questions)")]:
            self.db.execute("ALTER TABLE questions ADD COLUMN due REAL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.open = {}          # (who, how) -> [visit id, last, last written]
        self.place = None
        self.convo = None       # [name, since, last]
        self.mood = None
        self.now = {}           # what's in now.json
        self.closed = False     # (shutting down: what's felt or read now is let go)

    # ---------- remembering ----------
    def _visit(self, who, how, t=None):
        t = t or time.time()
        with self.lock:
            v = self.open.get((who, how))
            if not v:       # (just started: carry on a visit from before a restart)
                row = self.db.execute("SELECT id, last FROM visits WHERE who=? AND how=? ORDER BY last DESC LIMIT 1",
                                      (who, how)).fetchone()
                if row and t - row[1] < VISIT_GAP_S:
                    v = self.open[(who, how)] = [row[0], row[1], row[1]]
            if v and t - v[1] < VISIT_GAP_S:
                v[1] = t
                if t - v[2] >= FLUSH_S:
                    self.db.execute("UPDATE visits SET last=? WHERE id=?", (t, v[0]))
                    self.db.commit()
                    v[2] = t
                return
            if v:
                self._flush(v)
            cur = self.db.execute("INSERT INTO visits (who, how, place, first, last) VALUES (?,?,?,?,?)",
                                  (who, how, self.place, t, t))
            self.db.commit()
            self.open[(who, how)] = [cur.lastrowid, t, t]

    def _flush(self, v):
        if v[1] > v[2]:
            self.db.execute("UPDATE visits SET last=? WHERE id=?", (v[1], v[0]))
            v[2] = v[1]

    def seen(self, who):
        """Her camera recognised them."""
        self._visit(who, "seen")

    def heard(self, who):
        """She recognised their voice (or took it as her conversation's)."""
        self._visit(who, "heard")

    def online(self, who):
        """They're logged in on her page (users.py)."""
        self._visit(who, "online")

    def said(self, who, text, action):
        """A command, and who said it (None: couldn't tell)."""
        t = time.time()
        with self.lock:
            self.db.execute("INSERT INTO said VALUES (?,?,?,?,?)", (t, who, self.place, text, action))
            self.db.commit()
        if who:
            if self.convo and self.convo[0] == who and t - self.convo[2] < CONVO_S:
                self.convo[2] = t
            else:
                self.convo = [who, t, t]

    def moved(self, place, ssid):
        self.place = place
        with self.lock:
            self.db.execute("INSERT INTO moves VALUES (?,?,?)", (time.time(), place, ssid))
            self.db.commit()

    def learned(self, who, question, answer, source, lasting=False, vec=None, private=False, owner=None):
        with self.lock:
            self.db.execute("INSERT INTO answers (at, who, question, answer, source, lasting, vec, private, owner) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (time.time(), who, question, answer, source, int(lasting),
                             None if vec is None else np.asarray(vec, np.float32).tobytes(), int(private), owner))
            self.db.commit()

    def known_question(self, question):
        """She's already been told the answer to exactly this question."""
        with self.lock:
            return self.db.execute("SELECT 1 FROM answers WHERE question=? LIMIT 1", (question,)).fetchone() is not None

    def unembedded(self):
        """[(rowid, question)] of answers with no vec yet (friday was off)."""
        with self.lock:
            return self.db.execute("SELECT rowid, question FROM answers WHERE vec IS NULL").fetchall()

    def set_vec(self, rowid, vec):
        with self.lock:
            self.db.execute("UPDATE answers SET vec=? WHERE rowid=?", (np.asarray(vec, np.float32).tobytes(), rowid))
            self.db.commit()

    def answer(self, rowid):
        with self.lock:
            r = self.db.execute("SELECT rowid, question, answer, at, source, lasting, NULL, private, owner FROM answers "
                                "WHERE rowid=?", (rowid,)).fetchone()
        return _answer(r, 1.0) if r else None

    def recall(self, question, vec=None, private=False, floor=0.8, k=3, asker=None):
        """The answers she's been told for the questions most like this one,
        best first (up to k, at least floor alike; the latest of equals), as
        [{id, question, answer, at, source, lasting, private, score}]. score:
        how alike the questions mean (with vec; 1 = the same), else 1 for
        near enough letter by letter. Private answers only if private;
        someone's own business (owner) only for them (asker)."""
        q = _plain(question)
        with self.lock:
            rows = self.db.execute("SELECT rowid, question, answer, at, source, lasting, vec, private, owner, who FROM answers "
                                   "WHERE (? OR NOT IFNULL(private, 0)) AND (owner IS NULL OR owner = ?) ORDER BY at DESC",
                                   (int(private), asker)).fetchall()
        found = []
        for r in rows:
            if vec is not None and r[6] is not None:
                score = float(np.frombuffer(r[6], np.float32) @ vec)
            elif difflib.SequenceMatcher(None, q, _plain(r[1])).ratio() >= SAME_QUESTION:
                score = 1.0
            else:
                continue
            if score >= floor:
                found.append(_answer(r, score))
        found.sort(key=lambda a: -a["score"])       # (stable: the latest of equals first)
        return found[:k]

    def related(self, vecs, k=6, floor=0.68, skip=("book", "books")):
        """What she knows that's most like any of these meanings (vecs: the sentences of something she's
        read, say), best first: [{id, question, answer, at, source, who, score, ...}]. Nothing private or
        anyone's own business, and nothing from skip (her reading itself)."""
        if not len(vecs):
            return []
        m = np.asarray(vecs, np.float32)
        with self.lock:
            rows = self.db.execute("SELECT rowid, question, answer, at, source, lasting, vec, private, owner, who "
                                   "FROM answers WHERE NOT IFNULL(private, 0) AND owner IS NULL AND vec IS NOT NULL "
                                   "AND source NOT IN (%s)" % ",".join("?" * len(skip)), tuple(skip)).fetchall()
        found = []
        for r in rows:
            score = float(np.max(m @ np.frombuffer(r[6], np.float32)))
            if score >= floor:
                found.append(_answer(r, score))
        found.sort(key=lambda a: -a["score"])
        return found[:k]

    def branch(self, since):
        """Everything she wrote since then, for merging elsewhere (merge): {table: [[values], ...]},
        JSON-safe (blobs as {"b64"})."""
        out = {}
        with self.lock:
            for t, (cols, _) in MERGE_LOGS.items():
                out[t] = [[_out(v) for v in r] for r in self.db.execute(
                    "SELECT %s FROM %s WHERE at >= ? ORDER BY at" % (",".join(cols), t), (since,))]
            out["visits"] = [list(r) for r in self.db.execute(
                "SELECT %s FROM visits WHERE last >= ? OR first >= ?" % ",".join(VISIT_COLS), (since, since))]
            out["questions"] = [list(r) for r in self.db.execute(
                "SELECT %s FROM questions WHERE at >= ? OR IFNULL(asked, 0) >= ? OR IFNULL(answered, 0) >= ?"
                % ",".join(QUESTION_COLS), (since, since, since))]
        return out

    def merge(self, part):
        """Fold another machine's branch (or a piece of it) in: {table: added or changed}."""
        got = {}
        with self.lock:
            for t, rows in part.items():
                n = 0
                if t in MERGE_LOGS:
                    cols, key = MERGE_LOGS[t]
                    for r in rows:
                        row = dict(zip(cols, (_in(v) for v in r)))
                        if self.db.execute("SELECT 1 FROM %s WHERE %s LIMIT 1" % (t, " AND ".join(
                                "%s IS ?" % k for k in key)), [row[k] for k in key]).fetchone():
                            continue
                        self.db.execute("INSERT INTO %s (%s) VALUES (%s)" % (t, ",".join(cols), ",".join("?" * len(cols))),
                                        [row[c] for c in cols])
                        n += 1
                elif t == "visits":
                    for r in rows:
                        v = dict(zip(VISIT_COLS, r))
                        same = self.db.execute("SELECT id, last FROM visits WHERE who IS ? AND how IS ? AND first IS ?",
                                               (v["who"], v["how"], v["first"])).fetchone()
                        if not same:
                            self.db.execute("INSERT INTO visits (who, how, place, first, last) VALUES (?,?,?,?,?)",
                                            [v[c] for c in VISIT_COLS])
                            n += 1
                        elif (v["last"] or 0) > (same[1] or 0):
                            self.db.execute("UPDATE visits SET last=? WHERE id=?", (v["last"], same[0]))
                            n += 1
                elif t == "questions":
                    for r in rows:
                        q = dict(zip(QUESTION_COLS, r))
                        same = self.db.execute("SELECT id, asked, answered FROM questions WHERE for_who IS ? AND text IS ? "
                                               "ORDER BY answered IS NOT NULL DESC LIMIT 1",
                                               (q["for_who"], q["text"])).fetchone()
                        if not same:
                            self.db.execute("INSERT INTO questions (%s) VALUES (%s)" % (
                                ",".join(QUESTION_COLS), ",".join("?" * len(QUESTION_COLS))), [q[c] for c in QUESTION_COLS])
                            n += 1
                            continue
                        if q["asked"] and (not same[1] or q["asked"] > same[1]):
                            self.db.execute("UPDATE questions SET asked=? WHERE id=?", (q["asked"], same[0]))
                            n += 1
                        if q["answered"] and not same[2]:
                            self.db.execute("UPDATE questions SET answer=?, answered=?, how=? WHERE id=?",
                                            (q["answer"], q["answered"], q["how"], same[0]))
                            n += 1
                got[t] = n
            self.db.commit()
        return got

    def lately(self, n=8, asker=None, since=None):
        """What she's learned most recently, newest first: [(at, who, question,
        answer, source)]. Nothing private; someone's own business only for them."""
        with self.lock:
            return self.db.execute("SELECT at, who, question, answer, source FROM answers WHERE NOT IFNULL(private, 0) "
                                   "AND (owner IS NULL OR owner = ?) AND at >= ? ORDER BY at DESC LIMIT ?",
                                   (asker, since or 0, n)).fetchall()

    def names(self, n=40):
        """Names in the questions she's been told answers to, newest first
        ("Tom Waits", "Kowloon"), at most n: for Whisper's hint."""
        with self.lock:
            qs = [r[0] for r in self.db.execute("SELECT question FROM answers ORDER BY at DESC LIMIT 200")]
        out = []
        for q in qs:
            for m in re.finditer(r"(?<![.?!]\s)(?<!^)\b([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*)", q):
                name = m.group(1)
                if name not in out:
                    out.append(name)
        return out[:n]

    def feeling(self, cause, who, moved, why=None):
        """A push to her feelings (limbic.py)."""
        if self.closed:
            return
        with self.lock:
            self.db.execute("INSERT INTO feelings VALUES (?,?,?,?,?)", (time.time(), cause, who, json.dumps(moved), why))
            self.db.commit()

    CHEMISTRY = ("oxytocin", "dopamine", "serotonin", "cortisol", "adrenaline", "melatonin", "happy")

    def chemistry(self, levels):
        """A reading of all her hormones (limbic.py)."""
        if self.closed:
            return
        with self.lock:
            self.db.execute("INSERT INTO chemistry VALUES (?,?,?,?,?,?,?,?)",
                            (time.time(),) + tuple(levels.get(k) for k in self.CHEMISTRY))
            self.db.commit()

    def feelings(self, since):
        """[(at, cause, who, {hormone: change}, why)] since then, oldest first."""
        with self.lock:
            rows = self.db.execute("SELECT at, cause, who, moved, why FROM feelings WHERE at >= ? ORDER BY at",
                                   (since,)).fetchall()
        return [(at, c, w, json.loads(m or "{}"), why) for at, c, w, m, why in rows]

    def readings(self, since):
        """[{at, each hormone, happy}] since then, oldest first."""
        with self.lock:
            rows = self.db.execute("SELECT at, %s FROM chemistry WHERE at >= ? ORDER BY at" % ", ".join(self.CHEMISTRY),
                                   (since,)).fetchall()
        return [dict(zip(("at",) + self.CHEMISTRY, r)) for r in rows]

    def wonder(self, for_who, kind, text, about=None, due=None):
        """Something she wants to ask them (drives.py), not before due (a
        time, or None: now); its id. Not twice: the same question,
        unanswered, is the same one."""
        with self.lock:
            row = self.db.execute("SELECT id FROM questions WHERE for_who=? AND text=? AND answered IS NULL",
                                  (for_who, text)).fetchone()
            if row:
                return row[0]
            cur = self.db.execute("INSERT INTO questions (at, for_who, kind, about, text, due) VALUES (?,?,?,?,?,?)",
                                  (time.time(), for_who, kind, about, text, due))
            self.db.commit()
            return cur.lastrowid

    def wondering(self, for_who=None, kind=None):
        """Her unanswered questions, due now [{id, at (or when it fell due), for_who, kind, about, text,
        asked, how}], oldest first."""
        sql = ("SELECT id, MAX(at, IFNULL(due, 0)), for_who, kind, about, text, asked, how FROM questions "
               "WHERE answered IS NULL AND IFNULL(due, 0) <= ?")
        args = [time.time()]
        if for_who:
            sql, args = sql + " AND for_who=?", args + [for_who]
        if kind:
            sql, args = sql + " AND kind=?", args + [kind]
        with self.lock:
            rows = self.db.execute(sql + " ORDER BY 2", args).fetchall()
        return [dict(zip(("id", "at", "for_who", "kind", "about", "text", "asked", "how"), r)) for r in rows]

    def question(self, qid):
        with self.lock:
            r = self.db.execute("SELECT id, at, for_who, kind, about, text, asked, how, answer, answered "
                                "FROM questions WHERE id=?", (qid,)).fetchone()
        return dict(zip(("id", "at", "for_who", "kind", "about", "text", "asked", "how", "answer", "answered"), r)) \
            if r else None

    def asked(self, qid, how):
        with self.lock:
            self.db.execute("UPDATE questions SET asked=?, how=? WHERE id=?", (time.time(), how, qid))
            self.db.commit()

    def unasked(self, qid):
        """Put to an empty room after all: not asked, so it's asked again (and isn't counted)."""
        with self.lock:
            self.db.execute("UPDATE questions SET asked=NULL, how=NULL WHERE id=?", (qid,))
            self.db.commit()

    def answered(self, qid, answer):
        """answer None: never mind (it's not wanted any more)."""
        with self.lock:
            self.db.execute("UPDATE questions SET answer=?, answered=? WHERE id=?", (answer, time.time(), qid))
            self.db.commit()

    def asked_since(self, for_who, since):
        """How many questions she's put to them since then."""
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM questions WHERE for_who=? AND asked >= ?",
                                   (for_who, since)).fetchone()[0]

    def asked_list(self, for_who, since):
        """How each question she's put to them since then was put ("voice", "telegram:...")."""
        with self.lock:
            return [r[0] or "" for r in self.db.execute("SELECT how FROM questions WHERE for_who=? AND asked >= ?",
                                                        (for_who, since))]

    def ever_asked(self, for_who):
        """Every question she's ever had for them (asked or not), as text."""
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT text FROM questions WHERE for_who=?", (for_who,))}

    def felt(self, mood, why=None):
        """Her mood, logged when it changes."""
        if mood == self.mood:
            return
        self.mood = mood
        with self.lock:
            self.db.execute("INSERT INTO moods VALUES (?,?,?)", (time.time(), mood, why))
            self.db.commit()

    def forget(self, who):
        with self.lock:
            for how in ("seen", "heard"):
                self.open.pop((who, how), None)
            self.db.execute("DELETE FROM visits WHERE who=?", (who,))
            self.db.execute("UPDATE said SET who=NULL WHERE who=?", (who,))
            self.db.execute("UPDATE feelings SET who=NULL WHERE who=?", (who,))
            self.db.commit()
        if self.convo and self.convo[0] == who:
            self.convo = None

    def rename(self, old, new):
        """Someone she didn't know (strangers.py) has a name now."""
        with self.lock:
            for how in ("seen", "heard"):
                v = self.open.pop((old, how), None)
                if v:
                    self._flush(v)
                    if (new, how) not in self.open:
                        self.open[(new, how)] = v
            self.db.execute("UPDATE visits SET who=? WHERE who=?", (new, old))
            self.db.execute("UPDATE said SET who=? WHERE who=?", (new, old))
            self.db.execute("UPDATE feelings SET who=? WHERE who=?", (new, old))
            self.db.commit()

    def people(self):
        """{who: (first, last, visits)} for everyone she remembers."""
        with self.lock:
            for v in self.open.values():
                self._flush(v)
            self.db.commit()
            return {who: (first, last, n) for who, first, last, n in self.db.execute(
                "SELECT who, MIN(first), MAX(last), COUNT(*) FROM visits GROUP BY who")}

    def close(self):
        with self.lock:
            for v in self.open.values():
                self._flush(v)
            self.db.commit()
            self.db.close()
            self.closed = True

    # ---------- recalling ----------
    def talking(self):
        """Who she's in a conversation with, or None."""
        c = self.convo
        return c[0] if c and time.time() - c[2] < CONVO_S else None

    def last(self, who):
        """(when, how, place) she last saw or heard them, or None."""
        with self.lock:
            best = None
            for (w, how), v in self.open.items():
                if w == who and (not best or v[1] > best[0]):
                    best = (v[1], how, None)
            row = self.db.execute("SELECT last, how, place FROM visits WHERE who=? ORDER BY last DESC LIMIT 1",
                                  (who,)).fetchone()
        if row and (not best or row[0] >= best[0]):
            return row
        if best:            # (still running: where it started)
            with self.lock:
                place = self.db.execute("SELECT place FROM visits WHERE id=?",
                                        (self.open[(who, best[1])][0],)).fetchone()
            return best[0], best[1], place[0] if place else None
        return None

    def before(self, who):
        """(when, how, place) she last saw or heard them (or, if never, had
        them online) before now: a visit still going on (within HERE_S)
        doesn't count, the one before it does. For "when did you last see
        me?". None if never."""
        now, best = time.time(), None
        with self.lock:
            for how in ("seen", "heard", "online"):
                rows = self.db.execute("SELECT id, last, place FROM visits WHERE who=? AND how=? "
                                       "ORDER BY last DESC LIMIT 2", (who, how)).fetchall()
                if not rows:
                    continue
                v = self.open.get((who, how))
                top = max(rows[0][1], v[1] if v and v[0] == rows[0][0] else 0)
                pick = rows[1] if now - top < HERE_S else rows[0]
                if pick is not rows[0] and len(rows) < 2:
                    continue
                if how == "online" and best:
                    break           # (seen or heard: that's the answer)
                if not best or pick[1] > best[0]:
                    best = (pick[1], how, pick[2])
        return best

    def state(self, **now):
        """Update now.json with what's going on (place, awake, present, ...)
        plus who she's talking with; written only when something changed."""
        now = dict(now, place=self.place, mood=self.mood, talking=self.talking())
        if now == self.now:
            return
        self.now = now
        try:
            tmp = NOW + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dict(now, updated=time.strftime("%Y-%m-%d %H:%M:%S")), f, indent=1)
            os.replace(tmp, NOW)
        except OSError:
            pass


# Merging (mind.py on friday -> the Pi's brain, server.py /merge): what she lived through on one
# machine, folded into the other's memory, like a git merge. Logs are append-only, so they just
# combine (a row the same in time and content isn't added twice: merging again is harmless);
# visits keep the later "last"; her questions for people match by who and wording, and an
# answer from either side wins.
MERGE_LOGS = {          # table: (columns, the ones that make a row the same)
    "answers": (("at", "who", "question", "answer", "source", "lasting", "vec", "private", "owner"), ("at", "question")),
    "said": (("at", "who", "place", "text", "action"), ("at", "text")),
    "feelings": (("at", "cause", "who", "moved", "why"), ("at", "cause")),
    "moods": (("at", "mood", "why"), ("at",)),
    "chemistry": (("at", "oxytocin", "dopamine", "serotonin", "cortisol", "adrenaline", "melatonin", "happy"), ("at",)),
    "moves": (("at", "place", "ssid"), ("at",)),
}
VISIT_COLS = ("who", "how", "place", "first", "last")
QUESTION_COLS = ("at", "for_who", "kind", "about", "text", "asked", "how", "answer", "answered", "due")


def _out(v):
    return {"b64": base64.b64encode(v).decode()} if isinstance(v, bytes) else v


def _in(v):
    return base64.b64decode(v["b64"]) if isinstance(v, dict) and "b64" in v else v


def _answer(r, score):
    return {"id": r[0], "question": r[1], "answer": r[2], "at": r[3], "source": r[4], "lasting": bool(r[5]),
            "private": bool(r[7]), "owner": r[8] if len(r) > 8 else None, "who": r[9] if len(r) > 9 else None,
            "score": score}


def _plain(text):
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


def when(t, now=None):
    """'at 3:17pm', 'yesterday at 9:05am', 'on Saturday at 2:40pm', 'on 12 October'."""
    now = now or time.time()
    clock = time.strftime("%I:%M%p", time.localtime(t)).lstrip("0").lower()
    day = lambda x: time.strftime("%Y-%m-%d", time.localtime(x))
    if day(t) == day(now):
        return "at " + clock
    if day(t) == day(now - 86400):
        return "yesterday at " + clock
    if now - t < 6 * 86400:
        return "on %s at %s" % (time.strftime("%A", time.localtime(t)), clock)
    return "on " + time.strftime("%d %B", time.localtime(t)).lstrip("0")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", type=int, metavar="N", help="the last N things said to her")
    ap.add_argument("--visits", metavar="WHO")
    a = ap.parse_args()
    db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    fmt = lambda t: time.strftime("%a %d %b %H:%M:%S", time.localtime(t))
    if a.log:
        for at, who, place, text, action in reversed(db.execute(
                "SELECT * FROM said ORDER BY at DESC LIMIT ?", (a.log,)).fetchall()):
            print("%s  %-10s %-8s %s" % (fmt(at), who or "?", place or "", text))
    elif a.visits:
        for how, place, first, last in db.execute(
                "SELECT how, place, first, last FROM visits WHERE who=? ORDER BY last DESC LIMIT 20", (a.visits,)):
            print("%s - %s  %-6s %s" % (fmt(first), time.strftime("%H:%M:%S", time.localtime(last)), how, place or ""))
    else:
        try:
            with open(NOW) as f:
                print(f.read())
        except OSError:
            pass
        for who, last, how, place in db.execute(
                "SELECT who, MAX(last), how, place FROM visits GROUP BY who ORDER BY 2 DESC"):
            print("%-12s %s %s (%s)" % (who, how, when(last), place or "?"))


if __name__ == "__main__":
    main()
