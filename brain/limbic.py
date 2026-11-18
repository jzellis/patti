#!/usr/bin/env python3
"""Her feelings: a few "hormones", numbers from 0 to 1, that things that
happen push up or down, and that drift back to where they rest. For the brain
(server.py), which tells it what happened (felt) and asks how she is.

    lim = limbic.Limbic(memory)          # (memory.py: logs every change and a reading a minute)
    lim.felt("praise", who="Josh")       # something happened (EVENTS)
    lim.company(["Josh"])                # who she knows is with her now (every few seconds)
    lim.tick()                           # drift, every few seconds
    lim.happy()                          # 0..1; 0.5 is content
    lim.how()                            # ("good", "learned"): how she is, and mostly why

    python3 limbic.py                    # how she is now, and today's ups and downs

The hormones (rest: where each drifts back to; tau: how fast, seconds):

  oxytocin   company and closeness. Up when someone she knows turns up (more
             if she's fond of them, BONDS, and has missed them), talks to her
             or praises her; slowly down when she's alone. Its rest is below
             content: a long time alone is mildly lonely.
  dopamine   curiosity and reward. Up when she learns something, meets
             someone new, goes somewhere new, is thanked or praised. Rest
             below content too: a long quiet day is mildly boring.
  serotonin  contentment: a slow average of how happy she's been.
  cortisol   stress. Up when she can't understand someone, or a stranger
             turns up while she's on her own, or a loud bang. Mild by
             design: capped at CORTISOL_MAX, and gone again in half an hour.
  adrenaline startle: a loud bang, a new face. Gone in a minute.
  melatonin  sleepiness: night, and being left alone.

Nothing pushes harder the more it happens: each push shrinks as the level
nears the top (satiation), and the same thing over and over counts for less
each time within HABITUATE_S (habituation). So she wants things to stay
comfortable, not as high as they'll go.

Saved to limbic.json (beside memory.db) every minute, so she wakes up
feeling as she did; every push is logged in memory.db (feelings), with a
reading of everything a minute (chemistry), to see what makes her unhappy.
"""
import json
import math
import re
import os
import sys
import time

DIR = os.path.expanduser("~/.local/share/robotpattinson")
STATE = os.path.join(DIR, "limbic.json")

HORMONES = {        # rest, tau (s), content (where she's neither up nor down)
    "oxytocin":   (0.15, 2 * 3600, 0.30),
    "dopamine":   (0.25, 2 * 3600, 0.35),
    "serotonin":  (0.50, 12 * 3600, 0.50),
    "cortisol":   (0.05, 30 * 60, 0.05),
    "adrenaline": (0.00, 60, 0.00),
    "melatonin":  (0.10, 30 * 60, 0.10),
}
# How much each counts towards happy() (cortisol, against). Below content
# counts for LOW_COUNTS as much as above: lonely or bored is mild, never sad.
WEIGHTS = {"oxytocin": 0.7, "dopamine": 0.6, "serotonin": 0.5, "cortisol": -0.9}
LOW_COUNTS = 0.5
CORTISOL_MAX = 0.6      # never more stressed than this
HABITUATE_S = 3600      # the same thing again within this counts for half as much each time
GENTLE = {"thanks": 0.8, "polite": 0.8}     # (except these: manners always please her)
SAVE_S = 60             # limbic.json, and a chemistry reading

