"""Her drives: what she does of her own accord, because of how she feels
(limbic.py), within her laws (laws.py). For the brain (server.py):

    d = drives.Drives(app, ctx)       # ctx: see server.py (present, say, transcribe, name_stranger, ...)
    asyncio.create_task(d.run())
    d.met_stranger(sid)               # someone new: she'll want to know who
    await d.reply(qid, text, who)     # an answer to one of her questions (voice or Telegram)

For now she wonders and asks:

  - Who someone new was ("stranger" questions, for whoever may name faces).
  - Follow-ups (server.py, understand_said): "How was London?", the day
    after they said they were going; their answer is made sense of like
    anything else said to her.
  - About the people she knows (YOU: favourite albums, where they grew up),
    when she's curious (dopamine low) and they're with her, settled (here
    PRESENT_FOR_S, nobody talking for a while), at most ASK_GAP_S apart and
    DAILY a day each. Their answer is kept as something she's been told
    (memory.py, answers), so "what's my favourite album?" works after.
    Only aloud when she can hear the answer (her mic on) and they're really
    there (seen or heard in the last HERE_NOW_S, not just "around": that's
    five minutes); if they've gone by the time she's asked, it wasn't asked
    (memory.py, unasked), and she'll ask another time. No harm done: an
    unanswered question never upsets her.

She's patient. A question waits (memory.py, questions) till they're with
her; if they aren't for FALLBACK_S, she messages them instead (Telegram,
at most BATCH at a time, BATCH_GAP_S apart, MESSAGES_DAILY a day), and waits
for that answer as long as it takes: a reply to it, or the next thing they
tell her there that isn't a question or a command. Wanting to ask doesn't stress her: having asked, or
noted it down, is enough. "Leave me alone" (dnd) quietens her for DND_S.
"""
import asyncio
import json
import random
import re
import sys
import time

import laws
import memory

CHECK_S = 30
PRESENT_FOR_S = 120     # with her this long first (not the moment they walk in)
SETTLED_S = 90          # and nobody's said anything to her for this long
ASK_GAP_S = 1800        # at most a question per person this often, in person
HERE_NOW_S = 20         # seen (or heard) this recently: really with her, to ask aloud
DAILY = 3               # in person, a day each (Telegram's extra, for what's waited)
CURIOUS = 0.40          # dopamine below this: she wants to know things (limbic.py; content is 0.35)
ASK_CHANCE = 0.3        # each CHECK_S she could ask, she does this often (so it isn't on the dot)
FALLBACK_S = 600        # not seen them this long, and a question's waited this long: message them
BATCH = 3
BATCH_GAP_S = 3600
MESSAGES_DAILY = 8
DND_S = 2 * 3600
ANSWER_WAIT_S, ANSWER_MAX_S, ANSWER_QUIET_S = 8.0, 15.0, 1.5

# About them: (what she says, what she remembers it as, lasting). {name}: theirs.
YOU = [
    ("What's your favourite album?", "What is {name}'s favourite album?", True),
    ("What's your favourite band?", "What is {name}'s favourite band?", True),
    ("What's your favourite book?", "What is {name}'s favourite book?", True),
    ("What's your favourite film?", "What is {name}'s favourite film?", True),
    ("What's your favourite food?", "What is {name}'s favourite food?", True),
    ("What's your favourite colour?", "What is {name}'s favourite colour?", True),
    ("Where did you grow up?", "Where did {name} grow up?", True),
    ("When's your birthday?", "When is {name}'s birthday?", True),
    ("What do you do for work?", "What does {name} do for work?", True),
    ("What's the best place you've ever lived?", "What is the best place {name} has lived?", True),
    ("What's something you're really good at?", "What is {name} really good at?", True),
    ("What are you working on at the moment?", "What is {name} working on?", False),
    ("What music should I listen to?", "What music does {name} think I should listen to?", True),
    ("What should I learn about next?", "What does {name} think I should learn about?", False),
    ("What's your favourite thing about where we live?", "What is {name}'s favourite thing about where we live?", True),
    ("What made you want to build me?", "Why did {name} want to build me?", True),
]

# "Not now", put any way ("I'm sorry, Patti, I can't talk right now").
NOT_NOW = re.compile(r"^\W*(?:no|nope|nah|later|pass|skip|stop|shush|i don'?t know|dunno|no idea|not sure)\b"
                     r"|\b(?:not now|not right now|another time|later on|can'?t talk|cannot talk|can'?t right now|"
                     r"(?:i'?m|i am)\s+(?:a bit\s+|really\s+|too\s+|quite\s+)?busy|no time|leave me alone|go away|rather not|"
                     r"don'?t want to)\b", re.I)
NAMED = re.compile(r"^\W*(?:(?:that'?s|that was|it'?s|it was|this is|they'?re|she'?s|he'?s|her name is|"
                   r"his name is|their name is)\s+)?(?:(?:my|our|a|the)\s+(?:\w+\s+)?(?:friend|neighbou?r|"
                   r"cousin|brother|sister|mum|mom|dad|son|daughter|partner|wife|husband|colleague|boss)\s+)?"
                   r"([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*)?)\W*$", re.I)


