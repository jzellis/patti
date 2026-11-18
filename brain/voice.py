"""Voice commands for the brain, through Vosk (offline speech recognition).

Vosk listens all the time but may only hear the phrases in PHRASES (plus
"[unk]" for anything else), which keeps it light on the Pi and hard to fool:
it can't mistake chatter for a command that isn't on the list. Every command
starts with her name, except "stop", which works on its own and is acted on
mid-word (from Vosk's partial result) so it's as quick as possible. A misheard
name still counts, as long as something came before the command.

Needs the vosk package and its small English model:

    pip install --user --break-system-packages vosk
    (model) ~/.local/share/robotpattinson/vosk/vosk-model-small-en-us-0.15
            from https://alphacephei.com/vosk/models, or set RP_VOSK

It reads the mic through arecord from the shared "rpmic" ALSA device
(systemd/50-rpmic.conf), so recordings can use the mic at the same time.

Everything it hears is logged ("voice: ..."), with Vosk's confidence, so the
phrases and MIN_CONF can be tuned from journalctl -u rp-brain.
"""

import collections
import difflib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time

import numpy as np

import memory
import users

MODEL = os.path.expanduser(os.environ.get(
    "RP_VOSK", "~/.local/share/robotpattinson/vosk/vosk-model-small-en-us-0.15"))
RATE = 16000
CHUNK = RATE // 10 * 2          # 0.1 s of 16-bit mono
RETRY_S = 5                     # mic gone (unplugged)? try again this often
# Heard while she's talking (her ears are otherwise shut then), these stop her.
BARGE = ("stop", "patti stop", "patty stop", "stop talking", "shush", "hush", "be quiet", "quiet",
         "shut up", "that's enough", "enough")
BARGE_CONF = 0.7
BARGE_WINDOW = 12               # 0.1 s chunks: how often it decides
MIN_CONF = 0.6                  # mean word confidence to act on a command
# Less sure than this, and not a command: a noise (beard scratching came out
# as "hi" at 0.23-0.27; real speech 0.5 and up), so it isn't shown as heard.
HEARD_MIN_CONF = 0.4
TALK_DB = 10                    # someone's talking (the chest's voice screen): this far over the room,
TALK_MIN_S = 0.4                # for this long (a cough or a scratch is shorter)
STOP_CONF = 0.4                 # stopping is always safe, so take it on less
# A sound counts as loud (wakes her up) at LOUD_DB above the room's background
# (the quietest LOUD_FLOOR_PCT% of the last LOUD_WINDOW chunks), and never
# below LOUD_MIN_DBFS. At home on 2026-10-22 the background was about -36 dBFS.
LOUD_DB = 15
LOUD_MIN_DBFS = -40
LOUD_WINDOW = 300               # 30 s of 0.1 s chunks
LOUD_FLOOR_PCT = 20
# What was heard just before each command (not her own voice), so the brain
# can tell who said it (voices.py): the last RECENT_S, as `said`.
RECENT_S = 5.0
# A chatty room (the telly, people talking among themselves): this many
# things heard that weren't to her in CHATTY_S, and only her name will do.
CHATTY_N = 4
CHATTY_S = 60
# "Patti, <anything>": her name, then words she doesn't know. She keeps up to
# FREE_S of what was said, for friday to write down (server.py, spoken_free).
FREE_S = 15.0
FREE_PAD_S = 0.6

# Her name, as Vosk may spell it (all three are in its vocabulary). She's
# Patti, after Patti Smith (Pattinson until 2026-10-31).
NAMES = list(users.HER_NAMES)
GREETS = ("hey", "oi", "ok", "okay")     # before her name: "hey Patti, ..."