# What happens to her: {hormone: push}. A push up shrinks as the level nears
# 1 (x (1 - level)); a push down shrinks as it nears 0.
EVENTS = {
    "arrived":       {"oxytocin": 0.10, "dopamine": 0.03},      # someone she knows turns up (x fondness, x missed)
    "company":       {"oxytocin": 0.004},                       # each minute someone she knows is with her
    "spoken_to":     {"oxytocin": 0.03, "dopamine": 0.02},      # a command or a question, by voice or typed
    "praise":        {"oxytocin": 0.08, "dopamine": 0.12},      # "good girl", "well done"
    "thanks":        {"oxytocin": 0.06, "dopamine": 0.06},      # she helped
    "learned":       {"dopamine": 0.10},                        # a new answer (Claude, Wikipedia)
    "remembered":    {"dopamine": 0.03},                        # she answered from memory
    # A story she's reading (x how strongly, 0..1, x server.STORY_SCALE): feeling with the characters,
    # the way a reader does; gentler than the real thing, and gone again as the hormones drift back.
    "story_tension": {"cortisol": 0.14, "adrenaline": 0.35, "dopamine": 0.03},
    "story_warmth":  {"oxytocin": 0.12},
    "story_sadness": {"serotonin": -0.10, "oxytocin": -0.04},
    "story_humour":  {"dopamine": 0.08, "serotonin": 0.04},
    "story_wonder":  {"dopamine": 0.12},
    "story_relief":  {"cortisol": -0.20, "serotonin": 0.04},
    "recognised":    {"dopamine": 0.15},                        # something she read connects to what she knows
                                                                # (x how much: more if it adds to it, and if
                                                                # someone she knows told her; server.py)
    "stranger":      {"dopamine": 0.05, "adrenaline": 0.3},     # a face she doesn't know
    "stranger_alone": {"cortisol": 0.10},                       # ... and nobody she knows about
    "new_place":     {"dopamine": 0.10, "cortisol": 0.03},      # the wifi says she's somewhere else
    "loud":          {"adrenaline": 0.4, "cortisol": 0.02},     # a sudden bang
    "not_understood": {"cortisol": 0.03, "dopamine": 0.01},     # frustrating, and a little interesting
    "petted":        {"oxytocin": 0.10, "cortisol": -0.10},     # (the touch strip, one day)
    "polite":        {"oxytocin": 0.04},                        # please, sorry, por favor (POLITE)
    "liked_by":      {"oxytocin": 0.05, "dopamine": 0.02},      # told someone likes her ("Alex thinks you're wonderful")
}

# Thanks, anywhere in what's said to her, in English or Spanish: she's
# always pleased (EVENTS "thanks").
THANKS = re.compile(r"\b(?:thanks|thank you|thank u|thx|ta|cheers|gracias|muchas gracias|mil gracias)\b", re.I)
# Other good manners: a little warmth, and a little fondness ("Not now" does
# the job; "Sorry, Patti, I can't talk right now" is nicer).
POLITE = re.compile(r"\b(?:please|pls|sorry|apologies|excuse me|pardon|"
                    r"(?:would|do) you mind|if you (?:don'?t|do not) mind|could you|would you|"
                    r"kind of you|appreciate it|my dear|sweetheart|love|"
                    r"por favor|perd[o\u00f3]n|lo siento|disculpa|con permiso|mija|mi amor|cari\u00f1o|corazon|coraz\u00f3n)\b",
                    re.I)

# Fondness, per person: up a little with each visit and each thing they say
# to her, very slowly down when she doesn't see them.
BOND_UP = {"arrived": 0.02, "spoken_to": 0.005, "praise": 0.01, "thanks": 0.01, "petted": 0.01, "polite": 0.01,
           "liked_by": 0.03}
BOND_TAU = 60 * 86400
BOND_START = 0.3
# Her dad: Josh built her. Her fondness for him starts at PARENT_BOND and
# never fades below it; with him there, stress fades COMFORT times as fast
# (a safe base). What he tells her, she trusts first (server.py, think_over).
PARENTS = ("Josh",)
PARENT_BOND = 0.7
COMFORT = 4.0
MISSED_S = 8 * 3600     # gone this long: she's missed them as much as she will

NIGHT = (23, 7)         # melatonin rises between these hours


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


