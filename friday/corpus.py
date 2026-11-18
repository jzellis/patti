#!/usr/bin/env python3
"""On friday: Patti's corpus. Every time Claude thinks for her (ask_claude.py:
understanding what's said to her, answering, reading, looking at photos...)
is kept here: the instructions it had, what it was given, what it answered.
That's two things: a running count of how much she leans on Claude, and,
in time, the training set for her own model (a small one that learns to do
what Claude did for her, from her own life), when she gets hardware for it.

    python3 corpus.py stats [--weeks 8]       # her Claude calls by kind, and how what's said to her got answered
    python3 corpus.py export understand > understand.jsonl
                                              # one kind, as chat examples (system, user, assistant) to train on

Kept in DIR (private: household conversations), a file a month:
  calls-YYYY-MM.jsonl  {"ts", "kind", "model", "system": prompt id, "input", "output" (Claude's raw answer:
                        the training target), "ok", "ms", "usage": {input/output/cache tokens}, "cost",
                        "meta"}
  prompts/<id>.txt     each different set of instructions, once (they're long, and the same every time)
  tally-YYYY-MM.jsonl  {"ts", "how"}: what happened to each thing said to her (brain/server.py,
                        not_a_command): "recall" (her own memory), "wikipedia", "thought" (worked out from
                        what she's been told), "books", "claude" (she had to ask), "asked" (she asked them),
                        "understood" (conversation), ...
Backed up every night to Google Drive, My Drive/Patti/corpus (patti-corpus-backup.timer: rclone copy,
never deletes there).
A chapter she reads isn't copied in (it's in the library: meta has the book and sitting, and the text's
hash); a photo isn't kept at all (by design: meta has what she was told about it).
"""
import argparse
import collections
import hashlib
import json
import os
import sys
import threading
import time

DIR = os.path.expanduser("~/patti/corpus")
_lock = threading.Lock()

# What answered something said to her (the tally): from what she has, or from Claude
OWN = ("recall", "wikipedia", "thought", "books", "own")
KNOWLEDGE = OWN + ("claude", "asked")


def _month(prefix, t=None):
    return os.path.join(DIR, "%s-%s.jsonl" % (prefix, time.strftime("%Y-%m", time.localtime(t))))


def _append(path, obj):
    os.makedirs(os.path.join(DIR, "prompts"), mode=0o700, exist_ok=True)
    os.chmod(DIR, 0o700)
    with _lock, open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def prompt_id(system):
    pid = hashlib.sha1(system.encode()).hexdigest()[:16]
    path = os.path.join(DIR, "prompts", pid + ".txt")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path + ".tmp", "w") as f:
            f.write(system)
        os.replace(path + ".tmp", path)
    return pid


def record(kind, cmd, given, stdout, ms, meta=None):
    """One call to Claude (ask_claude.py: cmd is the claude -p command line, given what went in on stdin,
    stdout its JSON). Never raises: keeping it mustn't break her."""
    try:
        model = cmd[cmd.index("--model") + 1] if "--model" in cmd else None
        system = cmd[cmd.index("--system-prompt") + 1] if "--system-prompt" in cmd else ""
        try:
            out = json.loads(stdout)
        except ValueError:
            out = {"result": stdout, "is_error": True}
        u = out.get("usage") or {}
        _append(_month("calls"), {
            "ts": time.time(), "kind": kind, "model": model, "system": prompt_id(system) if system else None,
            "input": given, "output": out.get("result"), "ok": not out.get("is_error"), "ms": int(ms),
            "usage": {k: u.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                            "cache_creation_input_tokens") if u.get(k) is not None},
            "cost": out.get("total_cost_usd"), "meta": meta or {}})
    except Exception as e:
        print("corpus: couldn't keep a %s call (%r)" % (kind, e), file=sys.stderr)


def tally(how, who=None):
    try:
        _append(_month("tally"), {"ts": time.time(), "how": str(how)[:40]})
    except Exception as e:
        print("corpus: couldn't tally (%r)" % e, file=sys.stderr)


