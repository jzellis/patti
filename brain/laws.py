"""Her laws: what she may do of her own accord, checked before anything her
drives (drives.py) choose. Hard rules in code, above her feelings: nothing
she wants can outweigh them, and they're never a prompt or a reward. In
order, the first that says no wins:

  1. Do no harm. Her own drives never move her (wheels or head): she moves
     only when someone tells her to (yet). Nothing about the people in the
     house leaves it as a picture (no photos in messages); what someone
     told her, she only tells them.
  2. Do as she's asked, by who may ask. Quiet hours (QUIET), and "leave me
     alone" (do not disturb), and she starts nothing: no talking, no
     messages. She only reaches out to people with a login (users.py) who
     may chat with her; questions about strangers only to people who may
     name them ("people").
  3. Look after herself, last of all, never over the others.

    ok, why = laws.check("speak", quiet=False, dnd=False, perms={"chat"})
    ok, why = laws.check("message", ..., photo=False, kind="stranger", perms={"chat", "people"})

"Stop", from anyone, is her commands' (voice.py), and isn't hers to weigh.
Being honest (saying when she isn't sure, never making things up) is how
her answers are made (claude.py, wiki.py, memory.py's recall).
"""
import time

QUIET = (22, 8)         # quiet hours: from 10pm to 8am she starts nothing
KINDS = ("speak", "message", "move")


def quiet_now(t=None):
    h = time.localtime(t).tm_hour
    return h >= QUIET[0] or h < QUIET[1]


def check(kind, dnd=False, perms=(), photo=False, about="you", told_by=None, to=None, t=None):
    """May she do this of her own accord? (ok, why not). kind: "speak"
    (start talking to someone), "message" (Telegram), "move". perms: the
    person's (users.py). about: "you" (them) or "stranger". told_by: whose
    answer she'd be passing on; to: who to."""
    if kind not in KINDS:
        return False, "not something she does on her own"
    # 1. harm
    if kind == "move":
        return False, "her drives never move her"
    if photo:
        return False, "no pictures of people leave the house"
    if told_by and to and told_by != to:
        return False, "what someone told her is theirs"
    # 2. asked
    if quiet_now(t):
        return False, "quiet hours"
    if dnd:
        return False, "asked to leave them be"
    if "chat" not in perms:
        return False, "only people who may chat with her"
    if about == "stranger" and "people" not in perms:
        return False, "only people who may name faces"
    return True, None