# ... or the name at the end, after "is", "was", "called": "The person you just met is Alex."
NAMED_END = re.compile(r"\b(?:is|was|called|named|that'?s|it'?s)\s+([A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+)?)\W*$")
NOT_NAMES = {"Patti", "Patty", "Pattie", "Josh", "Me", "Her", "Him", "Them", "My", "The", "A", "That", "This"}


def name_in(text):
    """'That's my friend John.' -> 'John'; 'The person you just met is Alex.'
    -> 'Alex'; None if there isn't one."""
    m = NAMED.match(text.strip())
    if m and len(m.group(1).split()) <= 2 and len(text.split()) <= 6:
        name = " ".join(w[:1].upper() + w[1:] for w in m.group(1).split())
    else:
        m = NAMED_END.search(text.strip()) or (re.search(r"\b([A-Z][a-z'-]+)\W*$", text.strip())
                                               if len(text.split()) <= 6 else None)    # ("my cleaner Alex")
        name = m.group(1) if m else None
    return name if name and name.split()[0] not in NOT_NAMES else None


class Drives:
    def __init__(self, app, ctx):
        self.app, self.ctx = app, ctx
        self.mem = app["memory"]
        self.here_since = {}        # person -> when she started seeing them, this time
        self.asked_at = {}          # person -> last in-person question
        self.batch_at = {}          # login -> last Telegram batch
        self.dnd_until = 0.0
        self.refused = {}           # (kind, why) -> last logged, so the log isn't flooded

    # ---------- told to ----------
    def dnd(self, on=True):
        self.dnd_until = time.time() + DND_S if on else 0.0

    def quiet(self):
        return time.time() < self.dnd_until

    def met_stranger(self, sid):
        """Someone new: for everyone who may name them, "who was that?"."""
        when = memory.when(time.time())
        for login, perms in self.ctx["logins"]():
            if "people" in perms and "users" in perms:
                self.mem.wonder(login, "stranger", "I met someone new %s. Who was it?" % when, sid)

    # ---------- the loop ----------
    async def run(self):
        while True:
            await asyncio.sleep(CHECK_S)
            try:
                await self._tick()
            except Exception as e:      # (never take the brain down)
                print("drives: %r" % e, file=sys.stderr)

    def _may(self, kind, **kw):
        ok, why = laws.check(kind, dnd=self.quiet(), **kw)
        if not ok and time.time() - self.refused.get((kind, why), 0) > 3600:
            self.refused[(kind, why)] = time.time()
            print("drives: won't %s (%s)" % (kind, why), file=sys.stderr)
        return ok

    async def _tick(self):
        now = time.time()
        st = self.app["who"]["strangers"]
        for q in self.mem.wondering(kind="stranger"):       # (named or forgotten since: never mind)
            if not st or q["about"] not in st.people:
                self.mem.answered(q["id"], None)
                continue
            known = self.ctx["known_as"](q["about"])        # (met properly since: "Patti, remember me")
            if known and await self.ctx["name_stranger"](q["about"], known):
                print("drives: %s was %s all along" % (q["about"], known), file=sys.stderr)
                self.mem.answered(q["id"], known)
        present = self.ctx["present"]()
        for p in list(self.here_since):
            if p not in present:
                del self.here_since[p]
        for p in present:
            self.here_since.setdefault(p, now)
        await self._in_person(now, present)
        await self._messages(now, present)

    async def _in_person(self, now, present):
        lim = self.app["limbic"]
        if not present or self.ctx["busy"]() or self.mem.talking() or not self.ctx["hears"]():
            return
        for person in present:
            login = self.ctx["login_for"](person)
            if not login or now - self.here_since[person] < PRESENT_FOR_S:
                continue
            if self.ctx["here_ago"](person) > HERE_NOW_S:
                continue                # (around, but not with her just now: maybe gone upstairs)
            if now - self.ctx["last_said"]() < SETTLED_S or now - self.asked_at.get(person, 0) < ASK_GAP_S:
                continue
            if self.mem.asked_since(login, now - 86400) >= DAILY:
                continue
            waiting = self.mem.wondering(login)
            if not waiting and lim.level["dopamine"] >= CURIOUS:
                continue                # (nothing to ask, and not curious enough to think of something)
            if random.random() > ASK_CHANCE:
                continue
            perms = self.ctx["perms"](login)
            q = waiting[0] if waiting else self._new_question(login, person)
            if not q or not self._may("speak", perms=perms, about=q["kind"]):
                continue
            self.asked_at[person] = now
            await self._ask(q, person)
            return

    def _new_question(self, login, person):
        had = self.mem.ever_asked(login)
        fresh = [y for y in YOU if y[0] not in had]
        if not fresh:
            return None
        said, keep, lasting = random.choice(fresh)
        qid = self.mem.wonder(login, "you", said, json.dumps({"q": keep.format(name=person), "lasting": lasting}))
        return self.mem.question(qid)

    async def _ask(self, q, person):
        m, v = self.app["meet"], self.app["voice"]
        if m.busy or not v:
            return
        m.busy = True
        try:
            print("drives: asking %s %r" % (person, q["text"]), file=sys.stderr)
            self.mem.asked(q["id"], "voice")
            began = time.monotonic()
            await m.say_text("%s, can I ask you something? %s" % (person, q["text"]))
            audio = await asyncio.to_thread(v.utterance, ANSWER_WAIT_S, ANSWER_MAX_S, ANSWER_QUIET_S)
            text = (await self.ctx["transcribe"](audio)).strip() if audio else ""
            print("drives: %s answered %r" % (person, text), file=sys.stderr)
            if not text and self.ctx["here_ago"](person) > time.monotonic() - began:
                self.mem.unasked(q["id"])   # (not seen or heard since just before: gone, so it wasn't asked)
                print("drives: %s had gone; I'll ask another time" % person, file=sys.stderr)
                return
            if not text:                # (patient: whatever they say next, a while from now, is the answer)
                self.ctx["await_answer"](person, q)
                await m.say_text(random.choice(["No rush. Tell me whenever you're ready.",
                                                "That's okay, tell me later."]))
                return
            if NOT_NOW.search(text):
                polite = self.app["limbic"].manners(text, person)
                await m.say_text("That's okay, thanks for letting me know." if polite else "Okay, no worries.")
                if re.search(r"leave me alone|go away|shush", text, re.I):
                    self.dnd()
                return
            await m.say_text(await self.reply(q["id"], text, person))
        finally:
            m.busy = False

    async def _messages(self, now, present):
        """Questions that have waited, for people she hasn't seen: Telegram."""
        t = self.app["telegram"]
        if not t:
            return
        lim = self.app["limbic"]
        by = {}
        for q in self.mem.wondering():
            if (not q["asked"] or (q["how"] == "voice" and now - q["asked"] >= FALLBACK_S)) and now - q["at"] >= FALLBACK_S:
                by.setdefault(q["for_who"], []).append(q)
        for login, qs in by.items():
            person = self.ctx["person_for"](login)
            if person in present or now - lim.last_seen.get(person, 0) < FALLBACK_S:
                continue                # (she'll see them: ask then)
            if now - self.batch_at.get(login, 0) < BATCH_GAP_S or not t.chats(login):
                continue
            perms = self.ctx["perms"](login)
            sent = 0
            left = MESSAGES_DAILY - sum(1 for q in self.mem.asked_list(login, now - 86400) if q.startswith("telegram"))
            for q in qs[:max(0, min(BATCH, left))]:
                if not self._may("message", perms=perms, about=q["kind"]):
                    continue
                for chat in t.chats(login):
                    msg = await asyncio.to_thread(t.send, chat, q["text"] + (
                        " (My People page has their picture: reply with their name.)" if q["kind"] == "stranger"
                        else " (Reply to this, whenever.)"), None, True)
                    if msg:
                        self.mem.asked(q["id"], "telegram:%s:%s" % (chat, msg["message_id"]))
                        self.ctx["await_answer"](person, q, True)
                        sent += 1
            if sent:
                self.batch_at[login] = now
                print("drives: messaged %s %d question(s)" % (login, sent), file=sys.stderr)

    # ---------- answers ----------
    def question_for_reply(self, chat, message_id):
        """The question a Telegram reply answers, or None."""
        how = "telegram:%s:%s" % (chat, message_id)
        return next((q for q in self.mem.wondering() if q["how"] == how), None)

    async def reply(self, qid, text, who):
        """Their answer to one of her questions: kept (and acted on). What she says back."""
        q = self.mem.question(qid)
        if not q or q["answered"]:
            return "Thanks, but I've already got the answer to that."
        self.ctx["answered"](who, qid)          # (no longer waiting for this one: the answer's here)
        lim = self.app["limbic"]
        lim.manners(text, who)
        if q["kind"] == "stranger":
            if NOT_NOW.search(text):
                self.mem.answered(qid, None)
                return "Okay. I'll keep them as someone I don't know."
            name = name_in(text)
            got = await self.ctx["name_stranger"](q["about"], name) if name else None
            if not got:
                return "I didn't catch a name there. Reply with just their name?"
            self.mem.answered(qid, got)
            lim.felt("learned", who, why="named %s" % got)
            return "%s! Thanks, I'll know them next time." % got
        about = json.loads(q["about"] or "{}")
        self.mem.answered(qid, text)
        if not about.get("q"):          # (a follow-up, "How was London?": made sense of like anything said)
            line = await self.ctx["understand"](who, "(Answering Patti's question: %s) %s" % (q["text"], text))
            if line:
                return line
        else:
            await self.ctx["remember"](who, about["q"], "%s told me: %s" % (who, text), bool(about.get("lasting")))
        lim.felt("learned", who, why=q["text"])
        return random.choice(["Thanks! I'll remember that.", "Ooh, good to know. Thanks!", "Noted. Thank you!"])
