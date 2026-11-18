# Send one line to the Pico's REPL and print the reply, without resetting it (so servos keep holding).
#   python3 arm/pico/repl.py "arm.go(1, 95)"
import sys, time, serial

port = serial.Serial("/dev/ttyACM0", 115200, timeout=0.2)
port.write(b"\r\n"); time.sleep(0.2); port.read(4096)
port.write(sys.argv[1].encode() + b"\r\n")
out, idle, t0 = b"", 0, time.time()
while time.time() - t0 < 120:
    c = port.read(4096)
    out += c
    if out.rstrip().endswith(b">>>"): break
print(out.decode(errors="replace").replace("\r", ""))
