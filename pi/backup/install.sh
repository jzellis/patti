#!/bin/sh
# Back Patti's data up to friday every hour. Run on the Pi: sudo ./install.sh
set -e
cd "$(dirname "$0")"
install -m 644 rp-backup.service /etc/systemd/system/rp-backup.service
install -m 644 rp-backup.timer /etc/systemd/system/rp-backup.timer
systemctl daemon-reload
systemctl enable --now rp-backup.timer
