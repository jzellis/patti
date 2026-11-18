# Arm over Wi-Fi: a TCP line server on the Pico 2 W (Kitronik Simply Servos). Started by main.py at boot.
# Uses arm.py for pins, limits, speed and the shoulder trim; adds non-blocking moves so "stop" works mid-move.
#
# Wi-Fi: secrets.py on the Pico (the user writes it; never printed or logged here):
#   WIFI_SSID = "..."
#   WIFI_PASSWORD = "..."
#
# Protocol: connect to port 7070, one command per line, one reply line each ("ok ..." or "err ...").
#   go <n> <deg>      move servo n (1-8, or a name) slowly to deg (clamped to arm.LIMITS)
#   <name> <deg>      same, by name: base 90, elbow 120, forearm 100, wrist 90 (shoulder <deg>: see below)
#                     names: base 1, elbow 4, forearm 5 (wrist pitch, the belt MG996 in the forearm), wrist 6 (roll)
#   shoulder <deg>    the shoulder: servo 2 alone (arm.SHOULDER_PAIR False, since 2026-11-17: the pair fights), else
#                     2+3 together, 3 mirrored (180 - deg + trim). With the pair off, servo 3 is never driven.
#   trim <t>          shoulder trim for this session (arm.SH_TRIM), applied at once if the shoulder is held
#   speed <dps>       move speed, degrees per second
#   off [n|name] / stop    stop pulses to n, or to all (limp); "off shoulder" releases 2 and 3
#   where             last commanded angle of every driven servo, and what's still moving
#   wait              reply only once nothing is moving
#   ping              ok pong
#   park              everything held moves slowly to the rest pose (shoulder 30 = resting on the base, elbow 90,
#                     base 90, forearm 90),
#                     then releases; the angles are saved on the Pico
#   wake              wakes the shoulder, elbow and forearm AT their remembered angles, one by one - no jump if
#                     nothing moved them while limp. NEVER the base: it turns freely when limp and whips the arm round
#                     (2026-11-16: a 30 deg base jump nearly tore the USB lead out and knocked the PSU off the table)
#   wake <name> [deg] wakes one joint: at deg if given (say where it really is), else at its remembered angle.
#                     For the base: line it up by hand to the angle "where"/"wake" reports, or tell it the angle.
# Every release (park, stop, off) saves the released servos' angles to pose.json, so "wake" picks up from there.
# A servo's FIRST pulse (or the first after "off") jumps at full speed from wherever it really is: the reply says
# "jumped". Moving 2 or 3 alone is refused while both are held (they'd fight): use "shoulder".
# Dropping the connection changes nothing: driven servos keep holding.
import asyncio, time, network, json
from machine import Pin
import arm

PORT = 7070
HOSTNAME = "patti-arm"
TICK = 0.02                      # 50 Hz motion loop
led = Pin("LED", Pin.OUT)

NAMES = {"base": 1, "elbow": 4, "forearm": 5, "wrist": 6}
# rest pose: shoulder 30 = upper arm forward, resting on its own base (2026-11-16: balanced at 90 it fell forward onto
# the base when released - balance isn't reliable). The elbow holds wherever it's left.
PARK = {1: 90, 2: 30, 4: 90, 5: 90}
POSE_FILE = "pose.json"


def load_pose():
    try:
        with open(POSE_FILE) as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return dict(PARK)


def save_pose(update):
    if not update:
        return
    p = load_pose()
    p.update(update)
    p.pop(3, None)                       # servo 3 is always the mirror of 2
    with open(POSE_FILE, "w") as f:
        json.dump({str(k): round(v, 1) for k, v in p.items()}, f)

target = [None] * 9              # where each servo is heading (None = not commanded)
coupled = False                  # servo 3 follows servo 2 (mirrored)


def driven(n):
    return arm.pwm[n] is not None


def moving():
    return [n for n in range(1, 9) if target[n] is not None and driven(n) and abs(target[n] - arm.pos[n]) > 0.01]


def mirror(a):
    return max(arm.LIMITS[3][0], min(arm.LIMITS[3][1], 180 - a + arm.SH_TRIM))


def start(n, deg):
    """Set a target; returns 'jumped' if this is the servo's first pulse."""
    deg = arm._clamp(n, deg)
    target[n] = deg
    if not driven(n) or arm.pos[n] is None:
        arm._write(n, deg)
        return "jumped"
    return "moving"


async def mover():
    while True:
        step = arm.SPEED * TICK
        for n in range(1, 9):
            t = target[n]
            if t is None or not driven(n) or (coupled and n == 3):
                continue
            p = arm.pos[n]
            if abs(t - p) > 0.01:      # the last step lands exactly on the target
                arm._write(n, p + max(-step, min(step, t - p)))
        if coupled and driven(2) and driven(3):
            arm._write(3, mirror(arm.pos[2]))
        await asyncio.sleep(TICK)


def stop(n=None):
    global coupled
    ids = [n] if n else list(range(1, 9))
    save_pose({i: arm.pos[i] for i in ids if driven(i) and arm.pos[i] is not None})
    for i in ids:
        target[i] = None
        arm.off(i)
    if n is None or n in (2, 3):
        coupled = False


def servo(word):
    word = word.lower()
    return NAMES[word] if word in NAMES else int(word)


