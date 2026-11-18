"""Meeting people: she learns a new face and voice by talking with them, and
forgets them again when asked. For the brain (server.py).

    m = meet.Meet(app, ctx)
    await m.meet()              # a stranger: ask, then name, face and voice
    await m.meet(name="Sam")   # a face she knows: ask, then just the voice
    await m.meet(asked=True)    # they asked ("Patti, remember me")
    await m.forget("Alex")      # "Patti, forget me", once she knows who's asking
    await m.introduce(sid)      # "Patti, this is my friend": name someone she's seen (strangers.py)

Nothing is kept without a yes: she asks first, saying what she'll keep. Then:

  name    "What's your name?", heard with all the words Vosk knows (not the
          command list), and checked back: "Alex, is that right?" Two tries.
          A name she already has is refused, so nobody's mixed up.
  face    "Turn your head slowly": FACE_COUNT crops from her own camera, one
          every FACE_EVERY_S, skipping any too like one already taken (SAME),
          as who.py enroll does. Fewer than FACE_MIN in FACE_TIMEOUT_S: the
          light's probably bad; nothing kept.
  voice   "Talk to me for half a minute": VOICE_S recorded, cut into clips
          and kept as voices.py enroll does. Fewer than VOICE_MIN_CLIPS: not
          kept (the face still is).

Then both are reloaded at once, no restart. Answers come from the mic with
no wake word (voice.listen_once), so while she waits for one, commands don't
work: a few seconds at a time.

Introducing someone (introduce): she's already got their face, unnamed
(strangers.py), so it's just "What's their name?", checked back as above,
and their face crops join faces/<name>. A name she already knows is taken
as that person (she just didn't recognise them): it helps her next time.
Their voice can follow later ("Patti, learn my voice").

ctx: say(text), detect_faces(frame), greeted(name) (counts as greeted
today), forgot(name) (drop them from who's around), named(sid, name) (a
stranger has a name: memory.py).
"""
import asyncio
import glob
import os
import re
import shutil
import sys
import time

import cv2
import numpy as np

import faces
import users
import replies
import voices

FACE_COUNT = 20
FACE_MIN = 10
FACE_EVERY_S = 0.7
FACE_TIMEOUT_S = 60
FACE_MIN_PX = 80        # a face narrower than this is too far away to learn
SAME = 0.92             # as who.py's SAME
VOICE_S = 30
# Someone she has a face for but no voice, not recognised for sure (a face
# enrolled from photos): giving that name is accepted if the face in front of
# her looks more like them than anyone, at least this much (strangers score
# about 0.1-0.25 against the household).
LOOKS_LIKE = 0.25
VOICE_MIN_CLIPS = 5

YES = ("yes", "yeah", "yep", "yup", "sure", "okay", "ok", "yes please", "go on", "go ahead", "go for it",
       "do it", "please do", "alright", "of course", "please", "ask", "ask claude", "ask him", "ask it",
       "yes ask claude", "yeah go ahead", "sure go ahead", "yes go ahead", "why not", "definitely", "absolutely")
NO = ("no", "nope", "nah", "no thanks", "no thank you", "not now", "don't", "don't bother", "never mind",
      "no don't", "not really", "leave it")
# Heard but not one of those exactly ("go" for "go ahead"): a word of each kind
# decides it, as long as it isn't both.
YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "okay", "ok", "go", "ahead", "do", "please", "alright",
             "course", "ask", "definitely", "absolutely"}
NO_WORDS = {"no", "nope", "nah", "don't", "never", "leave"}     # (not "not": "sure, why not")
# What people say around a name: "my name's Alex", "I'm Alex", "call me Alex".
FILLER = {"my", "name", "name's", "names", "is", "i", "i'm", "im", "am", "it's", "its", "call", "me",
          "this", "they", "the", "a", "hi", "hello", "hey", "oh", "um", "uh", "er", "well", "so"}


def yes_or_no(a):
    """What an answer means: True, False, or None (nothing, or can't tell)."""
    if a in YES:
        return True
    if a in NO:
        return False
    words = set(a.split())
    y, n = bool(words & YES_WORDS), bool(words & NO_WORDS)
    return True if y and not n else False if n and not y else None


def parse_name(text):
    """'my name is alex' -> 'Alex'; '' -> None. At most two words."""
    words = [w for w in re.findall(r"[a-z']+", text.lower().replace("[unk]", " ")) if w not in FILLER]
    words = [w for w in words if w not in YES and w not in NO and w not in users.HER_NAMES and len(w) > 1][:2]
    return " ".join(w.capitalize() for w in words) or None


