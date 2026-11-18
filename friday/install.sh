#!/bin/bash
# Patti's helpers on friday. From the laptop:
#   scp -r friday/ friday:patti && ssh friday 'bash patti/install.sh'
# The key (~/.config/patti/ask-key) is copied from Patti's
# ~/.config/robotpattinson/claude-key; it's never printed.
set -e
cd "$(dirname "$0")"
mkdir -p ~/.config/systemd/user ~/.config/patti
[ -x venv/bin/python ] || { python3 -m venv venv && venv/bin/pip install -q fastembed; }
chmod 700 ~/.config/patti
cp patti-ask.service kowloon-sync.service kowloon-sync.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now patti-ask.service
# Kowloon's repos: once gh is logged in with the read-only token.
gh auth status >/dev/null 2>&1 && systemctl --user enable --now kowloon-sync.timer
# nftables: the ask port only from Patti (her Tailscale address), and only
# over Tailscale. Kept in /etc/nftables.conf's "friday" table.
if ! grep -q "dport 8765" /etc/nftables.conf; then
    sudo sed -i 's|^\( *\)tcp dport 4713 drop$|&\n\1tcp dport 8765 iifname "tailscale0" ip saddr 100.64.0.20 accept\n\1tcp dport 8765 drop|' /etc/nftables.conf
    sudo nft -f /etc/nftables.conf
fi
