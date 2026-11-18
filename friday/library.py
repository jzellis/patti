#!/usr/bin/env python3
"""On friday: Josh's books, searchable by what they mean and by their words,
for Patti ("what do my books say about ...?"). Run with ~/patti/venv's python.

    library.py index            # (re)index the Calibre library: new and changed books only
    library.py search "how buildings change over time"
    library.py books "taltos"     # find a book by title or author (its id)
    library.py sittings 42        # how she'd read that book: chapter by chapter

Reads the Calibre library (LIBRARY: its metadata.db for titles and authors,
the files beside it). Each book's text, chapter by chapter (epub: in reading
order; mobi, azw3, docx: converted to epub by Calibre's ebook-convert, into
CONVERTED; pdf: pdftotext, a page at a time; txt as it is), is split into
passages of about CHUNK_WORDS words, overlapping by OVERLAP_WORDS, and kept in
INDEX (SQLite): the passage, its book and chapter, its meaning (bge-small,
as /embed), and a full-text index (FTS5) for exact names and terms.

find(question): the passages most like it by meaning, and the best by words,
merged (reciprocal rank fusion), then the best FIND_RERANK read alongside the
question (bge-reranker-base, as /rerank) to put the ones that answer it
first. ask_claude.py serves it as /library.

Reading (Patti reads a book): books(query) finds one by title or author;
sittings(id) splits it into chapter-sized sittings; start/next_sitting/
done_sitting keep her place and her notes in READING (one book "current"),
read by ask_claude.py's /read and folded into what's true about her for
/understand (about_reading).

The books are only data: nothing in them is run, followed or fetched.
"""
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree

import numpy as np

LIBRARY = os.path.expanduser("~/books/library")
INDEX = os.path.expanduser("~/books/index.db")
CONVERTED = os.path.expanduser("~/books/converted")
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RERANK_MODEL = "BAAI/bge-reranker-base"
EMBED_CACHE = os.path.expanduser("~/.cache/fastembed")
CHUNK_WORDS = 300
OVERLAP_WORDS = 50
MIN_WORDS = 40          # shorter chapters (title pages, copyright) aren't worth a passage
BATCH = 64
FIND_EACH = 40          # candidates from meaning, and from words
FIND_RERANK = 20
CONVERT = ("mobi", "azw3", "azw", "docx", "rtf", "lit", "fb2")
PREFER = ("epub",) + CONVERT + ("pdf", "txt")

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (id INTEGER PRIMARY KEY, title TEXT, authors TEXT, file TEXT, mtime REAL,
                                  chunks INTEGER);
CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, book INTEGER, chapter TEXT, seq INTEGER, text TEXT,
                                   vec BLOB);
CREATE INDEX IF NOT EXISTS chunks_book ON chunks (book);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5 (text, content='chunks', content_rowid='id');
"""


# ---------- reading books ----------
class _Text(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.heading, self.in_h = [], 0, None, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.in_h = True

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        if tag in ("h1", "h2", "h3"):
            self.in_h = False

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_h and not self.heading and data.strip():
            self.heading = data.strip()[:120]
        self.out.append(data)


def _xhtml_text(raw):
    p = _Text()
    try:
        p.feed(raw.decode("utf-8", "replace"))
    except Exception:       # (a broken page: whatever it got)
        pass
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip(), p.heading


def epub_chapters(path):
    """[(chapter title, text)] in reading order."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        try:
            container = ElementTree.fromstring(z.read("META-INF/container.xml"))
            opf_path = next(e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile"))
            opf = ElementTree.fromstring(z.read(opf_path))
        except (KeyError, StopIteration, ElementTree.ParseError):
            opf_path, opf = None, None
        order = []
        if opf is not None:
            base = os.path.dirname(opf_path)
            items = {e.get("id"): e.get("href") for e in opf.iter() if e.tag.endswith("item")}
            for e in opf.iter():
                if e.tag.endswith("itemref") and items.get(e.get("idref")):
                    href = os.path.normpath(os.path.join(base, html.unescape(items[e.get("idref")]).split("#")[0]))
                    if href in names:
                        order.append(href)
        if not order:
            order = sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))
        toc = _toc(z, names)
        out, last = [], None
        for n in order:
            if z.getinfo(n).file_size > 20_000_000:
                continue
            text, heading = _xhtml_text(z.read(n))
            last = toc.get(n) or heading or last
            if text:
                out.append((last or "the start", text))
        return out


