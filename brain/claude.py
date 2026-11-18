"""Asking Claude: questions she can't answer herself go to Claude Code on
friday (friday/ask_claude.py), when someone says yes to "Should I ask
Claude?". For the brain (server.py).

    claude.ask("How far away is the moon?", asker="Josh", place="Bristol", history=[{"q", "a", "kowloon"}])
        # {answer, lasting, private, keep, standalone}: see friday/ask_claude.py; raises Unavailable
    claude.transcribe(audio, hint="Tom Waits")  # what was said (16-bit mono 16 kHz), by Whisper; raises Unavailable
    claude.embed(["How far is the moon?"])     # what they mean, as unit vectors (to compare); None if friday's off
    claude.rerank([(q1, q2)])                  # whether each pair asks the same thing (4+: yes); None if friday's off
    claude.understand("I'm off to London tomorrow", "Josh", "Bristol", ["Sam"])
        # {kind, reply, facts: [{q, a, lasting, theirs}], followup}: see friday/ask_claude.py; raises Unavailable
    claude.reason("What time does the café close today?", "Josh", [{"q", "a", "when"}])
        # an answer worked out from only those notes, or None; raises Unavailable

Needs the key friday has (~/.config/robotpattinson/claude-key, made by
friday/install.sh's instructions). Answers are kept in her memory
(memory.py, answers), so she can answer the same question herself next time.
"""
import json
import threading
import os
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

URL = os.environ.get("RP_ASK_CLAUDE", "http://100.64.0.10:8765/ask")     # friday, over Tailscale
EMBED_URL = URL.rsplit("/", 1)[0] + "/embed"
TRANSCRIBE_URL = URL.rsplit("/", 1)[0] + "/transcribe"
TRANSCRIBE_TIMEOUT_S = 30
EMBED_TIMEOUT_S = 5
KEY_FILE = os.path.expanduser("~/.config/robotpattinson/claude-key")
TIMEOUT_S = 150


class Unavailable(Exception):
    pass


def ready():
    return os.path.exists(KEY_FILE)


def _post(url, body, timeout, raw=False, query=None):
    try:
        with open(KEY_FILE) as f:
            key = f.read().strip()
    except OSError:
        raise Unavailable("no key")
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, data=body if raw else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/octet-stream" if raw else "application/json",
                                          "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            raise Unavailable(json.load(e).get("error", str(e)))
        except ValueError:
            raise Unavailable(str(e))
    except (OSError, ValueError) as e:
        raise Unavailable(str(e))


def post_json(path, body, timeout=EMBED_TIMEOUT_S):
    """Blocking: another of friday's services ("/wiki"), its JSON answer; raises Unavailable."""
    return _post(URL.rsplit("/", 1)[0] + path, body, timeout)


UNDERSTAND_TIMEOUT_S = 60


def understand(text, speaker=None, place=None, known=(), about=(), history=()):
    """Blocking: what to make of something said to her that isn't one of her
    commands (a statement, small talk, or a question to look up); raises
    Unavailable. about: what's true about her now (lines); history: the
    conversation so far ([{"q", "a"}])."""
    r = _post(URL.rsplit("/", 1)[0] + "/understand",
              {"text": text, "speaker": speaker, "place": place, "known": list(known), "about": list(about),
               "history": list(history)}, UNDERSTAND_TIMEOUT_S)
    if "kind" not in r:
        raise Unavailable(r.get("error", "no answer"))
    return r


def reason(question, asker=None, notes=()):
    """Blocking: an answer worked out from what she's been told (notes), or
    None if they don't answer it; raises Unavailable."""
    r = _post(URL.rsplit("/", 1)[0] + "/reason", {"question": question, "asker": asker, "notes": list(notes)},
              UNDERSTAND_TIMEOUT_S)
    if "answer" not in r:
        raise Unavailable(r.get("error", "no answer"))
    return r["answer"]


def ask(question, asker=None, place=None, history=()):
    """Blocking (run it in a thread): Claude's answer, ready to say, and
    about it: lasting (stays true, or could change), private (from Kowloon's
    repos and project memory), keep (worth remembering), standalone (the
    question as it'd be asked on its own, to remember it by).
    history: the conversation so far, oldest first."""
    r = _post(URL, {"question": question, "asker": asker, "place": place, "history": list(history)}, TIMEOUT_S)
    if "answer" not in r:
        raise Unavailable("no answer")
    return {"answer": r["answer"], "lasting": bool(r.get("lasting")), "private": bool(r.get("private")),
            "keep": r.get("keep", True), "standalone": r.get("standalone") or question}


LIBRARY_TIMEOUT_S = 150
READ_TIMEOUT_S = 300


