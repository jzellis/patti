# Runs at every boot of the motor Pico, including a watchdog reboot from
# drive.py: stop all motors first. The PCA9685 keeps its outputs through a
# Pico reset, so without this a motor running when the Pico died keeps going.
try:
    import motors
    motors.Motors()
except Exception as e:
    print("motors: couldn't stop at boot:", e)