def _toc(z, names):
    """{file in the epub: its title in the table of contents} (NCX, or the EPUB 3 nav)."""
    out = {}
    for n in names:
        low = n.lower()
        if not (low.endswith(".ncx") or ("nav" in os.path.basename(low) and low.endswith((".xhtml", ".html")))):
            continue
        try:
            raw = z.read(n).decode("utf-8", "replace")
        except KeyError:
            continue
        base = os.path.dirname(n)
        if low.endswith(".ncx"):
            pairs = re.findall(r"<navLabel>\s*<text>(.*?)</text>.*?<content[^>]*src=\"([^\"]+)\"", raw, re.S)
        else:
            pairs = [(re.sub(r"<[^>]+>", "", t), h) for h, t in re.findall(r"<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",
                                                                       raw, re.S)]
        for label, href in pairs:
            f = os.path.normpath(os.path.join(base, html.unescape(href).split("#")[0]))
            label = html.unescape(re.sub(r"\s+", " ", label)).strip()[:120]
            if f in names and label and f not in out:
                out[f] = label
    return out


def pdf_chapters(path):
    r = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", path, "-"], capture_output=True, timeout=600)
    pages = r.stdout.decode("utf-8", "replace").split("\f")
    return [("page %d" % (i + 1), re.sub(r"[ \t]+", " ", p).strip()) for i, p in enumerate(pages) if p.strip()]