def tally(how):
    """What answered something said to her ("recall", "wikipedia", "claude", "understood", ...), counted
    on friday (corpus.py): how much she leans on Claude, over time. In the background; never fails."""
    def send():
        try:
            _post(URL.rsplit("/", 1)[0] + "/tally", {"how": how}, EMBED_TIMEOUT_S)
        except Unavailable:
            pass
    if ready():
        threading.Thread(target=send, daemon=True).start()


def library(question, asker=None):
    """Blocking: a question answered from Josh's own books (friday's index
    of his ebook library): {reply, books, q, a, found}; raises Unavailable."""
    r = _post(URL.rsplit("/", 1)[0] + "/library", {"question": question, "asker": asker}, LIBRARY_TIMEOUT_S)
    if "reply" not in r:
        raise Unavailable(r.get("error", "no answer"))
    return r


def books(q=None, stop=False, want=None):
    """Blocking: his books most like a title or author ({"books": [{id, title, authors, score}]}), the one
    she's reading ("reading"), what she's read ("shelf") and her reading list ("wanted"); stop: she puts the
    one she's reading down; want: a book's id, onto her list (and started, if she isn't reading one)."""
    return _post(URL.rsplit("/", 1)[0] + "/books", {"q": q, "stop": stop, "want": want}, UNDERSTAND_TIMEOUT_S)


def read(book_id=None, by=None):
    """Blocking (a minute or two): she reads the next sitting (a chapter) of that book, starting it, or of
    the one she's reading: {book, authors, id, chapter, sitting, of, notes, thought, question, facts,
    finished}, or {"nothing": true} / {"busy": true}; raises Unavailable."""
    return _post(URL.rsplit("/", 1)[0] + "/read", {"id": book_id, "by": by}, READ_TIMEOUT_S)


def connect(book, chapter, notes, thought, known):
    """Blocking: which of the things she knew (known: [{q, a, who, when}]) what she's just read (that sitting
    of book: {title, authors}) genuinely connects to: {"links": [{n (1-based), kind: expands | echo | same,
    how, ask}], "line": her "oh!"}. Raises Unavailable."""
    r = _post(URL.rsplit("/", 1)[0] + "/connect",
              {"title": book.get("title"), "authors": book.get("authors"), "chapter": chapter,
               "notes": notes, "thought": thought, "known": list(known)}, UNDERSTAND_TIMEOUT_S)
    if "links" not in r:
        raise Unavailable(r.get("error", "no answer"))
    return r


def linked(book_id, links, interest):
    """Blocking: what the sitting she's just read of that book reminded her of (["how", ...]) and how much
    it caught her interest, kept with her notes on friday (what she's liked reading)."""
    return _post(URL.rsplit("/", 1)[0] + "/books", {"linked": {"id": book_id, "links": list(links),
                                                               "interest": interest}}, UNDERSTAND_TIMEOUT_S)


SEE_TIMEOUT_S = 120


def see(jpeg, sender=None, caption="", people=(), place=None):
    """Blocking: a photo sent to her (JPEG bytes), looked at on friday.
    people: who her face recognition found in it. {"reply": what she
    says back, "about": a sentence to remember it by}; raises Unavailable."""
    r = _post(URL.rsplit("/", 1)[0] + "/see", jpeg, SEE_TIMEOUT_S, raw=True,
              query={"sender": sender or "", "caption": (caption or "")[:500], "people": ",".join(people),
                     "place": place or ""})
    if "reply" not in r:
        raise Unavailable(r.get("error", "no answer"))
    return r


def transcribe(audio, hint=""):
    """Blocking: what was said in audio (16-bit mono, 16 kHz), by Whisper on
    friday. hint: names it might hear, so it spells them right."""
    try:
        return _post(TRANSCRIBE_URL, audio, TRANSCRIBE_TIMEOUT_S, raw=True, query={"hint": hint})["text"]
    except KeyError as e:
        raise Unavailable(str(e))


def rerank(pairs):
    """Blocking: per pair of questions, how much they ask the same thing
    (friday's cross-encoder: 4 to 8 for the same put another way, under 2
    for different), or None if friday can't say."""
    try:
        return _post(EMBED_URL.rsplit("/", 1)[0] + "/rerank", {"pairs": [list(p) for p in pairs]},
                     EMBED_TIMEOUT_S)["scores"]
    except (Unavailable, KeyError):
        return None


def embed(texts):
    """Blocking: [unit vector] per text, or None if friday can't say."""
    try:
        vs = _post(EMBED_URL, {"texts": texts}, EMBED_TIMEOUT_S)["vectors"]
    except (Unavailable, KeyError):
        return None
    out = []
    for v in vs:
        v = np.asarray(v, np.float32)
        out.append(v / (np.linalg.norm(v) or 1.0))
    return out
