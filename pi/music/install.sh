#!/bin/sh
# Make her a network speaker. Run on the Pi as yourself (not root): ./install.sh
# MPD and shairport-sync run as your user so they reach PipeWire; upmpdcli
# stays a system service (it only talks to MPD).
set -e
cd "$(dirname "$0")"
name="Patti"

# The packages' own system services would fight over the ports and can't
# reach your PipeWire.
sudo apt-get install -y pipewire-alsa >/dev/null
sudo systemctl disable --now shairport-sync.service mpd.service mpd.socket 2>/dev/null || true

mkdir -p ~/.config/wireplumber/wireplumber.conf.d ~/.config/pipewire/pipewire.conf.d ~/.config/mpd ~/.config/robotpattinson ~/.config/systemd/user ~/.local/share/mpd/playlists ~/Music
install -m 644 mpd.conf ~/.config/mpd/mpd.conf
install -m 644 shairport-sync.conf ~/.config/robotpattinson/shairport-sync.conf
install -m 644 rp-airplay.service ~/.config/systemd/user/rp-airplay.service
install -m 644 wireplumber-bluetooth.conf ~/.config/wireplumber/wireplumber.conf.d/80-bluetooth.conf
# Her sound on friday's speakers when friday's there (falls back to her own).
install -m 644 pipewire-friday.conf ~/.config/pipewire/pipewire.conf.d/60-friday.conf

set_conf() {    # key value: uncomment or add it in /etc/upmpdcli.conf
    if grep -qE "^#?\s*$1\s*=" /etc/upmpdcli.conf; then
        sudo sed -i -E "s|^#?\s*$1\s*=.*|$1 = $2|" /etc/upmpdcli.conf
    else
        echo "$1 = $2" | sudo tee -a /etc/upmpdcli.conf >/dev/null
    fi
}
set_conf friendlyname "$name"
set_conf avfriendlyname "$name"
set_conf ohproductroom "$name"
set_conf mpdhost localhost
set_conf mpdport 6600

sudo loginctl enable-linger "$(id -un)"
systemctl --user daemon-reload
systemctl --user enable --now mpd.service rp-airplay.service
systemctl --user restart wireplumber.service
sudo systemctl restart upmpdcli.service