def converted(path):
    """A mobi (etc.) as an epub, made once (ebook-convert)."""
    os.makedirs(CONVERTED, exist_ok=True)
    out = os.path.join(CONVERTED, hashlib.sha1(path.encode()).hexdigest()[:16] + ".epub")
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(path):
        subprocess.run(["ebook-convert", path, out], capture_output=True, timeout=900,
                       env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    return out if os.path.exists(out) else None


def chapters(path):
    ext = path.rsplit(".", 1)[-1].lower()
    if ext == "epub":
        return epub_chapters(path)
    if ext in CONVERT:
        e = converted(path)
        return epub_chapters(e) if e else []
    if ext == "pdf":
        return pdf_chapters(path)
    if ext == "txt":
        with open(path, encoding="utf-8", errors="replace") as f:
            return [("text", f.read())]
    return []


WORDY = re.compile(r"^[\"'(\u201c\u2018]*[A-Za-z][a-z'\u2019-]*[.,;:!?)\"'\u201d\u2019]*$")


def readable(text):
    """Most of it words (not a PDF's scrambled font encoding, or a table of numbers)."""
    toks = text.split()
    return bool(toks) and sum(1 for t in toks if WORDY.match(t)) / len(toks) >= 0.6


def passages(chaps):
    """[(chapter, text)] of about CHUNK_WORDS words, overlapping; readable ones only."""
    out = []
    for title, text in chaps:
        words = text.split()
        if len(words) < MIN_WORDS:
            continue
        step = CHUNK_WORDS - OVERLAP_WORDS
        for i in range(0, max(1, len(words) - OVERLAP_WORDS), step):
            chunk = words[i:i + CHUNK_WORDS]
            if len(chunk) >= MIN_WORDS and readable(" ".join(chunk)):
                out.append((title, " ".join(chunk)))
    return out


# ---------- the library ----------
def calibre_books():
    """[(id, title, authors, best file)] from Calibre's metadata.db."""
    db = sqlite3.connect("file:%s?mode=ro" % os.path.join(LIBRARY, "metadata.db"), uri=True)
    rows = db.execute("SELECT b.id, b.title, b.path, (SELECT group_concat(a.name, ' & ') FROM books_authors_link l "
                      "JOIN authors a ON a.id = l.author WHERE l.book = b.id) FROM books b").fetchall()
    out = []
    for bid, title, path, authors in rows:
        fmts = {f.lower(): name for f, name in db.execute("SELECT format, name FROM data WHERE book=?", (bid,))}
        for ext in PREFER:
            if ext in fmts:
                out.append((bid, title, authors or "", os.path.join(LIBRARY, path, "%s.%s" % (fmts[ext], ext))))
                break
    return out


_embedder = _reranker = None


def embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(EMBED_MODEL, cache_dir=EMBED_CACHE)
    return _embedder


def reranker():
    global _reranker
    if _reranker is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _reranker = TextCrossEncoder(RERANK_MODEL, cache_dir=EMBED_CACHE)
    return _reranker


def connect():
    db = sqlite3.connect(INDEX, check_same_thread=False)
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    return db


def index(log=print):
    db = connect()
    have = {r[0]: (r[1], r[2]) for r in db.execute("SELECT id, file, mtime FROM books")}
    books = calibre_books()
    gone = set(have) - {b[0] for b in books}
    for bid in gone:
        _drop(db, bid)
    t0, done = time.time(), 0
    for n, (bid, title, authors, path) in enumerate(books):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if have.get(bid) == (path, mtime):
            continue
        _drop(db, bid)
        try:
            parts = passages(chapters(path))
        except Exception as e:      # (a broken book: skip it, carry on)
            log("  skipped %s: %s" % (title, e))
            parts = []
        vecs = []
        for i in range(0, len(parts), BATCH):
            vecs += list(embedder().embed([t for _, t in parts[i:i + BATCH]]))
        for seq, ((chap, text), v) in enumerate(zip(parts, vecs)):
            cur = db.execute("INSERT INTO chunks (book, chapter, seq, text, vec) VALUES (?,?,?,?,?)",
                             (bid, chap, seq, text, np.asarray(v, np.float32).tobytes()))
            db.execute("INSERT INTO chunks_fts (rowid, text) VALUES (?,?)", (cur.lastrowid, text))
        db.execute("INSERT OR REPLACE INTO books VALUES (?,?,?,?,?,?)", (bid, title, authors, path, mtime, len(parts)))
        db.commit()
        done += 1
        log("%d/%d %s (%s): %d passages, %.0f s so far" % (n + 1, len(books), title, path.rsplit(".", 1)[-1],
                                                           len(parts), time.time() - t0))
    log("indexed %d book(s), dropped %d, in %.0f s" % (done, len(gone), time.time() - t0))


def _drop(db, bid):
    for (cid,) in db.execute("SELECT id FROM chunks WHERE book=?", (bid,)).fetchall():
        db.execute("INSERT INTO chunks_fts (chunks_fts, rowid, text) SELECT 'delete', id, text FROM chunks WHERE id=?",
                   (cid,))
    db.execute("DELETE FROM chunks WHERE book=?", (bid,))
    db.execute("DELETE FROM books WHERE id=?", (bid,))
    db.commit()


# ---------- finding ----------
class Finder:
    """Loaded once (the meanings of every passage, in memory)."""

    def __init__(self):
        self.db = connect()
        self.loaded = 0.0
        self._load()

    def _load(self):
        rows = self.db.execute("SELECT id, vec FROM chunks").fetchall()
        self.ids = np.array([r[0] for r in rows], np.int64)
        self.vecs = np.frombuffer(b"".join(r[1] for r in rows), np.float32).reshape(len(rows), -1) \
            if rows else np.zeros((0, 384), np.float32)
        self.loaded = os.path.getmtime(INDEX)

    def find(self, question, k=5):
        """[{title, authors, chapter, text, score}], best first."""
        if os.path.getmtime(INDEX) > self.loaded + 60:     # (reindexed since)
            self._load()
        if not len(self.ids):
            return []
        q = np.asarray(next(iter(embedder().embed([question]))), np.float32)
        by_meaning = self.ids[np.argsort(-(self.vecs @ q))[:FIND_EACH]].tolist()
        words = " OR ".join('"%s"' % w for w in re.findall(r"[A-Za-z0-9']{3,}", question)[:12])
        by_words = [r[0] for r in self.db.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?", (words, FIND_EACH))] \
            if words else []
        fused = {}
        for ranked in (by_meaning, by_words):
            for r, cid in enumerate(ranked):
                fused[cid] = fused.get(cid, 0) + 1 / (60 + r)
        best = sorted(fused, key=lambda c: -fused[c])[:FIND_RERANK]
        rows = {r[0]: r[1:] for r in self.db.execute(
            "SELECT c.id, c.text, c.chapter, b.title, b.authors FROM chunks c JOIN books b ON b.id = c.book "
            "WHERE c.id IN (%s)" % ",".join("?" * len(best)), best)}
        scores = list(reranker().rerank(question, [rows[c][0] for c in best]))
        out = [{"title": rows[c][2], "authors": rows[c][3], "chapter": rows[c][1], "text": rows[c][0],
                "score": round(float(s), 3)} for c, s in zip(best, scores)]
        out.sort(key=lambda p: -p["score"])
        return out[:k]



# ---------- reading (Patti reads a book, a sitting at a time) ----------
READING = os.path.expanduser("~/books/reading.json")
SITTING_WORDS = 9000        # a chapter, or this much of a long one
SHORT_WORDS = 1500          # chapters shorter than this are read along with the next
NOTES_KEPT = 8              # earlier sittings' notes given back with the next one (the story so far)
_read_lock = threading.Lock()


def _norm(s):
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split()


def books(query, n=5):
    """The books in the index most like a title or author ("taltos", "the anne rice one"):
    [{id, title, authors, score}], best first (score 0-1)."""
    import difflib
    want = [w for w in _norm(query) if w not in ("the", "a", "an", "by", "book", "novel", "one")]
    if not want:
        return []
    db = connect()
    out = []
    for bid, title, authors, chunks in db.execute("SELECT id, title, authors, chunks FROM books WHERE chunks > 0"):
        words = _norm(title) + _norm(authors)
        if not words:
            continue
        hit = sum(1 for w in want if w in words or any(difflib.SequenceMatcher(None, w, x).ratio() > 0.85
                                                       for x in words)) / len(want)
        whole = difflib.SequenceMatcher(None, " ".join(want), " ".join(_norm(title))).ratio()
        score = max(hit * 0.9 + whole * 0.1, whole)
        if score >= 0.5:
            out.append({"id": bid, "title": title, "authors": authors, "score": round(score, 3)})
    out.sort(key=lambda b: -b["score"])
    return out[:n]


def _state():
    try:
        with open(READING) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"current": None, "books": {}}


def _save_state(st):
    tmp = READING + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, READING)


