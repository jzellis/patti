# sensehat-off

The Pi firmware lights the Sense HAT matrix with a rainbow at boot and nothing
turns it off; on a headless Pi the Plymouth boot splash also draws onto it
(a white block) after that. This installs a oneshot systemd service that clears it.

    sudo ./install.sh                  # install + enable + run now
    python3 sensehat_off.py            # clear by hand any time
