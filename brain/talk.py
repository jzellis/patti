#!/usr/bin/env python3
"""Talk to her from a terminal on the Pi:

    say "Hello, Josh"                   # she says it
    say "[cheeky] Y'alright?"           # stage directions work, as in replies.py
    ask "What's the weather like?"      # a command, as if said to her (no name needed)
    ask set a timer for 10 minutes      # quotes are optional; digits are fine

`ask` takes the same phrases as her voice commands (voice.py), and prints what
she says back. Both go to the brain (server.py) on 127.0.0.1, so rp-brain has
to be running.

Set up, once (links in ~/.local/bin, which is on the PATH):

    ln -s ~/robotpattinson/brain/talk.py ~/.local/bin/say
    ln -s ~/robotpattinson/brain/talk.py ~/.local/bin/ask

or run it as `talk.py say ...` / `talk.py ask ...`.
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:%s/" % os.environ.get("RP_PORT", "8080")


def post(what, text):
    req = urllib.request.Request(URL + what, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except ValueError:
            return {"error": "The brain said %d %s." % (e.code, e.reason)}
    except urllib.error.URLError:
        return {"error": "Can't reach her brain on %s. Is rp-brain running? (systemctl status rp-brain)" % URL}


def main():
    what, args = os.path.basename(sys.argv[0]).removesuffix(".py"), sys.argv[1:]
    if what not in ("say", "ask"):
        what, args = (args[0], args[1:]) if args else ("", [])
    text = " ".join(args).strip()
    if what not in ("say", "ask") or not text:
        sys.exit('say "something for her to say"\nask "one of her commands, like what time is it"')
    out = post(what, text)
    if "error" in out:
        msg = out["error"]
        if out.get("near"):
            msg += " Did you mean: " + "; ".join(out["near"]) + "?"
        sys.exit(msg)
    if what == "ask":
        print("(%s)" % out["action"] if not out["said"] else "\n".join(out["said"]))


if __name__ == "__main__":
    main()