def sittings(bid):
    """The book (id) split into sittings: [(chapter title, text)], the front and back matter that's too
    short left out, short chapters joined to the next, long ones split."""
    db = connect()
    row = db.execute("SELECT file FROM books WHERE id=?", (bid,)).fetchone()
    if not row:
        return []
    out, held = [], []
    for title, text in chapters(row[0]):
        words = text.split()
        if len(words) < MIN_WORDS or not readable(" ".join(words[:400])):
            continue
        held.append((title, words))
        if sum(len(w) for _, w in held) < (SITTING_WORDS * 2 // 3 if title.startswith("page ") else SHORT_WORDS):
            continue
        title = held[0][0] if len(held) == 1 else "%s to %s" % (held[0][0], held[-1][0])
        words = [x for _, w in held for x in w]
        held = []
        parts = max(1, round(len(words) / SITTING_WORDS))
        size = -(-len(words) // parts)
        for i in range(parts):
            out.append((title if parts == 1 else "%s (part %d of %d)" % (title, i + 1, parts),
                        " ".join(words[i * size:(i + 1) * size])))
    if held:
        out.append((held[0][0], " ".join(x for _, w in held for x in w)))
    return out


def reading(bid=None):
    """Where she's got to: {id, title, authors, at, of, notes: [{chapter, notes, thought, question, at}],
    started, finished} for that book (or the one she's reading), or None."""
    st = _state()
    bid = str(bid or st.get("current") or "")
    return dict(st["books"][bid], id=int(bid)) if bid in st["books"] else None


def shelf():
    """Every book she's read or started: [{id, title, authors, at, of, finished}], latest first."""
    st = _state()
    out = [dict(id=int(k), **{x: b.get(x) for x in ("title", "authors", "at", "of", "started", "finished")})
           for k, b in st["books"].items()]
    return sorted(out, key=lambda b: -(b.get("finished") or b.get("started") or 0))


def start(bid, by=None):
    """She's reading this one now (from where she left it, if she'd started it)."""
    db = connect()
    row = db.execute("SELECT title, authors FROM books WHERE id=?", (bid,)).fetchone()
    if not row:
        raise KeyError("no book %s" % bid)
    with _read_lock:
        st = _state()
        b = st["books"].setdefault(str(bid), {"title": row[0], "authors": row[1], "at": 0, "of": None,
                                              "notes": [], "started": time.time(), "finished": None, "by": by})
        if b["of"] is None:
            b["of"] = len(sittings(bid))
        st["current"] = bid
        st["queue"] = [x for x in st.get("queue") or [] if x != bid]
        _save_state(st)
    return reading(bid)


def want(bid):
    """She wants to read this one: next, after the one she's reading (her list, oldest first). Started now
    if she isn't reading anything. Her state."""
    with _read_lock:
        st = _state()
        q = st.setdefault("queue", [])
        b = st["books"].get(str(bid))
        if bid not in q and not (b and b.get("finished")) and st.get("current") != bid:
            q.append(bid)
        _save_state(st)
        busy = st.get("current") is not None
    if not busy:
        _next_on_list()
    return _state()


def _next_on_list():
    """Done with one (or not reading): the next on her list becomes the one she's reading."""
    with _read_lock:
        st = _state()
        q = st.get("queue") or []
    while q:
        bid = q.pop(0)
        with _read_lock:
            st = _state()
            st["queue"] = q
            _save_state(st)
        try:
            return start(bid)
        except KeyError:
            continue
    return None


def wanted():
    """Her list: [{id, title, authors}], next first."""
    db = connect()
    out = []
    for bid in _state().get("queue") or []:
        row = db.execute("SELECT title, authors FROM books WHERE id=?", (bid,)).fetchone()
        if row:
            out.append({"id": bid, "title": row[0], "authors": row[1]})
    return out


def stop():
    with _read_lock:
        st = _state()
        st["current"] = None
        _save_state(st)


def next_sitting(bid=None):
    """(book state, chapter title, text, sitting number) for the next sitting of that book (or the current
    one), or None if she's finished it / isn't reading one."""
    b = reading(bid)
    if not b or b["finished"]:
        return None
    s = sittings(b["id"])
    if b["at"] >= len(s):
        return None
    title, text = s[b["at"]]
    return b, title, text, b["at"] + 1


def done_sitting(bid, chapter, got):
    """Her notes on the sitting she's just read: kept, and her place moved on."""
    with _read_lock:
        st = _state()
        b = st["books"][str(bid)]
        b["notes"].append({"chapter": chapter, "notes": got.get("notes", ""), "thought": got.get("thought", ""),
                           "question": got.get("question", ""), "pull": got.get("pull"), "felt": got.get("felt") or {},
                           "felt_for": got.get("felt_for") or "", "at": time.time()})
        b["at"] += 1
        b["pull"], b["last"] = got.get("pull"), time.time()
        if b["of"] is not None and b["at"] >= b["of"]:
            b["finished"] = time.time()
            if st.get("current") == bid:
                st["current"] = None
        _save_state(st)
        out = dict(b, id=bid)
    if out["finished"]:
        nxt = _next_on_list()
        out["next"] = nxt and nxt["title"]
    return out


def linked(bid, links, interest):
    """What the sitting she's just read reminded her of (["how", ...]), and how much it caught her interest
    (0..): kept with her notes on it, and added up for the book (what she's liked reading)."""
    with _read_lock:
        st = _state()
        b = st["books"].get(str(bid))
        if not b or not b["notes"]:
            return
        b["notes"][-1]["links"] = links
        b["interest"] = round(b.get("interest", 0) + interest, 3)
        _save_state(st)


FELT_WORDS = {"tension": "tense and worried", "warmth": "warm", "sadness": "sad", "humour": "amused",
              "wonder": "full of wonder", "relief": "relieved"}


def _ago(t):
    m = (time.time() - t) / 60
    return "just now" if m < 15 else "%d minutes ago" % m if m < 90 else "%d hours ago" % (m / 60) if m < 36 * 60 \
        else time.strftime("on %-d %B", time.localtime(t))


def about_reading():
    """Lines for what's true about her now (ask_claude.py, understand): what she's reading and what she
    made of it so far, and what she's read lately."""
    st = _state()
    lines = []
    b = reading()
    if b:
        lines.append("She's reading %s by %s (one of Josh's books), %d of %d sittings in." % (
            b["title"], b["authors"], b["at"], b["of"] or 0))
        pull = b.get("pull")
        if pull is not None:
            lines.append("She's finding it %s." % ("gripping: she can't wait to read on" if pull >= 0.7 else
                                                  "slow going" if pull <= 0.3 else "good"))
        for n in b["notes"][-3:]:
            lines.append("In %s, %s: %s Her thought: %s" % (b["title"], n["chapter"], n["notes"][:300],
                                                            n["thought"][:200]))
            felt = [FELT_WORDS[k] for k, v in sorted((n.get("felt") or {}).items(), key=lambda kv: -kv[1])
                    if v >= 0.4 and k in FELT_WORDS]
            if felt:
                lines.append("Reading that bit (%s), she felt %s%s." % (
                    _ago(n["at"]), " and ".join(felt[:3]), (" with " + n["felt_for"]) if n.get("felt_for") else ""))
            for how in (n.get("links") or [])[:2]:
                lines.append("It reminded her: %s" % how[:200])
    if wanted():
        lines.append("Next on her reading list: %s." % "; ".join("%s by %s" % (w["title"], w["authors"])
                                                             for w in wanted()[:4]))
    for f in shelf()[:4]:
        if f["finished"]:
            last = st["books"][str(f["id"])]["notes"][-1:] or [{}]
            lines.append("She finished reading %s by %s on %s. Her last thought on it: %s" % (
                f["title"], f["authors"], time.strftime("%-d %B %Y", time.localtime(f["finished"])),
                last[0].get("thought", "")[:200]))
    return lines

if __name__ == "__main__":
    if sys.argv[1:2] == ["index"]:
        index()
    elif sys.argv[1:2] == ["search"] and len(sys.argv) > 2:
        for p in Finder().find(" ".join(sys.argv[2:])):
            print("%.2f  %s (%s), %s\n      %s...\n" % (p["score"], p["title"], p["authors"], p["chapter"],
                                                      p["text"][:240]))
    elif sys.argv[1:2] == ["books"] and len(sys.argv) > 2:
        for b in books(" ".join(sys.argv[2:])):
            print("%5d  %.2f  %s (%s)" % (b["id"], b["score"], b["title"], b["authors"]))
    elif sys.argv[1:2] == ["sittings"] and len(sys.argv) == 3:
        for i, (t, text) in enumerate(sittings(int(sys.argv[2])), 1):
            print("%3d  %6d words  %s" % (i, len(text.split()), t))
    else:
        sys.exit(__doc__)
