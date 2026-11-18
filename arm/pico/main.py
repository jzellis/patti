# Arm bench test: MicroPython on the Pico 2 on the Kitronik servo board, driven over USB serial.
# Commands (one per line, e.g. from Thonny's shell or `mpremote connect /dev/ttyACM0 repl`):
#   go 3 90        servo 3 to 90 degrees, slowly (SPEED degrees per second)
#   speed 30       change the speed
#   off 3          stop driving servo 3 (it goes limp)    off   - all of them
#   where          print what each servo was last told
# Nothing moves until you send a command. A servo's FIRST move jumps from wherever it physically is,
# so set that joint by hand near the target before its first "go".
import sys, time
from machine import Pin, PWM

# Kitronik board: servo 1-8 = GP2-GP9 (check against the silkscreen / Kitronik's docs; edit if different)
PINS = [2, 3, 4, 5, 6, 7, 8, 9]
MIN_US, MAX_US = 500, 2500   # 0 and 180 degrees; narrow these if a servo buzzes at an end
SPEED = 30

pwm = [None] * 8
pos = [None] * 8

def write(i, deg):
    if pwm[i] is None:
        pwm[i] = PWM(Pin(PINS[i])); pwm[i].freq(50)
    pwm[i].duty_ns(int((MIN_US + (MAX_US - MIN_US) * deg / 180) * 1000))
    pos[i] = deg

def go(i, target):
    target = max(0, min(180, target))
    if pos[i] is None:
        write(i, target); return
    step = 1 if target > pos[i] else -1
    d = pos[i]
    while abs(target - d) >= 1:
        d += step; write(i, d); time.sleep(1 / SPEED)
    write(i, target)

def off(i):
    if pwm[i] is not None:
        pwm[i].deinit(); Pin(PINS[i], Pin.IN); pwm[i] = None

print("arm test ready: go <1-8> <deg> | speed <dps> | off [n] | where")
while True:
    w = sys.stdin.readline().split()
    try:
        if not w: continue
        if w[0] == "go": go(int(w[1]) - 1, float(w[2]))
        elif w[0] == "speed": SPEED = max(1, float(w[1]))
        elif w[0] == "off":
            for i in ([int(w[1]) - 1] if len(w) > 1 else range(8)): off(i)
        elif w[0] == "where": print([None if p is None else round(p) for p in pos])
        else: print("?")
        print("ok")
    except Exception as e:
        print("error:", e)
