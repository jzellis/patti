#!/bin/sh
# Install the boot-time Sense HAT LED blanker. Run on the Pi: sudo ./install.sh
set -e
cd "$(dirname "$0")"
install -m 755 sensehat_off.py /usr/local/bin/sensehat_off.py
install -m 644 sensehat-off.service /etc/systemd/system/sensehat-off.service
systemctl daemon-reload
systemctl enable --now sensehat-off.service
