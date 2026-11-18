#!/bin/sh
# Start the brain at boot. Run on the Pi: sudo ./install.sh
set -e
cd "$(dirname "$0")"
user=${SUDO_USER:?run with sudo from your own account}
home=$(getent passwd "$user" | cut -d: -f6)
uid=$(id -u "$user")
sed -e "s|@USER@|$user|" -e "s|@HOME@|$home|" -e "s|@UID@|$uid|g" rp-brain.service > /etc/systemd/system/rp-brain.service
# Start the user's PipeWire at boot without a login, so she can talk.
loginctl enable-linger "$user"
# The camera mic as a shareable ALSA device ("rpmic"), for voice + recording.
install -D -m 644 50-rpmic.conf /etc/alsa/conf.d/50-rpmic.conf
systemctl daemon-reload
systemctl enable --now rp-brain.service