def command(line):
    global coupled
    w = line.split()
    if not w:
        return None
    c = w[0].lower()
    if c in NAMES:                       # "elbow 120" = "go elbow 120"
        w = ["go"] + w
        c = "go"
    try:
        if c == "ping":
            return "ok pong"
        if c == "go":
            n, deg = servo(w[1]), float(w[2])
            if not 1 <= n <= 8:
                return "err servo 1-8"
            if n == 3 and not arm.SHOULDER_PAIR:
                return "err servo 3 isn't used (the shoulder is servo 2 alone)"
            if n in (2, 3) and driven(2) and driven(3):
                return "err both shoulder servos are held: use shoulder <deg>"
            if n in (2, 3):
                coupled = False
            return "ok %d %s %g" % (n, start(n, deg), target[n])
        if c == "shoulder":
            deg = float(w[1])
            if not arm.SHOULDER_PAIR:
                return "ok shoulder %s %g (servo 2 alone)" % (start(2, deg), target[2])
            r = start(2, deg)
            if not driven(3):
                arm._write(3, mirror(arm.pos[2]))
                r = "jumped"
            target[3] = None
            coupled = True
            return "ok shoulder %s %g (3 mirrored, trim %g)" % (r, target[2], arm.SH_TRIM)
        if c == "trim":
            arm.SH_TRIM = float(w[1])
            return "ok trim %g" % arm.SH_TRIM
        if c == "speed":
            arm.SPEED = max(1.0, float(w[1]))
            return "ok speed %g" % arm.SPEED
        if c in ("off", "stop"):
            if c == "off" and len(w) > 1 and w[1].lower() == "shoulder":
                stop(2); stop(3)
            else:
                stop(servo(w[1]) if c == "off" and len(w) > 1 else None)
            return "ok off"
        if c == "where":
            held = " ".join("%d:%g" % (n, round(arm.pos[n], 1)) for n in range(1, 9) if driven(n))
            return "ok held %s | moving %s | speed %g trim %g" % (held or "-", moving() or "-", arm.SPEED, arm.SH_TRIM)
        return "err unknown: " + c
    except (IndexError, ValueError, KeyError):
        return "err bad arguments"


async def client(reader, writer):
    writer.write(b"ok patti-arm ready\n")
    await writer.drain()
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.decode().strip()
            low = line.lower()
            if low == "wait":
                while moving():
                    await asyncio.sleep(0.05)
                reply = "ok still"
            elif low == "park":
                reply = await park()
            elif low.split()[0] == "wake":
                try:
                    reply = await wake(line.split()[1:])
                except (ValueError, KeyError, IndexError):
                    reply = "err wake [name] [deg]"
            else:
                reply = command(line)
            if reply:
                writer.write((reply + "\n").encode())
                await writer.drain()
    except Exception as e:
        print("client error:", e)
    finally:
        writer.close()          # servos keep holding


async def park():
    global coupled
    for n, a in PARK.items():
        if n == 2 and arm.SHOULDER_PAIR and driven(2) and driven(3):
            target[2] = a; target[3] = None; coupled = True
        elif (n != 2 or not arm.SHOULDER_PAIR) and driven(n):
            target[n] = arm._clamp(n, a)
    while moving():
        await asyncio.sleep(0.05)
    stop()
    return "ok parked (saved %s)" % load_pose()


AUTO_WAKE = (2, 4, 5)                    # not the base (1)


async def wake(args=()):
    global coupled
    woke = []
    pose = load_pose()
    if args:
        n = 2 if args[0].lower() == "shoulder" else servo(args[0])
        if len(args) > 1:
            pose[n] = float(args[1])
        todo = [(n, pose.get(n, PARK.get(n, 90)))]
    else:
        todo = [(n, a) for n, a in sorted(pose.items()) if n in AUTO_WAKE]
    for n, a in todo:
        if driven(n) or n == 3:
            continue
        if n == 2 and arm.SHOULDER_PAIR:
            if driven(3):
                continue
            arm._write(2, a); arm._write(3, mirror(a)); target[2] = a; target[3] = None; coupled = True
        else:
            arm._write(n, arm._clamp(n, a)); target[n] = arm.pos[n]
        woke.append("%d:%g" % (n, a))
        await asyncio.sleep(0.3)         # one at a time: no current spike
    note = "" if args or driven(1) else " | base NOT woken (saved %g): line it up, then wake base" % pose.get(1, 90)
    return "ok woke " + (" ".join(woke) or "nothing (already held)") + note


def wifi():
    try:
        import secrets
    except ImportError:
        print("armnet: no secrets.py on the Pico (WIFI_SSID / WIFI_PASSWORD) - Wi-Fi off, REPL only")
        return None
    network.country("GB")
    network.hostname(HOSTNAME)
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.config(pm=0xA11140)    # no power saving: steadier replies
    wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
    for _ in range(60):
        if wlan.isconnected():
            break
        led.toggle()
        time.sleep(0.5)
    if not wlan.isconnected():
        print("armnet: Wi-Fi didn't connect (status %d) - REPL only" % wlan.status())
        led.off()
        return None
    led.on()
    print("armnet: %s at %s port %d" % (HOSTNAME, wlan.ifconfig()[0], PORT))
    return wlan


async def keep_wifi(wlan):
    import secrets
    while True:
        await asyncio.sleep(10)
        if not wlan.isconnected():
            led.off()
            wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
        else:
            led.on()


async def main(wlan):
    await asyncio.start_server(client, "0.0.0.0", PORT)
    asyncio.create_task(mover())
    asyncio.create_task(keep_wifi(wlan))
    while True:
        await asyncio.sleep(3600)


def run():
    wlan = wifi()
    if wlan is None:
        return
    try:
        asyncio.run(main(wlan))
    except KeyboardInterrupt:
        print("armnet: stopped (servos still holding; arm.off() to release)")
