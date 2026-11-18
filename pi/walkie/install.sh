#!/bin/bash
# Installs her walkie-talkie channel (README.md). Run with sudo, once:
#   sudo ~/robotpattinson/pi/walkie/install.sh
# Needs Bun and the walkie plugin first (README.md, "Set up").
set -e
[ "$(id -u)" = 0 ] || { echo "Run with sudo."; exit 1; }
user=${SUDO_USER:?run with sudo, as yourself}
home=$(getent passwd "$user" | cut -d: -f6)
here=$home/robotpattinson/pi/walkie
for s in rp-walkie-firewall rp-walkie; do
    sed -e "s|@USER@|$user|g" -e "s|@HOME@|$home|g" "$here/$s.service" > "/etc/systemd/system/$s.service"
done
chmod +x "$here/start.sh" "$here/confirm.sh"
systemctl daemon-reload
systemctl enable --now rp-walkie-firewall rp-walkie
echo "Installed. Her session: sudo -u $user tmux attach -t walkie  (Ctrl-b d to leave it running)"
