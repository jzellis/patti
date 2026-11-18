"""Patti's mind without her body: runs on friday, and while the Pi's brain
(server.py) can't be reached, she's on Telegram from here, as herself. The
same code as on the Pi (server.py's chat: her commands, memory, feelings,
Wikipedia, Claude), with no hardware. Anything that needs her body, she
says she can't do till she's back in it.

Only one of her is ever awake on Telegram: this one takes the bot only
after the Pi's been unreachable for DOWN_S, and lets go as soon as it
answers again.

Her memory here is a copy, from the Pi's newest hourly backup in
~/patti/backups (pi/backup/backup.py), taken when she wakes here. What she
learns here stays in friday's copy (mind-state.json "unmerged_since") and
isn't overwritten by the next backup till it's merged back into the Pi's.

Merging, like git: the backup she woke from is the common ancestor; what
she wrote here since "unmerged_since" is friday's branch. When the Pi's
back, she lets go of Telegram, then sends the branch to the Pi's brain
(server.py /merge, with the key they share), which folds it into her memory
there (memory.py merge: logs combine, nothing twice; limbic.py merge:
fondness three-way). Only when that's done is "unmerged_since" cleared
("merged_at"); till then she tries again every CHECK_S. A backup older
than the merge is never restored over friday's copy (it wouldn't have
friday's branch in it). If friday restarts while she's awake here (the
Pi still off), she wakes again at once.

    python3 mind.py              # the watcher (systemd: friday/patti-mind.service)
    python3 mind.py --now        # wake here at once, whatever the Pi's doing (testing; Pi must be off)
    python3 mind.py --restore    # just refresh friday's copy from the newest backup
"""
import argparse
import asyncio
import contextlib
import json
import os
import random
import shutil
import socket
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PI = ("100.64.0.20", 8080)     # her brain's page, over Tailscale
CHECK_S = 30
DOWN_S = 180                     # unreachable this long: she wakes here
BACKUPS = os.path.expanduser("~/patti/backups")
CONF = os.path.expanduser("~/.config/robotpattinson")
SHARE = os.path.expanduser("~/.local/share/robotpattinson")
STATE = os.path.join(SHARE, "mind-state.json")
# backup member -> where it goes here
RESTORE = {
    "databases/memory.db": os.path.join(SHARE, "memory.db"),
    "databases/users.db": os.path.join(CONF, "users.db"),
    "data/limbic.json": os.path.join(SHARE, "limbic.json"),
    "data/now.json": os.path.join(SHARE, "now.json"),
    "config/telegram-key": os.path.join(CONF, "telegram-key"),
    "config/telegram.json": os.path.join(CONF, "telegram.json"),
    "config/claude-key": os.path.join(CONF, "claude-key"),
    "config/weather.json": os.path.join(CONF, "weather.json"),
    "config/places.json": os.path.join(CONF, "places.json"),
}

# Commands she can still do in her head (the rest need her body)
IN_HER_HEAD = {"how", "thanks", "praise", "goodnight", "time", "weather", "lastseen"}
NO_BODY = ["I can't do that right now: my body's switched off, I'm only in my head (on friday).",
           "Not without my body, sorry. It's switched off, so I'm just thinking on friday for now.",
           "My body's off at the moment, so I can't. Ask me again when I'm back in it?"]
HELLO = "(My body's switched off right now, so I'm only in my head, on friday. I can chat and look at photos you send, but I can't see the room, hear or move.)"


