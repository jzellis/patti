# bootstatus

Shows her boot on the Sense HAT matrix, from the moment its driver loads
(about 7 s after power on; before that the HAT shows its own rainbow):

1. **Booting:** a single pixel spirals in from the top-left corner, round and round.
2. **Booted:** when boot finishes, it runs on to the centre and bursts.
3. **Network:** a blue wifi fan builds up while it waits (up to 60 s), then for 5 s:
   green (online), amber (wifi but no internet) or red with a slash (no network).
4. **Tailscale:** its nine dots for 2 s, green (connected) or red.

Then the matrix goes blank and the script exits.

    sudo ./install.sh                  # install + enable (shows from the next boot)
    sudo bootstatus.py --demo          # the whole show now, with made-up timings
    journalctl -b -u rp-bootstatus     # what it found

Settings in /etc/default/rp-bootstatus: `ROTATE` (0, 90, 180 or 270, for how
the matrix is mounted) and `BRIGHT` (0 to 1).

The installer disables `sensehat-off` (this blanks the matrix itself) and
removes `splash` from /boot/firmware/cmdline.txt, since Plymouth's splash
also draws on the matrix. It keeps the old cmdline as cmdline.txt.bak-splash.
