#!/bin/sh
# Install the hold-to-shut-down joystick watcher. Run on the Pi: sudo ./install.sh
set -e
cd "$(dirname "$0")"
install -m 755 sensehat_shutdown.py /usr/local/bin/sensehat_shutdown.py
install -m 644 sensehat-shutdown.service /etc/systemd/system/sensehat-shutdown.service
install -D -m 755 sensehat-blank /usr/lib/systemd/system-shutdown/sensehat-blank
systemctl daemon-reload
systemctl enable --now sensehat-shutdown.service
