#!/usr/bin/env python3
# Talk to the arm over Wi-Fi (armnet.py on the Pico, port 7070).
#   python3 arm/pico/armctl.py "go 1 90"        one command, print the reply
#   python3 arm/pico/armctl.py "speed 20" "shoulder 100" wait where
#   python3 arm/pico/armctl.py                  interactive (blank line or Ctrl-D to quit)
# Host: $ARM_HOST, else patti-arm, else 192.168.1.223 (its DHCP address 2026-11-16 - the router doesn't resolve the
# hostname; reserve that address for it in the router so it can't change).
import os, socket, sys

hosts = [os.environ["ARM_HOST"]] if "ARM_HOST" in os.environ else ["patti-arm", "192.168.1.223"]
for host in hosts:
    try:
        s = socket.create_connection((host, 7070), timeout=5)
        break
    except OSError as e:
        err = e
else:
    sys.exit("can't reach the arm at %s: %s" % (" or ".join(hosts), err))
f = s.makefile("rw")
s.settimeout(None)               # "wait" can take a while
print(f.readline().strip())

def send(cmd):
    f.write(cmd + "\n"); f.flush()
    print(f.readline().strip())

if len(sys.argv) > 1:
    for c in sys.argv[1:]:
        send(c)
else:
    try:
        while True:
            c = input("arm> ").strip()
            if not c:
                break
            send(c)
    except EOFError:
        pass
