"""The news, from the feeds Josh reads (FEEDS), for her to talk about. For the
brain (server.py).

    n = news.News(memory)
    n.fetch()                         # blocking: new stories from every feed (every FETCH_S, from server.py)
    n.headlines()                     # [story]: a few of the latest, one a source, not the same story twice
    n.like(vec, k)                    # [story]: the stories most like what that means (claude.embed)

A story: {source, title, summary, link, at (published), vec}. Kept in
memory.db (news) for KEEP_S, then forgotten: news is current, never lasting.
Feeds are only data: their text is said or reasoned over, never followed.
"""
import html
import re
import sys
import time
import urllib.request
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import numpy as np

import claude

FEEDS = {
    "the BBC": "https://feeds.bbci.co.uk/news/rss.xml",
    "the Guardian": "https://www.theguardian.com/uk/rss",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "the New York Times": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
}
AGENT = "Patti-home-robot/1.0 (+https://github.com/jzellis/robotpattinson)"
FETCH_S = 2 * 3600
KEEP_S = 7 * 86400
PER_FEED = 25           # the newest this many of each feed
SAME_STORY = 0.85       # this alike (meaning): the same story from two sources
TIMEOUT_S = 20
EMBED_BATCH = 40        # (friday takes at most 50 at a time)

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (link TEXT PRIMARY KEY, source TEXT, title TEXT, summary TEXT, at REAL, vec BLOB);
CREATE INDEX IF NOT EXISTS news_at ON news (at);
"""


def _plain(text):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _when(item):
    for tag in ("pubDate", "{http://purl.org/dc/elements/1.1/}date"):
        t = item.findtext(tag)
        if t:
            try:
                return parsedate_to_datetime(t).timestamp()
            except (TypeError, ValueError):
                try:
                    return time.mktime(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    pass
    return time.time()


def read(url):
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        root = ElementTree.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title, link = _plain(it.findtext("title")), (it.findtext("link") or "").strip()
        if title and link:
            out.append({"title": title, "link": link, "summary": _plain(it.findtext("description"))[:600],
                        "at": _when(it)})
    out.sort(key=lambda s: -s["at"])
    return out[:PER_FEED]


class News:
    def __init__(self, memory):
        self.mem = memory
        with memory.lock:
            memory.db.executescript(SCHEMA)

    def fetch(self):
        """Blocking. How many new stories."""
        new = []
        for source, url in FEEDS.items():
            try:
                got = read(url)
            except (OSError, ValueError, ElementTree.ParseError) as e:
                print("news: %s: %s" % (source, e), file=sys.stderr)
                continue
            with self.mem.lock:
                have = {r[0] for r in self.mem.db.execute(
                    "SELECT link FROM news WHERE link IN (%s)" % ",".join("?" * len(got)), [s["link"] for s in got])}
            new += [dict(s, source=source) for s in got if s["link"] not in have]
        vecs = []
        for i in range(0, len(new), EMBED_BATCH) if claude.ready() else ():
            got = claude.embed([s["title"] + ". " + s["summary"][:300] for s in new[i:i + EMBED_BATCH]])
            if not got:
                vecs = None
                break
            vecs += got
        with self.mem.lock:
            for i, s in enumerate(new):
                v = np.asarray(vecs[i], np.float32).tobytes() if vecs else None
                self.mem.db.execute("INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?)",
                                    (s["link"], s["source"], s["title"], s["summary"], s["at"], v))
            self.mem.db.execute("DELETE FROM news WHERE at < ?", (time.time() - KEEP_S,))
            self.mem.db.commit()
        print("news: %d new stories" % len(new), file=sys.stderr)
        return len(new)

    def _rows(self, since):
        with self.mem.lock:
            rows = self.mem.db.execute("SELECT source, title, summary, link, at, vec FROM news WHERE at >= ? "
                                       "ORDER BY at DESC", (since,)).fetchall()
        return [{"source": r[0], "title": r[1], "summary": r[2], "link": r[3], "at": r[4],
                 "vec": np.frombuffer(r[5], np.float32) if r[5] else None} for r in rows]

    def headlines(self, n=4, hours=12):
        """The latest few, taking each source in turn, skipping a story already told by another."""
        rows = self._rows(time.time() - hours * 3600)
        by = {}
        for s in rows:
            by.setdefault(s["source"], []).append(s)
        out = []
        while len(out) < n and any(by.values()):
            for src in list(by):
                while by[src]:
                    s = by[src].pop(0)
                    if not any(s["vec"] is not None and o["vec"] is not None and float(s["vec"] @ o["vec"]) >= SAME_STORY
                               for o in out):
                        out.append(s)
                        break
                if len(out) >= n:
                    break
        return out

    def like(self, vec, k=4, floor=0.55, days=7):
        """The stories most like vec (what a question means)."""
        v = np.asarray(vec, np.float32)
        scored = [(float(s["vec"] @ v), s) for s in self._rows(time.time() - days * 86400) if s["vec"] is not None]
        return [dict(s, score=round(sc, 3)) for sc, s in sorted(scored, key=lambda x: -x[0])[:k] if sc >= floor]


if __name__ == "__main__":
    import memory
    n = News(memory.Memory())
    n.fetch()
    for s in n.headlines():
        print("%s: %s" % (s["source"], s["title"]))