def log(*a):
    print("mind:", *a, file=sys.stderr, flush=True)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(s):
    os.makedirs(SHARE, exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        json.dump(s, f, indent=1)
    os.replace(STATE + ".tmp", STATE)


def newest_backup():
    try:
        snaps = sorted(f for f in os.listdir(BACKUPS) if f.startswith("snap-") and f.endswith("-data.tar.gz"))
    except OSError:
        return None
    return snaps[-1] if snaps else None


def restore(force=False):
    """Friday's copy of her from the newest backup, unless what she's
    learned here hasn't gone back to the Pi yet (then it's kept)."""
    s = load_state()
    snap = newest_backup()
    if not snap:
        log("no backups in", BACKUPS)
        return s
    if s.get("unmerged_since") and not force:
        log("keeping friday's copy: learned here since %s, not merged back yet"
            % time.ctime(s["unmerged_since"]))
        return s
    if s.get("restored_from") == snap and not force:
        return s
    if (s.get("merged_at") and os.path.getmtime(os.path.join(BACKUPS, snap)) < s["merged_at"] + 60
            and not force):     # (from before the merge: friday's own copy has more in it)
        return s
    os.makedirs(CONF, mode=0o700, exist_ok=True)
    os.makedirs(SHARE, mode=0o700, exist_ok=True)
    names = {"faces": set(), "voices": set()}
    with tarfile.open(os.path.join(BACKUPS, snap)) as t:
        for m in t.getmembers():
            parts = m.name.split("/")
            if m.isdir() and len(parts) == 3 and parts[0] == "data" and parts[1] in names and parts[2]:
                names[parts[1]].add(parts[2])
            dest = RESTORE.get(m.name)
            if dest and m.isfile():
                with t.extractfile(m) as src, open(dest + ".tmp", "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(dest + ".tmp", 0o600)
                os.replace(dest + ".tmp", dest)
    for stale in ("memory.db-wal", "memory.db-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(SHARE, stale))
    s.update(restored_from=snap, faces=sorted(names["faces"]), voices=sorted(names["voices"]))
    save_state(s)
    log("restored from", snap)
    return s


def restore_faces():
    """Her enrolled faces (data/faces in the backup), for photos sent to her.
    Only if friday hasn't got them: they don't change here."""
    dest = os.path.join(SHARE, "faces")
    snap = newest_backup()
    if os.path.isdir(dest) or not snap:
        return
    with tarfile.open(os.path.join(BACKUPS, snap)) as t:
        for m in t.getmembers():
            parts = m.name.split("/")
            if (m.isfile() and len(parts) == 4 and parts[:2] == ["data", "faces"]
                    and all(p and p not in (".", "..") for p in parts[2:])):
                d = os.path.join(dest, parts[2])
                os.makedirs(d, mode=0o700, exist_ok=True)
                with t.extractfile(m) as src, open(os.path.join(d, parts[3]), "wb") as out:
                    shutil.copyfileobj(src, out)
    log("faces restored from", snap)


MERGE_URL = "http://%s:%d/merge" % PI
MERGE_ROWS = 200         # rows per request (the Pi takes up to 1 MB)


def _post_pi(body):
    import urllib.request
    with open(os.path.join(CONF, "claude-key")) as f:
        key = f.read().strip()
    req = urllib.request.Request(MERGE_URL, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def merge_back(app=None):
    """friday's branch (everything since "unmerged_since") into the Pi's memory. True when done
    (or nothing to do); raises if the Pi can't take it yet."""
    s = load_state()
    since = s.get("unmerged_since")
    if not since:
        return True
    import memory, limbic
    mem = app["memory"] if app else memory.Memory()
    if app:
        app["limbic"]._save()
    branch = mem.branch(since - 3600)       # (a little before: anything written as she woke; nothing's added twice)
    sent = {}
    for t, rows in branch.items():
        for i in range(0, len(rows), MERGE_ROWS):
            got = _post_pi({"part": {t: rows[i:i + MERGE_ROWS]}})["merged"]
            sent[t] = sent.get(t, 0) + got.get(t, 0)
    base = {}
    snap = s.get("restored_from")
    if snap and os.path.exists(os.path.join(BACKUPS, snap)):
        with tarfile.open(os.path.join(BACKUPS, snap)) as t, t.extractfile("data/limbic.json") as f:
            base = json.load(f).get("bonds") or {}
    with open(os.path.join(SHARE, "limbic.json")) as f:
        mine = json.load(f)
    _post_pi({"limbic": {"base": base, "bonds": mine.get("bonds") or {}, "last_seen": mine.get("last_seen") or {},
                         "branch": "%s@%s" % (snap, since)}})
    s = load_state()
    s.pop("unmerged_since", None)
    s["merged_at"] = time.time()
    save_state(s)
    log("merged back into the Pi: %s" % (", ".join("%s %d" % kv for kv in sent.items() if kv[1]) or "nothing new"))
    return True


def pi_up():
    try:
        with socket.create_connection(PI, timeout=5):
            return True
    except OSError:
        return False


class Names:
    """Stands in for faces.Who / voices.Voices: just who she knows."""
    def __init__(self, people):
        self.people = list(people)


class Hat:
    """No Sense HAT here."""
    def thinking(self):
        return contextlib.nullcontext()


class Drives:
    """Her own initiative stays on the Pi (laws.py, her questions)."""
    def question_for_reply(self, chat, reply_to):
        return None

    async def reply(self, qid, text, who):
        return "I'll remember to ask about that when I'm back in my body."

    def dnd(self, on):
        pass


class Place:
    words = ssid = None


def make_app(server, s):
    import limbic
    import memory
    import users
    from aiohttp import web
    app = {}
    app["users"] = users.Users()
    app["memory"] = memory.Memory()
    app["limbic"] = limbic.Limbic(app["memory"])
    app["chats"], app["clarify"], app["tgasks"], app["online"] = {}, {}, {}, {}
    app["echo"] = {"lines": None, "quiet": True}
    app["attn"] = {"at": time.monotonic()}
    app["feel"] = {"mood_set": -1e9, "looked": 0.0, "told": 0.0}
    restore_faces()
    try:
        import faces
        known = faces.Who()         # (real face recognition: who's in photos sent to her)
    except (SystemExit, Exception) as e:
        log("faces off (%s)" % e)
        known = Names(s.get("faces", ()))
    app["who"] = {"faces": known, "voices": Names(s.get("voices", ())), "last": None,
                  "present": {}, "face": {}, "seen": {}, "greeted": {}, "stranger_at": -1e9, "asked_at": -1e9,
                  "last_voice": None, "strangers": None, "stranger": None, "voice_asked": {}}
    app["hat"], app["drives"], app["place"] = Hat(), Drives(), Place()
    app["situation"] = ("Her body is switched off right now, so she's only her mind, running on friday (the PC that "
                        "helps her think), chatting by Telegram: she can't see the room, hear or move till it's back on.")
    import news
    app["news"] = news.News(app["memory"])     # (what she's read; the feeds are fetched on the Pi)
    for k in ("speech", "voice", "motors", "camera", "gimbal", "eyes", "timer", "telegram", "chest"):
        app[k] = None

    head_only = server.voice_action

    async def voice_action(app, action, arg, speaker=None):
        if action in IN_HER_HEAD:
            return await head_only(app, action, arg, speaker)
        server.say(app, random.choice(NO_BODY))
    server.voice_action = voice_action

    async def say_post(request):
        return web.json_response({"error": "I can't say it out loud: my body's switched off. Try again when I'm back?"})
    server.say_post = say_post

    told = set()        # chats told she's only in her head, this time
    tg_send = server.tg_send

    async def send(app, chat, text, buttons=None):
        if chat not in told:
            told.add(chat)
            text = HELLO + "\n\n" + text
        return await tg_send(app, chat, text, buttons)
    server.tg_send = send
    app["told"] = told
    return app


OOPS = "Sorry, something went wrong in my head just then (I'm running on friday while my body's off). Try me again?"


async def safely(app, server, chat, work):
    """Never silent: if handling a message fails, she says so."""
    try:
        await work
    except Exception as e:
        import traceback
        log("couldn't answer: %r" % e)
        traceback.print_exc()
        await server.tg_send(app, chat, OOPS)


class Bot:
    """telegram.Telegram's poller, but it can let go (and confirms what it
    handled, so the Pi doesn't answer it again)."""
    def __init__(self, telegram, on_text, on_button, on_photo=None):
        self.t = telegram.Telegram(on_text, on_button, on_photo)
        self.telegram = telegram
        self.on = False
        self.task = None

    async def run(self):
        tg = self.telegram
        while self.on:
            try:
                ups = await asyncio.to_thread(tg.call, "getUpdates", {
                    "offset": self.t.offset, "timeout": 20, "allowed_updates": ["message", "callback_query"]}, 30)
            except (OSError, ValueError, KeyError) as e:
                log("telegram: %s" % e)
                await asyncio.sleep(10)
                continue
            for u in ups:
                self.t.offset = u["update_id"] + 1
                if not self.on:
                    break
                try:
                    await asyncio.to_thread(self.t._handle, u)
                except Exception as e:
                    log("telegram: %r: %s" % (u.get("update_id"), e))
        if self.t.offset:
            with contextlib.suppress(OSError, ValueError, KeyError):
                await asyncio.to_thread(tg.call, "getUpdates", {"offset": self.t.offset, "timeout": 0}, 15)


async def main(now=False):
    s = restore()
    import server       # (after restore: her modules read their files when made)
    import telegram
    if not telegram.ready():
        sys.exit("mind: no Telegram key (restore from a backup first)")
    loop = asyncio.get_running_loop()
    app = None
    bot = None
    down_since = None if not now else -1e9
    if load_state().get("awake") and not await asyncio.to_thread(pi_up):
        down_since = -1e9           # (friday restarted while she was awake here: straight back)
    while True:
        up = False if now else await asyncio.to_thread(pi_up)
        t = time.monotonic()
        if up:
            down_since = None
            if bot and bot.on:
                log("her body's back: letting go of Telegram")
                bot.on = False
                if app.get("readrun"):
                    app["readrun"].cancel()
                await bot.task
                app["limbic"]._save()
                bot = None
                st = load_state()
                st["awake"] = False
                save_state(st)
            if load_state().get("unmerged_since"):
                try:
                    if await asyncio.to_thread(merge_back, app) and app and not (bot and bot.on):
                        app["memory"].close()
                        app = None          # (next time she wakes here: from the Pi's newer backup)
                except Exception as e:      # (the Pi's brain not ready, or old: again next time)
                    log("merge back: not yet (%s)" % e)
        else:
            down_since = down_since if down_since is not None else t
            if not (bot and bot.on) and t - down_since >= DOWN_S:
                if app is None:
                    s = restore()
                    app = make_app(server, s)
                app["told"].clear()
                st = load_state()
                st.setdefault("unmerged_since", time.time())
                st["awake"] = True
                save_state(st)
                app["telegram"] = bot = Bot(
                    telegram,
                    lambda chat, login, text, reply_to: asyncio.run_coroutine_threadsafe(
                        safely(app, server, chat, server.telegram_text(app, chat, login, text, reply_to)), loop).result(),
                    lambda chat, login, data: asyncio.run_coroutine_threadsafe(
                        safely(app, server, chat, server.telegram_button(app, chat, login, data)), loop).result(),
                    lambda chat, login, jpeg, caption: asyncio.run_coroutine_threadsafe(
                        safely(app, server, chat, server.telegram_photo(app, chat, login, jpeg, caption)), loop).result())
                bot.chats = bot.t.chats
                bot.send = bot.t.send
                bot.on = True
                bot.task = asyncio.create_task(bot.run())
                app["readrun"] = asyncio.create_task(server.reading_watch(app))     # (she reads on here, too)
                log(("her body's been unreachable %d s" % (t - down_since)) if down_since > -1e8 else
                    "she was awake here before friday restarted", "- she's awake here, on Telegram")
        if app:
            app["limbic"].tick()
        await asyncio.sleep(CHECK_S)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--now", action="store_true", help="wake here at once (the Pi must be off)")
    ap.add_argument("--restore", action="store_true", help="refresh friday's copy from the newest backup")
    ap.add_argument("--force", action="store_true", help="with --restore: even over what she learned here")
    a = ap.parse_args()
    if a.restore:
        restore(a.force)
    else:
        try:
            asyncio.run(main(a.now))
        except KeyboardInterrupt:
            pass
