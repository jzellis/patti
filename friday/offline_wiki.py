"""Wikipedia's openings, offline: Kiwix's "mini" English Wikipedia (each
article's introduction, the whole of it, one ZIM file) in WIKI_DIR, read with
libzim. For ask_claude.py's /wiki, so Patti's who/what questions don't need
the internet (brain/wiki.py falls back to the live site).

    find("Tom Waits")   # [{"title", "extract", "disambiguation"}], best first; [] if none

Looked up by title (the path "Tom_Waits", redirects followed), then Kiwix's
title suggestions. The newest wikipedia_en_all_mini_*.zim in WIKI_DIR is used,
so a new month's file can be dropped in beside the old one.
"""
import glob
import html
import html.parser
import os
import re
import threading

WIKI_DIR = os.path.expanduser("~/patti/wiki")
MIN_PARA = 40           # a first "paragraph" shorter than this is a stub or a coordinate line: skip it

_lock = threading.Lock()
_archive = None


def archive():
    global _archive
    with _lock:
        if _archive is None:
            files = sorted(glob.glob(os.path.join(WIKI_DIR, "wikipedia_en_all_mini_*.zim")))
            if not files:
                return None
            from libzim.reader import Archive
            try:
                _archive = Archive(files[-1])
            except RuntimeError:        # (still downloading: not a whole ZIM yet)
                return None
        return _archive


class _Paras(html.parser.HTMLParser):
    """The text of each top-level <p>, skipping tables, infoboxes, notes and styles."""
    SKIP = {"table", "style", "script", "sup", "figure"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras, self.cur, self.in_p, self.skip = [], [], 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "p":
            self.in_p += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "p" and self.in_p:
            self.in_p -= 1
            if not self.in_p:
                text = re.sub(r"\s+", " ", "".join(self.cur)).strip()
                if text:
                    self.paras.append(text)
                self.cur = []

    def handle_data(self, data):
        if self.in_p and not self.skip:
            self.cur.append(data)


def opening(page_html):
    p = _Paras()
    p.feed(page_html)
    paras = [re.sub(r"\[\d+\]|\[[a-z]\]|\[note \d+\]", "", t) for t in p.paras]
    paras = [t for t in paras if len(t) >= MIN_PARA]
    return paras[0] if paras else ""


def _entry(a, path):
    try:
        e = a.get_entry_by_path(path)
    except KeyError:
        return None
    for _ in range(5):
        if not e.is_redirect:
            return e
        e = e.get_redirect_entry()
    return None


def _found(e):
    page = bytes(e.get_item().content).decode(errors="replace")
    text = opening(page)
    if not text:
        return None
    # A page listing what a name may mean: "Mercury may (also) refer to:", or a lead that's just "...:"
    dis = (bool(re.search(r"\b(?:may|can|might)(?: also)? refer to\b", text[:300])) or text.rstrip().endswith(":")
           or 'class="dmbox' in page or "disambiguation" in e.title.lower())
    return {"title": e.title, "extract": html.unescape(text), "disambiguation": dis}


def find(query, k=3):
    a = archive()
    if a is None:
        return []
    q = query.strip()
    tries = [q.replace(" ", "_"), (q[:1].upper() + q[1:]).replace(" ", "_"),
             q.title().replace(" ", "_"), re.sub(r"^(the|a|an)\s+", "", q, flags=re.I).capitalize().replace(" ", "_")]
    out, seen = [], set()
    for path in tries:
        e = _entry(a, path)
        if e and e.path not in seen:
            seen.add(e.path)
            f = _found(e)
            if f:
                out.append(f)
                break
    if not out:
        from libzim.suggestion import SuggestionSearcher
        for path in SuggestionSearcher(a).suggest(q).get_results(0, k):
            e = _entry(a, path)
            if e and e.path not in seen:
                seen.add(e.path)
                f = _found(e)
                if f:
                    out.append(f)
    return out[:k]
