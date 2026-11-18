"""Wikipedia, for "who is ...", "what is ..." and "tell me about ...": the
opening of the article, free and quick, before she'd ask Claude. For the
brain (server.py).

    wiki.subject("Who was Marc Bolan?")    # "Marc Bolan"; None if it isn't that sort of question
    wiki.lookup("Marc Bolan")              # {title, answer (to say), extract (all of it), url}, or None

First friday's offline copy (friday/offline_wiki.py, Kiwix's mini English
Wikipedia: no internet needed), then, if friday's off or hasn't got it, the
live site: the best match from Wikipedia's search, then its summary (the REST
API: no account, but it wants to know who's asking). None for a page that only lists
what a name may refer to (disambiguation), or a match that isn't much like
what was asked; and subject() turns down what an encyclopedia can't know:
"the current ...", "my ...", today's anything. She says the first SAY_SENTENCES
sentences, without the brackets (pronunciations, dates) that don't read aloud.
"""
import difflib
import json
import re
import urllib.parse
import urllib.request

import claude

AGENT = "Patti-home-robot/1.0 (+https://github.com/jzellis/robotpattinson)"
SEARCH = "https://en.wikipedia.org/w/rest.php/v1/search/page"
SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
TIMEOUT_S = 6
SAY_SENTENCES = 2
# The title (or its lead words) this like the subject, at least: Wikipedia's
# opening is the answer only when what's asked about is the article ("Tom
# Waits", "a quasar"), not something about it ("the atomic weight of cobalt"
# is not "Standard atomic weight", 0.72): those go to Claude.
ALIKE = 0.85

ASKS = re.compile(r"^(?:who|what)(?:'s|\s+is|\s+was|\s+are|\s+were)\s+(.+?)[\s?.!]*$"
                  r"|^tell me (?:about|who|what)\s+(?:is\s+|was\s+)?(.+?)[\s?.!]*$", re.I)
# What an encyclopedia can't answer: now, here, personal.
NOT_WIKI = re.compile(r"\b(current|currently|now|today|tonight|tomorrow|yesterday|latest|this week|this year|"
                      r"my|your|our|his|her|their|you|me|kowloon|weather|time|the time|playing)\b", re.I)


def subject(question):
    """The thing asked about, for a who/what question; else None."""
    m = ASKS.match(question.strip())
    if not m:
        return None
    s = (m.group(1) or m.group(2)).strip()
    if not s or NOT_WIKI.search(s) or len(s.split()) > 8:
        return None
    return s


def _get(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.load(r)


def _plain(text):
    return re.sub(r"^(the|a|an)\s+", "", text.lower().strip())


def spoken(extract, n=SAY_SENTENCES):
    """The first n sentences, without brackets."""
    text = re.sub(r"\s*\([^()]*\)", "", extract)
    text = re.sub(r"\s*\([^()]*\)", "", text)       # (nested ones)
    text = re.sub(r"\s+([,.;])", r"\1", re.sub(r"\s{2,}", " ", text)).strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    sentences = []
    for p in parts:     # (not after an initial or an abbreviation: "T. Rex", "St. Albans", "U.S. Army")
        if sentences and re.search(r"(?:\b[A-Z]|\b(?:St|Dr|Mr|Mrs|Ms|Jr|Sr|Mt|No|vs|Co|Inc|Ltd))\.$", sentences[-1]):
            sentences[-1] += " " + p
        else:
            sentences.append(p)
    return " ".join(sentences[:n])


def _alike(subj, title):
    want, lead = _plain(subj), title.split(" (")[0]     # "Mercury (planet)" -> "Mercury"
    return max(difflib.SequenceMatcher(None, want, _plain(t)).ratio() for t in (title, lead)) >= ALIKE


def _offline(subj):
    """From friday's copy: the answer, False (it's there, but a list of what
    the name may mean: no answer), or None (friday's off, or hasn't got it)."""
    try:
        found = claude.post_json("/wiki", {"q": subj}).get("results") or []
    except claude.Unavailable:
        return None
    for f in found:
        if _alike(subj, f["title"]):
            if f["disambiguation"]:
                return False
            return {"title": f["title"], "answer": spoken(f["extract"]), "extract": f["extract"],
                    "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(f["title"].replace(" ", "_")),
                    "offline": True}
    return None


def lookup(subj):
    """Blocking. Wikipedia's opening on subj, or None."""
    got = _offline(subj)
    if got is not None:
        return got or None
    try:
        pages = _get(SEARCH, {"q": subj, "limit": 3}).get("pages") or []
    except (OSError, ValueError):
        return None
    for p in pages:
        title = p.get("title", "")
        if not _alike(subj, title):
            continue
        try:
            s = _get(SUMMARY + urllib.parse.quote(p["key"], safe=""), {"redirect": "true"})
        except (OSError, ValueError):
            return None
        if s.get("type") != "standard" or not s.get("extract"):
            return None             # (a list of what it may refer to: ask Claude instead)
        return {"title": s.get("title", title), "answer": spoken(s["extract"]), "extract": s["extract"],
                "url": s.get("content_urls", {}).get("desktop", {}).get("page", "")}
    return None


if __name__ == "__main__":
    import sys
    for q in sys.argv[1:] or ["Who is Tom Waits?", "What is a quasar?", "Who was Marc Bolan?",
                              "What is Mercury?", "Who is the current prime minister?", "Tell me about Bristol"]:
        s = subject(q)
        print(q, "->", s, "->", lookup(s) if s else None)
