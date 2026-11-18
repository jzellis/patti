#!/bin/bash
# Her walkie-talkie channel: a Claude Code session, playing her, that the
# Walkie-Claudie app on Josh's phone talks to (README.md). Run in tmux by
# rp-walkie.service; it picks up the last conversation where it left off.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
export WALKIE_ENABLE=1 WALKIE_NAME=Patti
export PATH="$HOME/.bun/bin:$HOME/.local/bin:$PATH"
cont=()
if ls "$HOME/.claude/projects/$(pwd | tr / -)"/*.jsonl >/dev/null 2>&1; then
    cont=(--continue)
fi
exec claude "${cont[@]}" --name Patti --model sonnet --permission-mode dontAsk \
    --dangerously-load-development-channels plugin:walkie@walkie-claudie