def number_words(n):
    """21 -> "twenty one", the way Vosk writes it."""
    ones = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
            "sixteen seventeen eighteen nineteen").split()
    tens = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}
    return ones[n] if n < 20 else tens[n // 10] + ("" if n % 10 == 0 else " " + ones[n % 10])


def timer_phrases():
    """"set a timer for ten minutes", "timer for ten minutes", "ten minute
    timer" (and seconds, hours, halves) -> ("timer", seconds)."""
    lengths = {}                    # (as said after "for", as said before "timer") -> seconds
    for m in range(1, 61):
        lengths[("a minute" if m == 1 else number_words(m) + " minutes", number_words(m) + " minute")] = m * 60
    lengths[("one minute", "one minute")] = 60
    lengths[("ninety minutes", "ninety minute")] = 90 * 60
    for sec in (10, 15, 20, 30, 40, 45, 50):
        lengths[(number_words(sec) + " seconds", number_words(sec) + " second")] = sec
    for h in (1, 2, 3):
        lengths[("an hour" if h == 1 else number_words(h) + " hours", number_words(h) + " hour")] = h * 3600
    lengths[("one hour", "one hour")] = 3600
    lengths[("half an hour", "half hour")] = 1800
    lengths[("an hour and a half", "hour and a half")] = 5400
    lengths[("a minute and a half", "minute and a half")] = 90
    out = {}
    for (after, before), sec in lengths.items():
        out["set a timer for " + after] = ("timer", sec)
        out["timer for " + after] = ("timer", sec)
        out[before + " timer"] = ("timer", sec)
    return out


# phrase (after her name) -> (action, argument)
COMMANDS = {
    "stop": ("stop", None),
    "look left": ("look", (-30, 0)),
    "look right": ("look", (30, 0)),
    "look up": ("look", (0, 20)),
    "look down": ("look", (0, -20)),
    "look at me": ("seek", None),
    "find me": ("seek", None),
    "follow me": ("follow", None),
    "stop following": ("unfollow", None),
    "keep an eye out": ("watch", True),
    "start watching": ("watch", True),
    "look around": ("watch", True),
    "stop watching": ("watch", False),
    "stop looking around": ("watch", False),
    # (Without these, "curiosity" came out as the nearest phrase it knew:
    # "three hours thirty".)
    "turn on curiosity": ("watch", True),
    "curiosity on": ("watch", True),
    "be curious": ("watch", True),
    "turn off curiosity": ("watch", False),
    "curiosity off": ("watch", False),
    "stop being curious": ("watch", False),
    "look ahead": ("centre", None),
    "look forward": ("centre", None),
    "centre": ("centre", None),
    "relax": ("relax", None),
    "reset your eyes": ("eyes", None),
    "reset eyes": ("eyes", None),
    "go to sleep": ("sleep", None),
    # "Patti, do Johnny Five": the eye moods (eyes.MOODS).
    **{"do " + said: ("mood", mood) for said, mood in (
        ("normal", "normal"), ("happy", "happy"), ("angry", "angry"), ("sad", "sad"),
        ("surprised", "surprised"), ("suspicious", "suspicious"), ("love", "love"),
        ("dizzy", "dizzy"),
        ("johnny five", "johnny"), ("johnny", "johnny"), ("number five", "johnny"),
        ("hal", "hal"),
        ("arnie", "arnie"), ("terminator", "arnie"), ("the terminator", "arnie"),
        ("wall e", "walle"), ("walle", "walle"), ("wally", "walle"),
        # (Vosk has no "uwu")
        ("oo woo", "uwu"), ("you woo", "uwu"), ("anime", "uwu"),
        # her CPU and RAM as VU meters (Vosk hears "VU" as letters or "view")
        ("meters", "vu"), ("the meters", "vu"), ("your meters", "vu"), ("v u meters", "vu"),
        ("view meters", "vu"), ("vu meters", "vu"), ("stats", "vu"), ("your stats", "vu"))},
    "start recording": ("record", True),
    "stop recording": ("record", False),
    "table mode": ("table", True),
    # the kitchen timer (timers.py)
    **timer_phrases(),
    **{said: ("timer_cancel", None) for said in (
        "cancel the timer", "cancel timer", "stop the timer", "stop timer")},
    **{said: ("timer_left", None) for said in (
        "how long is left", "how much time is left", "how long left", "how long is left on the timer",
        "how much time is left on the timer", "how long is left on my timer", "how long on the timer")},
    # notes, into Joplin (notes.py, chest.py)
    "take a note": ("note", None), "make a note": ("note", None), "new note": ("note", None),
    "note this down": ("note", None), "write this down": ("note", None),
    "stop listening": ("mute", None),
    # the news (news.py): a few headlines
    **{said: ("news", None) for said in (
        "what's in the news", "what is in the news", "what's the news", "what is the news", "any news",
        "tell me the news", "what's happening in the world", "what's going on in the world", "the news",
        "read me the news", "what are the headlines", "headlines")},
    # quiet, please: she starts nothing for a while (drives.py), then carries on
    **{said: ("dnd", None) for said in (
        "leave me alone", "leave me alone for a bit", "leave me alone for a while", "give me some peace",
        "not now", "quiet time", "do not disturb", "i'm busy")},
    **{said: ("dnd_off", None) for said in (
        "you can talk to me again", "you can talk again", "i'm free now", "i'm not busy now")},
    # a question (server.py, spoken_question): then she listens, and friday writes it down. Here,
    # not with the chat, so the end is enough when her name's misheard ("... i've a question").
    **{said: ("question", None) for said in (
        "i've got a question", "i have a question", "i've a question", "i've got a quick question",
        "i have a quick question", "got a question", "have a question", "a question", "quick question",
        "can i ask you something", "can i ask you a question", "i want to ask you something",
        "question for you", "i've got a question for you", "i have a question for you")},
    # what the Sense HAT shows when she's idle (hat.py)
    **{said: ("hatidle", "life") for said in (
        "game of life", "the game of life", "play the game of life", "play game of life", "do the game of life")},
    **{said: ("hatidle", "blinkenlights") for said in (
        "blinking lights", "do blinking lights", "do the blinking lights")},
    **{said: ("hatidle", "rain") for said in (
        "matrix rain", "the matrix", "do the matrix", "do matrix rain")},
    **{said: ("hatidle", "lava") for said in (
        "lava lamp", "do the lava lamp", "do lava lamp", "be a lava lamp")},
    **{said: ("hatidle", "shuffle") for said in (
        "shuffle the lights", "shuffle lights", "mix it up", "surprise me")},
    # her own voice (speech.py)
    "speak up": ("voicevol", 1), "talk louder": ("voicevol", 1), "speak louder": ("voicevol", 1),
    "speak quietly": ("voicevol", -1), "talk quieter": ("voicevol", -1), "speak quieter": ("voicevol", -1),
    "speak softly": ("voicevol", -1),
    **{"use your %s voice" % said: ("voiceengine", mode) for said, mode in (
        ("best", "elevenlabs"), ("posh", "elevenlabs"), ("free", "kokoro"), ("cheap", "kokoro"),
        ("quick", "piper"), ("usual", "auto"), ("normal", "auto"))},
    # music (music.py)
    **{said: ("music", what) for said, what in (
        ("pause", "pause"), ("pause the music", "pause"), ("stop the music", "pause"),
        ("play", "play"), ("play the music", "play"), ("resume", "play"),
        ("next song", "next"), ("skip", "next"), ("skip this song", "next"), ("skip this", "next"),
        ("previous song", "previous"), ("last song", "previous"), ("go back", "previous"),
        ("what song is this", "what"), ("what is this song", "what"), ("what is playing", "what"),
        ("who is this", "what"), ("what is this", "what"),
        ("turn it up", "up"), ("louder", "up"), ("turn it down", "down"), ("quieter", "down"))},
}


def known_names():
    """Who she could be asked about, {name as said: who}: everyone enrolled
    (faces or voices), the cats, and the names and nicknames in logins'
    profiles (users.py: "joshua" -> "Josh"). Read once, at start: someone
    new is askable after a restart."""
    base = os.path.expanduser("~/.local/share/robotpattinson")
    names = {n for d in ("faces", "voices") if os.path.isdir(os.path.join(base, d))
             for n in os.listdir(os.path.join(base, d)) if os.path.isdir(os.path.join(base, d, n))}
    out = {n.lower(): n for n in names | set(memory.CATS)}
    for said, who in users.aliases().items():
        out.setdefault(said, who)
    return out


def lastseen_phrases():
    """"when did you last see josh", "where's pepper" -> ("lastseen", "Josh");
    "when did you last see me" -> ("lastseen", None)."""
    out = {}
    for n, name in known_names().items():
        for p in ("when did you last see %s", "when did you see %s last", "when's the last time you saw %s",
                  "when was the last time you saw %s", "have you seen %s", "have you seen %s today",
                  "where's %s", "where is %s", "when did you last hear %s"):
            out[p % n] = ("lastseen", name)
    for p in ("when did you last see me", "when did you see me last", "when's the last time you saw me",
              "when was the last time you saw me", "have you seen me today", "when did you last hear me"):
        out[p] = ("lastseen", None)         # (whoever's asking)
    return out


# Said to her, her name before or after: "hello patti", "patti, thank you",
# "what time is it, patti".
SOCIAL = {
    "hello": ("hello", None), "hi": ("hello", None), "hey": ("hello", None),
    "hola": ("hello", "hola"), "hola chica": ("hello", "hola"),
    "good morning": ("hello", "morning"), "good evening": ("hello", "evening"),
    "good night": ("goodnight", None), "night night": ("goodnight", None),
    "thank you": ("thanks", None), "thanks": ("thanks", None), "thank you very much": ("thanks", None),
    "cheers": ("thanks", None), "gracias": ("thanks", "es"), "muchas gracias": ("thanks", "es"),
    "good boy": ("praise", None), "good robot": ("praise", None), "well done": ("praise", None),
    "how are you": ("how", None),
    **{said: ("weather", None) for said in (
        "what's the weather like", "what is the weather like", "what's the weather", "what is the weather",
        "how's the weather", "how is the weather", "what's it like outside", "what is it like outside",
        "what's the forecast", "what is the forecast")},
    **{said: ("time", None) for said in (
        "what's the time", "what is the time", "what time is it")},
    # learning someone, or forgetting them (meet.py)
    **{said: ("meet", None) for said in (
        "remember me", "learn my face", "learn my voice", "get to know me", "remember my face",
        "remember my voice")},
    **{said: ("forget", None) for said in (
        "forget me", "forget my face", "forget my voice", "forget who i am")},
    # who she thinks is talking: voice and face (server.py, whoami)
    **{said: ("whoami", None) for said in (
        "who am i", "who do you think i am", "do you know who i am", "do you know me",
        "what's my name", "what is my name")},
    # someone she doesn't know (strangers.py, meet.py)
    **{said: ("introduce", None) for said in (
        "this is my friend", "that's my friend", "meet my friend", "say hello to my friend", "this is my",
        "i'd like you to meet someone", "i want you to meet someone", "meet someone", "this is a friend",
        "i want you to meet my friend", "i'd like you to meet my friend")},
    **{said: ("whosthat", None) for said in (
        "who's that", "who is that", "do you know who that is", "do you know this person",
        "who's this person", "who is this person")},
    # her memory (memory.py)
    **lastseen_phrases(),
    # where she is, by wifi (places.py)
    **{said: ("where", None) for said in (
        "where are we", "where are you", "where am i", "where do you think we are", "do you know where we are")},
    # storm watch (barometer.py)
    **{said: ("pressure", None) for said in (
        "what's the pressure", "what is the pressure", "what's the air pressure", "is there a storm coming",
        "is a storm coming", "is there a storm on the way")},
}
# Everyday words that aren't commands. Vosk only hears the words it's given,
# so without these, speech that isn't a command gets squeezed into the
# nearest one ("hello" came out as "do johnny"); with them it has somewhere
# else to go.
DECOYS = ("""hello hi hey yes no yeah okay right well oh so and but the a an is it
    this that what where when why how who you your i me my we they he she him her
    there here now then today tonight time good bad nice love like want need know
    think get got going come see look tell said say talk go make take give put
    thing things one two three some all just really very not do did done have had
    can could would should will was were be been are am of in on at to for with
    from about up down out off over back again still more much tea cat cats dog
    dinner lunch telly tv phone music song""").split()
# Said on their own: only stopping, which is always safe.
BARE = {"stop": ("stop", None), "halt": ("stop", None), "freeze": ("stop", None)}


def phrases():
    out = {}
    for name in NAMES:
        for p, act in COMMANDS.items():
            out["%s %s" % (name, p)] = act
        for p, act in SOCIAL.items():
            out["%s %s" % (name, p)] = act
            out["%s %s" % (p, name)] = act
    out.update(BARE)
    return out


def typed(text):
    """A typed line as a command, the way she'd take it said: "What's the
    weather like?" or "Patti, set a timer for 10 minutes" -> (the phrase,
    action). Her name is optional; numbers can be digits. (phrase, None) if
    it isn't one of hers."""
    t = text.lower().replace("\u2019", "'")
    t = re.sub(r"\d+", lambda m: number_words(int(m.group())) if int(m.group()) < 100 else m.group(), t)
    words = re.sub(r"[^a-z0-9' ]+", " ", t).split()
    if words and words[0] in NAMES:
        words = words[1:]
    if words and words[-1] in NAMES:
        words = words[:-1]
    t = " ".join(words)
    table = {**COMMANDS, **SOCIAL, **BARE}
    flat = {p.replace("'", ""): p for p in table}
    find = lambda t: t if t in table else flat.get(t.replace("'", ""), t)   # (typed without the apostrophe)
    found = find(t)
    if found not in table:      # "could you look left please": the command without the manners
        bare = re.sub(r"\s*\b(?:please|pls|thanks|thank you|cheers)$", "", t)
        bare = re.sub(r"^(?:please|could you|can you|would you|will you|would you mind)\s+", "", bare).strip()
        bare = re.sub(r"\s*\b(?:patti|patty|pattie)$", "", re.sub(r"^(?:patti|patty|pattie)\s+", "", bare)).strip()
        if bare and find(bare) in table:
            found = find(bare)
    return found, table.get(found)


def near(phrase, n=3):
    """The commands closest to a phrase that isn't one."""
    return difflib.get_close_matches(phrase, [*COMMANDS, *SOCIAL, *BARE], n=n, cutoff=0.6)


class Voice:
    """Listens in a thread once start(act) is called; calls act(action, arg,
    text) for each command. act runs on that thread, so it must hand the work
    over safely."""

    def __init__(self, device):
        self.device = device
        self.act = None
        self.thread = None
        self.ok = False
        self.error = ""
        self.heard = ""             # last thing recognised (command or not)
        self.heard_at = 0.0
        self.log = collections.deque(maxlen=12)   # (time.time(), text, action or None), newest last
        self.partial = ""           # what Vosk thinks it's hearing right now ("[unk]" = speech it can't place)
        self.speech_at = 0.0        # last time it thought someone was speaking
        self.level_db = -90.0       # loudness of the last 0.1 s, dBFS...
        self.floor_db = -60.0       # ...and of the room's background
        self.proc = None
        self.stopping = False
        self.on_loud = None             # called (from this thread) on a loud sound, at most 1/s
        self.deaf = None                # () -> True while she's talking: don't listen to herself
        self.barge = None               # (word) -> True if it stopped her: "stop" while she talks (BARGE)
        self.barge_rec = None
        self.barge_n = 0
        self.muted = False              # listening turned off (the chest's mic icon): ignore the mic entirely
        self.tap = None                 # (data) -> None: the mic goes here instead (a note, notes.py), not to Vosk
        self.recent = collections.deque(maxlen=int(RECENT_S * 10))     # 0.1 s chunks
        self.longer = collections.deque(maxlen=int(FREE_S * 10))       # (the same, further back)
        self.chatter = collections.deque(maxlen=20)                     # when she heard things not to her
        self.said = b""                 # the last RECENT_S before the latest command (16-bit mono, RATE)
        self.speaker = None             # (heard_at, name or None): who said it, set by the brain (voices.py)
        self.levels = collections.deque(maxlen=LOUD_WINDOW)
        self.loud_at = 0.0
        self.table = phrases()
        try:
            import vosk
        except ImportError:
            self.error = "vosk not installed"
            print("voice: off (%s)" % self.error, file=sys.stderr)
            return
        if not os.path.isdir(MODEL):
            self.error = "no Vosk model at %s" % MODEL
            print("voice: off (%s)" % self.error, file=sys.stderr)
            return
        vosk.SetLogLevel(-1)
        self.vosk = vosk
        self.model = vosk.Model(MODEL)

    def start(self, act):
        if not hasattr(self, "model"):
            return
        self.act = act
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _recognizer(self):
        r = self.vosk.KaldiRecognizer(self.model, RATE, json.dumps(list(self.table) + DECOYS + ["[unk]"]))
        r.SetWords(True)
        return r

    def _run(self):
        while not self.stopping:
            try:
                self._listen()
            except Exception as e:      # keep the brain up whatever happens here
                self.error = str(e)
                print("voice: %s" % e, file=sys.stderr)
            self.ok = False
            if not self.stopping:
                time.sleep(RETRY_S)

    def _listen(self):
        self.proc = subprocess.Popen(
            ["arecord", "-q", "-D", self.device, "-f", "S16_LE", "-r", str(RATE),
             "-c", "1", "-t", "raw"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rec = self._recognizer()
        stopped_on_partial = False
        talk_since = None               # when the current bit of talk got loud (TALK_MIN_S)
        first = True
        deafened = False
        while True:
            data = self.proc.stdout.read(CHUNK)
            if not data:
                err = self.proc.stderr.read().decode(errors="replace").strip()
                self.proc.wait()
                raise OSError("mic: " + (err.splitlines()[-1] if err else "arecord ended"))
            if first:
                self.ok, self.error, first = True, "", False
                print("voice: listening on %s" % self.device, file=sys.stderr)
            if self.muted or (self.deaf and self.deaf()):
                # Her own voice, or not listening: drop it, and whatever was half heard before.
                if self.muted:
                    self.level_db = -90.0
                if not deafened:
                    rec.Reset()
                    self.partial, deafened = "", True
                    self._barge_reset()
                if not self.muted and self.barge:
                    self._barge_feed(data)
                continue
            deafened = False
            self._loudness(data)
            self.recent.append(data)
            self.longer.append(data)
            tap = self.tap
            if tap:
                tap(data)
                if self.partial:
                    rec.Reset()
                    self.partial = ""
                continue
            if rec.AcceptWaveform(data):
                self._final(json.loads(rec.Result()), stopped_on_partial)
                stopped_on_partial = False
            else:
                partial = json.loads(rec.PartialResult()).get("partial", "")
                self.partial = partial
                # Talk, not just a word-shaped noise: louder than the room,
                # and going on for a while (a cough is loud, but over quickly).
                now = time.monotonic()
                if not partial:
                    talk_since = None
                elif self.level_db > self.floor_db + TALK_DB:
                    talk_since = talk_since or now
                    if now - talk_since >= TALK_MIN_S:
                        self.speech_at = now
                # "stop" alone: don't wait for the end of the utterance.
                if not stopped_on_partial and partial.split()[:1] == ["stop"]:
                    stopped_on_partial = True
                    self.act("stop", None, "stop")

    def _loudness(self, data):
        x = np.frombuffer(data, np.int16).astype(np.float32)
        db = 20 * math.log10(math.sqrt(float((x * x).mean())) / 32768 + 1e-9)
        self.levels.append(db)
        self.level_db = db
        if len(self.levels) < 20:
            return
        floor = self.floor_db = float(np.percentile(self.levels, LOUD_FLOOR_PCT))
        if not self.on_loud:
            return
        now = time.monotonic()
        if db > max(floor + LOUD_DB, LOUD_MIN_DBFS) and now - self.loud_at > 1:
            self.loud_at = now
            self.on_loud(db, floor)

    # ---------- "stop" while she talks ----------
    def _barge_reset(self):
        if self.barge_rec is None and self.barge:
            self.barge_rec = self.vosk.KaldiRecognizer(self.model, RATE, json.dumps(list(BARGE) + ["[unk]"]))
            self.barge_rec.SetWords(True)
        elif self.barge_rec is not None:
            self.barge_rec.Reset()
        self.barge_n = 0

    def _barge_feed(self, data):
        """While she talks her ears are shut (she'd hear herself), but this
        listens for BARGE alone, deciding every BARGE_WINDOW chunks (her
        voice never pauses long enough for Vosk to decide by itself): a
        window that's a stop word and nothing else, sure enough (BARGE_CONF),
        and not a word she's saying herself (self.barge checks), stops her.
        Her own speech mostly comes out as [unk] to a listener that knows only
        these words. Anything it makes out is logged, for tuning."""
        r = self.barge_rec
        if r is None:
            return
        self.barge_n += 1
        if not r.AcceptWaveform(data) and self.barge_n < BARGE_WINDOW:
            return
        res = json.loads(r.FinalResult())
        r.Reset()
        self.barge_n = 0
        text = res.get("text", "").replace("[unk]", "").strip()
        if not text:
            return
        words = [w for w in res.get("result", []) if w.get("word") != "[unk]"]
        conf = sum(w.get("conf", 0) for w in words) / len(words) if words else 0.0
        if text in BARGE and conf >= BARGE_CONF:
            self._barged(text, "%.2f" % conf)
        else:
            print("voice: barge? %r (%.2f)" % (text, conf), file=sys.stderr)

    def _barged(self, word, how):
        if self.barge(word):
            print("voice: barge %r (%s): hushed" % (word, how), file=sys.stderr)
        else:
            print("voice: barge %r (%s) ignored: she's saying it" % (word, how), file=sys.stderr)

    def _final(self, res, already_stopped):
        raw = res.get("text", "").split()
        text = res.get("text", "").replace("[unk]", "").strip()
        self.partial = ""
        if not text:
            return
        first = raw[0] in NAMES or (len(raw) > 1 and raw[0] in GREETS and raw[1] in NAMES)
        if ((first or raw[-1] in NAMES) and len(raw) >= 3 and text not in self.table
                and not any(text.endswith(" " + p) for p in COMMANDS)):
            # Her name, then something that isn't a command: "Patti, the café closes at
            # five" (as decoys and [unk]); or "hey Patti, ...", or her name last ("who's
            # Josh, Patti?"). friday writes it down; server.py checks it's to her (spoken_free).
            got = res.get("result") or []
            names = [w for w in got if w.get("word") in NAMES]
            name = (names[0] if first else names[-1]) if names else None
            mean = sum(w.get("conf", 0) for w in got) / len(got) if got else 0.0
            if name and name.get("conf", 0) < MIN_CONF:
                print("voice: %r (%.2f) unsure, the rest %.2f" % (name["word"], name.get("conf", 0), mean), file=sys.stderr)
            # (her name's own confidence, or the whole of it clear: friday checks the name anyway)
            if name and (name.get("conf", 0) >= MIN_CONF or mean >= MIN_CONF):
                long_s = got[-1].get("end", 0) - got[0].get("start", 0) + FREE_PAD_S
                chunks = list(self.longer)[-int(min(FREE_S, max(1.0, long_s)) * 10):]
                print("voice: %r (%.2f), and %.1f s she can't place -> free" % (name["word"], name["conf"], long_s),
                      file=sys.stderr)
                self.heard, self.heard_at = text, time.monotonic()
                self.log.append((time.time(), "%s ..." % name["word"], ("free", None)))
                self.said = b"".join(self.recent)
                self.act("free", b"".join(chunks), text)
                return
        words = [w for w in res.get("result", []) if w.get("word") != "[unk]"]
        act = self.table.get(text)
        now = time.monotonic()
        chatty = sum(1 for t in self.chatter if now - t < CHATTY_S) >= CHATTY_N
        if not act and not chatty:
            # In noise her name is the part that gets lost or misheard
            # ("forward stop recording"), so take a command that ends the
            # utterance after at least one other word, judged on its own words.
            # Not a one-word one ("resume", from the telly), and not while the
            # room's full of talk that isn't to her (CHATTY_N in CHATTY_S).
            for p in sorted(COMMANDS, key=len, reverse=True):
                n = len(p.split())
                if n >= 2 and text.endswith(" " + p) and len(words) > n:
                    act, words = COMMANDS[p], words[-n:]
                    break
        conf = sum(w.get("conf", 0) for w in words) / len(words) if words else 0.0
        if not act and conf < HEARD_MIN_CONF:
            # A scratch or a thump that came out as the nearest word ("hi"):
            # not worth showing (the chest's voice screen) or logging as heard.
            print("voice: noise? %r (%.2f)" % (text, conf), file=sys.stderr)
            return
        self.heard, self.heard_at = text, time.monotonic()
        if not act:
            self.chatter.append(now)
            print("voice: heard %r (%.2f), not a command" % (text, conf), file=sys.stderr)
            self.log.append((time.time(), text, None))
            return
        if conf < (STOP_CONF if act[0] == "stop" else MIN_CONF):
            print("voice: heard %r (%.2f), too unsure" % (text, conf), file=sys.stderr)
            self.log.append((time.time(), text, None))
            return
        print("voice: %r (%.2f) -> %s" % (text, conf, act[0]), file=sys.stderr)
        self.log.append((time.time(), text, act))
        if act[0] == "stop" and already_stopped and text in BARE:
            return
        self.said = b"".join(self.recent)
        self.act(act[0], act[1], text)

    def status(self):
        ago = time.monotonic() - self.heard_at if self.heard_at else None
        return {"ok": self.ok, "error": self.error, "muted": self.muted,
                "heard": self.heard if ago is not None and ago < 8 else ""}

    # ---------- for a conversation (meet.py): the mic in place of commands ----------

    def _borrow(self, take, timeout):
        """Hand the mic's 0.1 s chunks to take(data) -> True when done, in
        place of commands (the tap, as notes do), for at most timeout s.
        Not while she talks (she's deaf then). False if the mic is busy."""
        if not hasattr(self, "model") or self.tap:
            return False
        done = threading.Event()
        self.tap = lambda data: take(data) and done.set()
        try:
            done.wait(timeout)
        finally:
            self.tap = None
        return True

    def listen_once(self, grammar=None, wait_s=8.0, max_s=6.0, quiet_s=1.0):
        """One answer, no wake word: waits up to wait_s for talk to start,
        then until quiet_s of quiet (or max_s). Returns what Vosk made of it,
        "" for nothing: one of grammar's phrases, or (grammar None) any words
        it knows, for a name. Blocks: run it in a thread."""
        text = self.words(self.utterance(wait_s, max_s, quiet_s), grammar)
        print("voice: answer %r" % text, file=sys.stderr)
        return text

    def utterance(self, wait_s=8.0, max_s=6.0, quiet_s=1.0):
        """The audio of one bit of talk, no wake word (as listen_once hears
        it; 16-bit mono, RATE), b"" for none. Blocks."""
        chunks, st = [], {"start": None, "quiet": 0.0}
        t0 = time.monotonic()

        def take(data):
            loud = self.level_db > self.floor_db + TALK_DB
            now = time.monotonic()
            if st["start"] is None:
                if loud:
                    st["start"] = now
                    chunks.extend(list(self.recent)[-3:-1] + [data])    # (the start of the first word)
                return not loud and now - t0 > wait_s
            chunks.append(data)
            st["quiet"] = 0.0 if loud else st["quiet"] + 0.1
            return st["quiet"] >= quiet_s or now - st["start"] >= max_s

        if not self._borrow(take, wait_s + max_s + 2):
            return b""
        return b"".join(chunks)

    def words(self, audio, grammar=None):
        """What Vosk makes of some audio: one of grammar's phrases, or any
        words it knows; "" for nothing."""
        if not audio or not hasattr(self, "model"):
            return ""
        if grammar:
            r = self.vosk.KaldiRecognizer(self.model, RATE, json.dumps(list(grammar) + ["[unk]"]))
        else:
            r = self.vosk.KaldiRecognizer(self.model, RATE)
        r.AcceptWaveform(audio)
        return json.loads(r.FinalResult()).get("text", "").replace("[unk]", "").strip()

    def record(self, seconds):
        """seconds of the mic (16-bit mono, RATE), no wake word. Blocks."""
        chunks = []

        def take(data):
            chunks.append(data)
            return len(chunks) >= seconds * 10

        self._borrow(take, seconds + 5)
        return b"".join(chunks)

    def close(self):
        """Stop listening and wait for the thread, so Python never shuts down
        with it inside Vosk (that aborts the process)."""
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        if self.thread:
            self.thread.join(timeout=3)