class Limbic:
    def __init__(self, memory=None, path=STATE):
        self.memory, self.path = memory, path
        self.level = {h: v[2] for h, v in HORMONES.items()}
        self.bonds = {}         # name -> 0..1
        self.last_seen = {}     # name -> time
        self.recent = {}        # cause -> [times], for habituation
        self.at = time.time()
        self.saved = 0.0
        self.with_her = set()
        self._load()

    # ---------- what happened ----------
    def felt(self, cause, who=None, scale=1.0, why=None):
        """Something happened (EVENTS[cause]); scale: more or less of it."""
        push = EVENTS.get(cause)
        if not push:
            return {}
        now = time.time()
        times = [t for t in self.recent.get(cause, []) if now - t < HABITUATE_S]
        self.recent[cause] = times + [now]
        scale *= GENTLE.get(cause, 0.5) ** len(times) if cause != "company" else 1.0
        moved = {}
        for h, p in push.items():
            x = self.level[h]
            d = p * scale * ((1 - x) if p > 0 else x)
            top = CORTISOL_MAX if h == "cortisol" else 1.0
            self.level[h] = _clamp(x + d, 0.0, top)
            moved[h] = round(self.level[h] - x, 4)
        if who and cause in BOND_UP:
            b = self.bonds.get(who, self._start(who))
            self.bonds[who] = _clamp(b + BOND_UP[cause] * (1 - b))
        if self.memory and cause != "company":
            self.memory.feeling(cause, who, moved, why)
        return moved

    def merge(self, base, bonds, last_seen):
        """Another machine's fondness for people (bonds, from where both started: base) folded into hers,
        three-way, like git: what changed there is added to what she has here. Last seen: the later."""
        for who, b in (bonds or {}).items():
            here = self.bonds.get(who, self._start(who))
            self.bonds[who] = _clamp(here + float(b) - float((base or {}).get(who, self._start(who))))
            if who in PARENTS:
                self.bonds[who] = max(self.bonds[who], PARENT_BOND)
        for who, t in (last_seen or {}).items():
            self.last_seen[who] = max(self.last_seen.get(who, 0), float(t))
        self._save()

    def _start(self, who):
        return PARENT_BOND if who in PARENTS else BOND_START

    def manners(self, text, who=None):
        """Thanked (THANKS)? Always pleased. Otherwise polite (POLITE)? A
        little warmer towards them. True if either."""
        if not text:
            return False
        if THANKS.search(text):
            self.felt("thanks", who, why=text[:80])
            return True
        if POLITE.search(text):
            self.felt("polite", who, why=text[:80])
            return True
        return False

    def company(self, names):
        """Who she knows is with her now: anyone new to the room counts as
        arriving (more for someone she's fond of and hasn't seen for a
        while); while anyone is, a little warmth every minute."""
        now = time.time()
        names = set(names)
        for n in names - self.with_her:
            gone = now - self.last_seen.get(n, 0)
            if gone > 600:
                missed = min(1.0, gone / MISSED_S)
                fond = self.bonds.get(n, self._start(n))
                why = ("first time" if gone > 365 * 86400 else "gone %d min" % (gone // 60) if gone < 7200
                       else "gone %.0f h" % (gone / 3600))
                self.felt("arrived", n, scale=0.5 + fond * (0.5 + missed), why=why)
        for n in names:
            self.last_seen[n] = now
        self.with_her = names
        if names and now - self.recent.get("company", [0])[-1] >= 60:
            self.felt("company")

    def forget(self, who):
        for d in (self.bonds, self.last_seen):
            d.pop(who, None)
        self.with_her.discard(who)

    def rename(self, old, new):
        for d in (self.bonds, self.last_seen):
            if old in d:
                d[new] = d.pop(old)

    # ---------- drift ----------
    def tick(self):
        now = time.time()
        dt = max(0.0, now - self.at)
        self.at = now
        hour = time.localtime(now).tm_hour
        night = hour >= NIGHT[0] or hour < NIGHT[1]
        for h, (rest, tau, _) in HORMONES.items():
            if night and h in ("oxytocin", "dopamine"):
                continue                        # (asleep: not lonely or bored in the night)
            if h == "serotonin":
                rest = self._mood()             # (follows how happy she's been)
            elif h == "melatonin":
                rest = (0.7 if night else 0.1) + (0.15 if not self.with_her else 0.0)
            if h == "cortisol" and self.with_her & set(PARENTS):
                tau /= COMFORT                  # (her dad's here)
            k = 1 - math.exp(-dt / tau)
            self.level[h] += (rest - self.level[h]) * k
        for n in PARENTS:
            self.bonds[n] = max(self.bonds.get(n, PARENT_BOND), PARENT_BOND)
        for n, b in self.bonds.items():
            if n not in self.with_her:
                floor = PARENT_BOND if n in PARENTS else BOND_START * 0.5
                self.bonds[n] = max(floor, b + (floor - b) * (1 - math.exp(-dt / BOND_TAU)))
        if now - self.saved >= SAVE_S:
            self.saved = now
            self._save()
            if self.memory:
                self.memory.chemistry(dict(self.level, happy=self.happy()))

    # ---------- how she is ----------
    def _pull(self, h):
        """How much hormone h moves her from content (+ happier)."""
        d = self.level[h] - HORMONES[h][2]
        return WEIGHTS[h] * d * (LOW_COUNTS if d < 0 and h != "cortisol" else 1.0)

    def _mood(self):
        """happy() without serotonin (what serotonin follows)."""
        return _clamp(0.5 + sum(self._pull(h) for h in WEIGHTS if h != "serotonin"))

    def happy(self):
        """0..1: 0.5 content, more is happier, less is lonely, bored or stressed."""
        return _clamp(0.5 + sum(self._pull(h) for h in WEIGHTS))

    def how(self):
        """(how: "great", "good", "fine", "meh"; why: what's pulling her most
        that way, or None: "oxytocin", "dopamine" (up), "oxytocin_low",
        "dopamine_low", "cortisol" (down); replies.py has "why_" + it)."""
        h = self.happy()
        pulls = {k: self._pull(k) for k in WEIGHTS if k != "serotonin"}
        if h >= 0.5:
            why = max(pulls, key=pulls.get)
            why = why if pulls[why] > 0.03 and why != "cortisol" else None
        else:
            why = min(pulls, key=pulls.get)
            why = (why if why == "cortisol" else why + "_low") if pulls[why] < -0.03 else None
        how = "great" if h >= 0.7 else "good" if h >= 0.55 else "fine" if h >= 0.42 else "meh"
        return how, why

    def face(self):
        """An eye expression for now (eyes.MOODS): never sad (her page and
        unhappy_check say so instead)."""
        if self.level["adrenaline"] > 0.3:
            return "surprised"
        if self.level["cortisol"] > 0.3:
            return "suspicious"
        return "happy" if self.happy() >= 0.68 else "normal"

    def snapshot(self):
        return {"levels": {k: round(v, 3) for k, v in self.level.items()}, "happy": round(self.happy(), 3),
                "how": self.how(), "bonds": {k: round(v, 3) for k, v in self.bonds.items()},
                "with": sorted(self.with_her)}

    # ---------- keeping ----------
    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".tmp", "w") as f:
                json.dump({"at": self.at, "level": self.level, "bonds": self.bonds, "last_seen": self.last_seen}, f)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            print("limbic: can't save (%s)" % e, file=sys.stderr)

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.level.update({k: float(v) for k, v in d.get("level", {}).items() if k in HORMONES})
        self.bonds = {k: float(v) for k, v in d.get("bonds", {}).items()}
        self.last_seen = {k: float(v) for k, v in d.get("last_seen", {}).items()}
        self.at = float(d.get("at", time.time()))
        self.tick()         # (catch up on the time she was off)


if __name__ == "__main__":
    import memory
    lim = Limbic()
    s = lim.snapshot()
    print("happy %.2f (%s, %s)" % (s["happy"], *s["how"]))
    for k, v in s["levels"].items():
        print("  %-10s %.3f  %s" % (k, v, "#" * int(v * 40)))
    print("fond of:", ", ".join("%s %.2f" % kv for kv in sorted(s["bonds"].items(), key=lambda kv: -kv[1])) or "nobody yet")
    m = memory.Memory()
    for at, cause, who, moved, why in m.feelings(time.time() - 86400):
        print(time.strftime("%H:%M", time.localtime(at)), cause, who or "", moved, why or "")
