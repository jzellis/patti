"""Her kitchen timer: "Patti, set a timer for ten minutes".

One at a time. While it runs, the Sense HAT's matrix drains like an
hourglass (hat.py draws it from fraction()); when it's done the matrix
flashes and she says so, again every RING_EVERY_S, RINGS times or until it's
dismissed (the joystick, a tap on her chest, "stop", "cancel the timer").

    t = Timer(say)                  # say(key, **fields): a line from replies.py
    t.start(600)
    t.left()                        # seconds to go (None if none running)
    asyncio.create_task(t.run())    # rings it
"""
import asyncio
import time

RING_EVERY_S = 20
RINGS = 4


def spoken(seconds):
    """600 -> "ten minutes", 90 -> "a minute and a half", 3700 -> "an hour and a minute"."""
    words = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
             "fifteen sixteen seventeen eighteen nineteen").split()
    tens = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}
    num = lambda n: words[n] if n < 20 else tens[n // 10] + ("-" + words[n % 10] if n % 10 else "")
    unit = lambda n, name: ("a " + name if n == 1 else "%s %ss" % (num(n), name))
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if h:
        if m == 30 and not s:
            return ("an hour" if h == 1 else unit(h, "hour")) + " and a half"
        return unit(h, "hour").replace("a hour", "an hour") + (" and " + unit(m, "minute") if m else "")
    if m:
        if s == 30:
            return unit(m, "minute") + " and a half"
        return unit(m, "minute") + (" and " + unit(s, "second") if s and m < 5 else "")
    return unit(s, "second")


class Timer:
    def __init__(self, say):
        self.say = say
        self.length = 0.0
        self.ends = None                # time.monotonic() it's done, while one's set
        self.ringing = False
        self.rang = 0
        self.wake = asyncio.Event()

    def start(self, seconds):
        self.length = float(seconds)
        self.ends = time.monotonic() + seconds
        self.ringing = False
        self.wake.set()

    def cancel(self):
        """Stop it, running or ringing; True if there was one."""
        had = self.ends is not None or self.ringing
        self.ends, self.ringing = None, False
        self.wake.set()
        return had

    def dismiss(self):
        """Quieten a ringing timer; True if it was ringing."""
        if self.ringing:
            self.ringing = False
            self.wake.set()
            return True
        return False

    def left(self):
        return None if self.ends is None else max(0.0, self.ends - time.monotonic())

    def fraction(self):
        """How much is left, 1 to 0 (None if none running)."""
        left = self.left()
        return None if left is None else left / self.length if self.length else 0.0

    async def run(self):
        while True:
            if self.ends is None and not self.ringing:
                self.wake.clear()
                await self.wake.wait()
                continue
            if self.ends is not None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), self.left())
                    continue                    # changed: look again
                except asyncio.TimeoutError:
                    pass
                if self.ends is None or self.left() > 0:
                    continue
                self.ends, self.ringing, self.rang = None, True, 0
            while self.ringing and self.rang < RINGS:
                self.say("timer_done")
                self.rang += 1
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), RING_EVERY_S)
                except asyncio.TimeoutError:
                    pass
            self.ringing = False


if __name__ == "__main__":
    for s in (5, 30, 60, 90, 125, 600, 1800, 3600, 3660, 5400, 7200):
        print(s, spoken(s))
