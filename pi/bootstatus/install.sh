#!/bin/sh
# Install the boot status display. Run on the Pi: sudo ./install.sh
# It replaces sensehat-off (which would blank the matrix halfway through) and
# turns off Plymouth's boot splash, which also draws on the matrix.
set -e
cd "$(dirname "$0")"
install -m 755 bootstatus.py /usr/local/bin/bootstatus.py
install -m 644 rp-bootstatus.service /etc/systemd/system/rp-bootstatus.service
[ -f /etc/default/rp-bootstatus ] || printf 'ROTATE=0\nBRIGHT=0.6\n' > /etc/default/rp-bootstatus
systemctl daemon-reload
systemctl disable sensehat-off.service 2>/dev/null || true
systemctl enable rp-bootstatus.service
CMD=/boot/firmware/cmdline.txt
if grep -qw splash "$CMD"; then
    cp "$CMD" "$CMD.bak-splash"
    sed -i 's/ splash\b//' "$CMD"
    echo "removed 'splash' from $CMD (old copy: $CMD.bak-splash)"
fi
