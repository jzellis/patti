"""Telegram: she can message people when they're not in front of her, and
they can chat with her from anywhere, as on her page. For the brain
(server.py).

    t = telegram.Telegram(on_text, on_button, on_photo)  # on_text(chat, login, text, reply_to), on_button(chat, login, data),
                                                         # on_photo(chat, login, jpeg bytes, caption): from its thread
    t.start()
    t.send(chat, "Hello", [("Yes", "y:1"), ("No", "n:1")])     # buttons: (label, data for on_button); the message, or None
    t.send(chat, "Who was it?", reply=True)     # their answer comes back with reply_to: this message's id
    t.chats("jzellis")                          # the Telegram chats linked to that login

"say: dinner's ready" from a linked chat, she says out loud (server.py, TG_SAY).

The bot's key is ~/.config/robotpattinson/telegram-key (from Telegram's
@BotFather; put there by hand, never printed). Only linked chats are
listened to (LINKS: {"chat id": "login"}): anyone else gets nothing
back, and is logged once, so they can be linked:

    python3 telegram.py                 # the bot, and who's messaged it lately
    python3 telegram.py --link 123456 jzellis

Bot chats aren't end-to-end encrypted, so she sends words, not photos.
Photos sent TO her are downloaded (the biggest size up to MAX_PHOTO) and
handed to on_photo; without one, she says she can't see them yet.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

KEY_FILE = os.path.expanduser("~/.config/robotpattinson/telegram-key")
LINKS = os.path.expanduser("~/.config/robotpattinson/telegram.json")
API = "https://api.telegram.org/bot%s/%s"
POLL_S = 50             # long poll
RETRY_S = 30            # after a failure (no internet, say)
MAX_TEXT = 4000         # (Telegram's limit is 4096)
MAX_PHOTO = 5 * 1024 * 1024
FILE_URL = "https://api.telegram.org/file/bot%s/%s"


def ready():
    return os.path.exists(KEY_FILE)


def links():
    try:
        with open(LINKS) as f:
            return {str(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return {}


def link(chat, login):
    got = links()
    got[str(chat)] = login
    os.makedirs(os.path.dirname(LINKS), exist_ok=True)
    with open(LINKS + ".tmp", "w") as f:
        json.dump(got, f, indent=1)
    os.replace(LINKS + ".tmp", LINKS)


def call(method, body=None, timeout=20):
    with open(KEY_FILE) as f:
        key = f.read().strip()
    req = urllib.request.Request(API % (key, method), data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["result"]


def download(file_id):
    """Blocking: a file sent to her (a photo), as bytes."""
    path = call("getFile", {"file_id": file_id})["file_path"]
    with open(KEY_FILE) as f:
        key = f.read().strip()
    with urllib.request.urlopen(FILE_URL % (key, path), timeout=60) as r:
        return r.read(MAX_PHOTO + 1)[:MAX_PHOTO]


class Telegram:
    def __init__(self, on_text, on_button, on_photo=None):
        self.on_text, self.on_button, self.on_photo = on_text, on_button, on_photo
        self.offset = 0
        self.strangers = set()      # unlinked chats already logged

    def start(self):
        threading.Thread(target=self._run, name="telegram", daemon=True).start()

    def chats(self, login):
        return [c for c, who in links().items() if who == login]

    def send(self, chat, text, buttons=None, reply=False):
        """Blocking. The message sent ({"message_id", ...}), or None. reply:
        their phone opens a reply to it, so the answer's tied to it."""
        body = {"chat_id": chat, "text": text[:MAX_TEXT]}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in buttons]]}
        elif reply:
            body["reply_markup"] = {"force_reply": True}
        try:
            return call("sendMessage", body)
        except (OSError, ValueError, KeyError) as e:
            print("telegram: couldn't send (%s)" % e, file=sys.stderr)
            return None

    def _run(self):
        while True:
            try:
                ups = call("getUpdates", {"offset": self.offset, "timeout": POLL_S,
                                          "allowed_updates": ["message", "callback_query"]}, POLL_S + 10)
            except (OSError, ValueError, KeyError) as e:
                print("telegram: %s; again in %d s" % (e, RETRY_S), file=sys.stderr)
                time.sleep(RETRY_S)
                continue
            for u in ups:
                self.offset = u["update_id"] + 1
                try:
                    self._handle(u)
                except Exception as e:      # (one bad message mustn't stop her listening)
                    print("telegram: %r: %s" % (u.get("update_id"), e), file=sys.stderr)

    def _handle(self, u):
        known = links()
        if "callback_query" in u:
            q = u["callback_query"]
            chat = str(q["message"]["chat"]["id"])
            try:
                call("answerCallbackQuery", {"callback_query_id": q["id"]})
            except (OSError, ValueError, KeyError):
                pass
            if chat in known:
                self.on_button(chat, known[chat], q.get("data", ""))
            return
        m = u.get("message") or {}
        chat, text = str(m.get("chat", {}).get("id", "")), (m.get("text") or "").strip()
        if chat not in known:
            if chat not in self.strangers:
                self.strangers.add(chat)
                f = m.get("from", {})
                print("telegram: message from unlinked chat %s (%s %s); link with: python3 telegram.py --link %s LOGIN"
                      % (chat, f.get("first_name", ""), f.get("username", ""), chat), file=sys.stderr)
            return
        photo = [p for p in m.get("photo") or [] if p.get("file_size", 0) <= MAX_PHOTO]
        doc = m.get("document") or {}
        if not photo and str(doc.get("mime_type", "")).startswith("image/") and doc.get("file_size", 0) <= MAX_PHOTO:
            photo = [doc]
        if photo or m.get("photo"):
            if not self.on_photo:
                self.send(chat, "I can't see photos yet, sorry. Tell me about it?")
            elif not photo:
                self.send(chat, "That photo's too big for me, sorry.")
            else:
                self.on_photo(chat, known[chat], download(photo[-1]["file_id"]), (m.get("caption") or "").strip())
            return
        if text and text != "/start":
            self.on_text(chat, known[chat], text, (m.get("reply_to_message") or {}).get("message_id"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--link"] and len(sys.argv) == 4:
        link(sys.argv[2], sys.argv[3])
        print("Linked chat %s to %s." % (sys.argv[2], sys.argv[3]))
        sys.exit()
    me = call("getMe")
    print("Bot: @%s (%s)" % (me["username"], me["first_name"]))
    for u in call("getUpdates", {"timeout": 0}):        # (doesn't use them up: the brain still gets them)
        m = u.get("message") or {}
        f = m.get("from", {})
        print("chat %s: %s %s (@%s): %r" % (m.get("chat", {}).get("id"), f.get("first_name", ""),
                                           f.get("last_name", ""), f.get("username", ""), m.get("text")))
    print("Linked:", links() or "nobody yet")
