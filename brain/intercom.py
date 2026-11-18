"""Listening and talking through her, from her page: an intercom.

    /listen   websocket: her mic, sent as binary messages of 16-bit mono PCM
              at RATE, CHUNK_S at a time, for as long as it's open
    /talk     websocket: the page's mic, the same format the other way, played
              on her speaker; the page sends {"t":"down"} / {"t":"up"} round
              each push-to-talk, and audio only in between

Both need the login, like every page. A browser only lets a page use its mic
on HTTPS, so /talk works from https://rpattinson.<tailnet>.ts.net/ (tailscale
serve, see brain/README.md), not from the plain http:// address.

So nobody's overheard by surprise, while anyone's listening the Sense HAT
shows an ear (hat.py), and while someone talks it shows a speaker. While
someone talks her ears are off (her mic hears her speaker, and "Patti" in your
voice would wake her) and music is turned down (chest.py), as when she talks.

Her mic is shared (rpmic, systemd/50-rpmic.conf), so each listener gets its
own arecord alongside voice commands and recording. If a listener's link is
slow, old audio is dropped rather than queued, so what you hear stays live.
"""
import asyncio
import json
import subprocess
import sys
import time

from aiohttp import WSMsgType, web

RATE = 16000
CHUNK_S = 0.1
CHUNK = int(RATE * CHUNK_S) * 2         # bytes
BACKLOG = 5                             # chunks queued for a slow listener before dropping
TALK_TAIL_S = 0.8                       # her ears stay off this long after you let go
VOICE = "Voice"                         # music.py leaves this client's volume alone


class Intercom:
    def __init__(self, mic):
        self.mic = mic
        self.listeners = 0
        self.talkers = 0                # pages with the button held
        self.talked_at = 0.0

    @property
    def talking(self):
        return self.talkers > 0 or time.monotonic() - self.talked_at < TALK_TAIL_S

    def state(self):
        """What the Sense HAT shows: "talk", "listen" or None."""
        return "talk" if self.talking else "listen" if self.listeners else None

    # ---------- her mic to the page

    async def listen(self, request):
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        if not self.mic:
            await ws.close(message=b"no mic")
            return ws
        proc = await asyncio.create_subprocess_exec(
            "arecord", "-q", "-D", self.mic, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        q = asyncio.Queue()
        self.listeners += 1
        print("intercom: listening from %s" % who(request), file=sys.stderr)

        async def read():
            while True:
                data = await proc.stdout.readexactly(CHUNK)
                if q.qsize() >= BACKLOG:
                    q.get_nowait()          # too far behind: drop the oldest
                q.put_nowait(data)

        async def send():
            while True:
                await ws.send_bytes(await q.get())

        async def until_closed():
            async for _ in ws:
                pass

        tasks = [asyncio.create_task(c) for c in (read(), send(), until_closed())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            self.listeners -= 1
            print("intercom: stopped listening (%s)" % who(request), file=sys.stderr)
            await ws.close()
        return ws

    # ---------- the page's mic to her speaker

    async def talk(self, request):
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        player = None
        down = False
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        t = json.loads(msg.data).get("t")
                    except (ValueError, AttributeError):
                        continue
                    if t == "down" and not down:
                        down = True
                        self._press(True)
                        print("intercom: talking from %s" % who(request), file=sys.stderr)
                        player = await asyncio.create_subprocess_exec(
                            "paplay", "--client-name=" + VOICE, "--raw", "--rate=%d" % RATE,
                            "--channels=1", "--format=s16le", "--latency-msec=60",
                            stdin=subprocess.PIPE)
                    elif t == "up" and down:
                        down = False
                        await self._finish(player)
                        player = None
                        self._press(False)
                elif msg.type == WSMsgType.BINARY and player and player.returncode is None:
                    player.stdin.write(msg.data)
                    try:
                        await player.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        player = None
        finally:
            await self._finish(player)
            if down:
                self._press(False)
        return ws

    def _press(self, on):
        self.talkers += 1 if on else -1
        if not on:
            self.talked_at = time.monotonic()

    @staticmethod
    async def _finish(player):
        """Let what's been said play out, then close the player."""
        if not player:
            return
        try:
            player.stdin.close()
            await asyncio.wait_for(player.wait(), 5)
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
            if player.returncode is None:
                player.kill()
                await player.wait()


def who(request):
    """Whoever it is, as best we know: tailscale serve says who's logged in."""
    return request.headers.get("Tailscale-User-Login") or request.remote
