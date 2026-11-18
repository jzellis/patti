#!/bin/bash
# After rp-walkie.service starts her session: Claude Code asks, every time,
# to confirm loading a development channel (the walkie plugin). Josh chose to
# have that answered here (2026-10-29), for the plugin version reviewed in
# README.md. Waits up to a minute for exactly that warning, with "I am using
# this for local development" selected, presses Enter once, and stops.
for _ in $(seq 60); do
    screen=$(tmux capture-pane -p -t walkie 2>/dev/null)
    if grep -q "WARNING: Loading development channels" <<<"$screen" &&
       grep -q "Channels: plugin:walkie@walkie-claudie" <<<"$screen" &&
       grep -q "❯ 1. I am using this for local development" <<<"$screen"; then
        tmux send-keys -t walkie Enter
        exit 0
    fi
    sleep 1
done
echo "walkie: no development-channel warning to confirm" >&2
