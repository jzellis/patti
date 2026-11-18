# sensehat-shutdown

Hold the Sense HAT joystick in (press the middle) for 10 seconds and the Pi
shuts down cleanly (the emergency way: normally it's Settings > Shut down
on her chest screen, and short presses of the joystick work the screens): the LED matrix turns red, then `shutdown -h now`. The
red goes out at the very end of the shutdown (any shutdown, not just this
one), and then it's safe to pull the power. A shorter press does nothing, and so does any press in the first minute
after boot (so a wiring mistake that reads as "held" can't boot-loop her).

    sudo ./install.sh                  # install + enable + start
    journalctl -u sensehat-shutdown    # see what it's doing