def _read(prefix, since):
    if not os.path.isdir(DIR):
        return
    for name in sorted(os.listdir(DIR)):
        if name.startswith(prefix + "-") and name.endswith(".jsonl"):
            with open(os.path.join(DIR, name)) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("ts", 0) >= since:
                        yield r


def summary(days=7):
    """{"calls": {kind: n}, "tally": {how: n}, "own": n, "claude": n, "cost": $}: the last few days."""
    since = time.time() - days * 86400
    calls, tal, cost = collections.Counter(), collections.Counter(), 0.0
    for r in _read("calls", since):
        calls[r["kind"]] += 1
        cost += r.get("cost") or 0
    for r in _read("tally", since):
        tal[r["how"]] += 1
    return {"calls": dict(calls), "tally": dict(tal), "own": sum(tal[h] for h in OWN), "claude": tal["claude"],
            "cost": round(cost, 2)}


def about_line():
    """For what's true about her now (ask_claude.py, understand): how much she leans on Claude lately."""
    s = summary(7)
    asked = s["own"] + s["claude"]
    if asked < 3:
        return None
    return ("This past week, of %d questions about the world she was asked, she answered %d herself (from memory, "
            "Wikipedia, Josh's books or working it out) and had to ask Claude %d times. (Claude also helps her "
            "understand and talk: that's her way of thinking for now, till she has her own.)"
            % (asked, s["own"], s["claude"]))


def stats(weeks=8):
    now = time.time()
    rows = []
    kinds, hows = set(), set()
    for w in range(weeks - 1, -1, -1):
        a, b = now - (w + 1) * 7 * 86400, now - w * 7 * 86400
        c = collections.Counter(r["kind"] for r in _read("calls", a) if r["ts"] < b)
        t = collections.Counter(r["how"] for r in _read("tally", a) if r["ts"] < b)
        kinds |= set(c)
        hows |= set(t)
        rows.append((time.strftime("%d %b", time.localtime(a)), c, t))
    kinds, hows = sorted(kinds), sorted(hows)
    print("Claude calls a week, by kind (what Claude did for her):")
    print("  week of  " + "".join("%11s" % k[:10] for k in kinds) + "      total")
    for label, c, _ in rows:
        print("  %-8s" % label + "".join("%11d" % c[k] for k in kinds) + "%11d" % sum(c.values()))
    print("\nWhat was said to her, and what answered it:")
    print("  week of  " + "".join("%11s" % h[:10] for h in hows) + "   own/asked")
    for label, _, t in rows:
        own, cl = sum(t[h] for h in OWN), t["claude"]
        print("  %-8s" % label + "".join("%11d" % t[h] for h in hows)
              + ("   %3d%%" % (100 * own / (own + cl)) if own + cl else "      -"))
    print("\n  own/asked: of the questions about the world, how many she answered without asking Claude")


def export(kind):
    """One kind of call, as chat examples to train on (system, user, assistant), to stdout."""
    prompts = {}
    n = 0
    for r in _read("calls", 0):
        if r["kind"] != kind or not r.get("ok") or not r.get("output"):
            continue
        pid = r.get("system")
        if pid and pid not in prompts:
            with open(os.path.join(DIR, "prompts", pid + ".txt")) as f:
                prompts[pid] = f.read()
        msgs = ([{"role": "system", "content": prompts[pid]}] if pid else []) + [
            {"role": "user", "content": r["input"]}, {"role": "assistant", "content": r["output"]}]
        print(json.dumps({"messages": msgs, "meta": r.get("meta") or {}}, ensure_ascii=False))
        n += 1
    print("corpus: %d %s example(s)" % (n, kind), file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stats")
    st.add_argument("--weeks", type=int, default=8)
    ex = sub.add_parser("export")
    ex.add_argument("kind")
    a = ap.parse_args()
    if a.cmd == "stats":
        stats(a.weeks)
    else:
        export(a.kind)