class Meet:
    def __init__(self, app, ctx):
        self.app, self.ctx = app, ctx
        self.busy = False

    # ---------- talking ----------
    async def say_text(self, text):
        """Say it and wait till it's been heard."""
        self.ctx["say"](text)
        await asyncio.sleep(0.3)
        sp = self.app["speech"]
        while sp and sp.speaking:
            await asyncio.sleep(0.1)

    async def say_line(self, key, **fields):
        """Say one of her lines (replies.py) and wait till it's been heard."""
        await self.say_text(replies.pick(key, **fields))

    async def answer(self, grammar=None, **kw):
        v = self.app["voice"]
        return await asyncio.to_thread(v.listen_once, grammar, **kw) if v else ""

    async def yes(self, key, **fields):
        """Ask a yes-or-no question: True, False, or None (no answer, even
        when asked again). (For the brain's spoken questions too.)"""
        await self.say_line(key, **fields)
        for again in (False, True):
            if again:       # (nothing heard, or not a yes or a no: once more, not "no")
                await self.say_line("yes_again")
            got = yes_or_no(await self.answer(YES + NO))
            if got is not None:
                return got
        return None

    async def _looks_like(self):
        """Who the face in front of her looks most like, however unsure
        (above LOOKS_LIKE), or None."""
        who, cam = self.app["who"]["faces"], self.app["camera"]
        frame = cam.frame
        if frame is None:
            return None
        found = await asyncio.to_thread(self.ctx["detect_faces"], frame)
        if found is None or len(found) != 1:
            return None
        s = await asyncio.to_thread(lambda: who.scores(who.fingerprint(who.crop(frame, found[0]))))
        return s[0][1] if s and s[0][0] >= LOOKS_LIKE else None

    def _known(self, name):
        have = {os.path.basename(d.rstrip("/")).lower()
                for base in (faces.FACES_DIR, voices.VOICES_DIR) for d in glob.glob(os.path.join(base, "*", ""))}
        return name.lower() in have

    # ---------- learning ----------
    async def _name(self, ask="meet_name", again="meet_name_again"):
        for attempt in range(2):
            await self.say_line(ask if attempt == 0 else again)
            name = parse_name(await self.answer(None, max_s=4.0))
            if name and await self.yes("meet_name_check", name=name):
                return name
        await self.say_line("meet_name_fail")
        return None

    async def _face(self, name):
        """Crops of their face into faces.FACES_DIR/name; how many."""
        who, cam = self.app["who"]["faces"], self.app["camera"]
        d = os.path.join(faces.FACES_DIR, name)
        os.makedirs(d, exist_ok=True)
        first = max([int(os.path.splitext(f)[0]) for f in os.listdir(d) if os.path.splitext(f)[0].isdigit()],
                    default=0)          # (adding to someone she knows: after theirs)
        have, saved, last, seen = [], 0, 0.0, None
        start = time.monotonic()
        cam.join(look=True)
        try:
            while saved < FACE_COUNT and time.monotonic() - start < FACE_TIMEOUT_S:
                await asyncio.sleep(0.1)
                frame, at = cam.frame, cam.frame_at
                if frame is None or at == seen or time.monotonic() - last < FACE_EVERY_S:
                    continue
                seen = at
                found = await asyncio.to_thread(self.ctx["detect_faces"], frame)
                if found is None or len(found) != 1 or found[0][2] < FACE_MIN_PX:
                    continue            # (nobody, two people, or too far away)
                crop = await asyncio.to_thread(who.crop, frame, found[0])
                fp = await asyncio.to_thread(who.fingerprint, crop)
                if have and max(float(fp @ h) for h in have) > SAME:
                    continue            # too like one already taken: keep turning
                cv2.imwrite(os.path.join(d, "%04d.jpg" % (first + saved + 1)), crop)
                have.append(fp)
                saved += 1
                last = time.monotonic()
                if saved == FACE_COUNT // 2:
                    self.ctx["say"](replies.pick("meet_face_half"))
        finally:
            cam.leave(look=True)
        print("meet: %d face crops of %s" % (saved, name), file=sys.stderr)
        return saved

    async def _voice(self, name):
        """Clips of their voice into voices.VOICES_DIR/name; how many."""
        raw = await asyncio.to_thread(self.app["voice"].record, VOICE_S)
        x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
        if not len(x):
            return 0
        floor = np.percentile(voices.frames_db(x), 10)
        n = int(voices.CLIP_S * voices.RATE)
        clips = [c for c in (x[i:i + n] for i in range(0, len(x) - n + 1, n)) if voices.speechy(c, floor)]
        d = os.path.join(voices.VOICES_DIR, name)
        os.makedirs(d, exist_ok=True)
        first = len(glob.glob(os.path.join(d, "*.wav")))
        for i, c in enumerate(clips):
            voices.write_wav(os.path.join(d, "%04d.wav" % (first + i + 1)), c)
        print("meet: %d voice clips of %s" % (len(clips), name), file=sys.stderr)
        return len(clips)

    async def _reload(self):
        w = self.app["who"]
        for k in ("faces", "voices"):
            if w[k]:
                await asyncio.to_thread(w[k].load)

    # ---------- the conversations ----------
    async def meet(self, name=None, asked=False):
        """name: someone whose face she knows, to learn their voice. asked:
        they asked her to remember them (else she's offering)."""
        if self.busy:
            return
        self.busy = True
        try:
            await self._meet(name, asked)
        except Exception as e:      # keep the brain up whatever happens here
            print("meet: %r" % e, file=sys.stderr)
            self.ctx["say"]("Sorry, something went wrong. Let's try another time.")
        finally:
            self.busy = False
            self.app["who"]["asked_at"] = time.monotonic()

    async def _meet(self, name, asked):
        ok = await self.yes("meet_ask_voice" if name else "meet_ask_asked" if asked else "meet_ask",
                             name=name)
        if not ok:
            await self.say_line("meet_no" if ok is False else "meet_noanswer")
            return
        if name:                    # a face she knows: just the voice
            await self.say_line("meet_voice_only")
            if await self._voice(name) < VOICE_MIN_CLIPS:
                shutil.rmtree(os.path.join(voices.VOICES_DIR, name), ignore_errors=True)
                await self.say_line("meet_voice_fail")
                return
            await self._reload()
            await self.say_line("meet_done_voice", name=name)
            return
        name = await self._name()
        if not name:
            return
        if self._known(name):
            vs = self.app["who"]["voices"]
            if vs and name not in vs.people and await self._looks_like() == name:
                # Them after all, just not recognised for sure: their voice,
                # and fresh face pictures from her own camera too.
                await self.say_line("meet_face", name=name)
                await self._face(name)
                await self.say_line("meet_voice")
                if await self._voice(name) < VOICE_MIN_CLIPS:
                    shutil.rmtree(os.path.join(voices.VOICES_DIR, name), ignore_errors=True)
                    await self._reload()
                    await self.say_line("meet_voice_fail")
                    return
                await self._reload()
                self.ctx["greeted"](name)
                await self.say_line("meet_done", name=name)
                return
            await self.say_line("meet_known", name=name)
            return
        await self.say_line("meet_face", name=name)
        if await self._face(name) < FACE_MIN:
            shutil.rmtree(os.path.join(faces.FACES_DIR, name), ignore_errors=True)
            await self.say_line("meet_face_fail")
            return
        await self.say_line("meet_voice")
        if await self._voice(name) < VOICE_MIN_CLIPS:
            shutil.rmtree(os.path.join(voices.VOICES_DIR, name), ignore_errors=True)
            await self._reload()
            self.ctx["greeted"](name)
            await self.say_line("meet_voice_fail")
            return
        await self._reload()
        self.ctx["greeted"](name)       # (she's just met them: no "hello" on top)
        await self.say_line("meet_done", name=name)

    async def introduce(self, sid):
        """sid: the stranger in view (strangers.py), or None if there isn't one."""
        if self.busy:
            return
        if not sid:
            await self.say_line("intro_nobody")
            return
        self.busy = True
        try:
            name = await self._name("intro_name", "intro_name_again")
            if not name:
                return
            known = self._known(name)
            if not await asyncio.to_thread(self.app["who"]["strangers"].name, sid, name):
                await self.say_line("intro_lost")
                return
            await self._reload()
            self.ctx["named"](sid, name)
            self.ctx["greeted"](name)
            await self.say_line("intro_known" if known else "intro_done", name=name)
        except Exception as e:      # keep the brain up whatever happens here
            print("meet: %r" % e, file=sys.stderr)
            self.ctx["say"]("Sorry, something went wrong. Let's try another time.")
        finally:
            self.busy = False

    async def forget(self, name):
        """name: who's asking (by voice or face), or None if she can't tell."""
        if self.busy:
            return
        if not name:
            await self.say_line("forget_who")
            return
        self.busy = True
        try:
            if not await self.yes("forget_ask", name=name):
                await self.say_line("forget_no")
                return
            for base in (faces.FACES_DIR, voices.VOICES_DIR):
                shutil.rmtree(os.path.join(base, name), ignore_errors=True)
            await self._reload()
            self.ctx["forgot"](name)
            print("meet: forgot %s" % name, file=sys.stderr)
            await self.say_line("forget_done", name=name)
        finally:
            self.busy = False
