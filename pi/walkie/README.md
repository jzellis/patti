# walkie

Talk to her from your phone like a walkie-talkie, with
[Walkie-Claudie](https://github.com/ahn4jang27/walkie-claudie) (MIT): its
Android app does the speech recognition and reads the answers aloud with the
phone's own voices, and a Claude Code plugin delivers what you said into a
Claude Code session on the Pi. That session plays her (CLAUDE.md here), on the
claude.ai subscription rather than the API.

From the session she can run `ask` (any of her voice commands: weather,
timers, the barometer, music, moods, the HAT, her voice) and `say` (out loud
through her speaker), read her code and log, and search the web. Nothing
else: .claude/settings.json allows just those, and the session runs in
`dontAsk` mode, so anything else is refused rather than waiting for someone
to approve it. It can't edit files, install things or fetch web pages.

The app's traffic isn't encrypted itself, so its ports (7700-7709) answer over
Tailscale only (walkie.nft): pair the phone with her Tailscale address.

## Set up

```sh
curl -fsSL https://bun.sh/install | bash                 # Bun, in ~/.bun (the plugin runs on it)
claude plugin marketplace add ahn4jang27/walkie-claudie
claude plugin install walkie@walkie-claudie
sudo ~/robotpattinson/pi/walkie/install.sh               # the firewall, and the session at boot
```

Claude Code warns about loading a development channel every time the
session starts; confirm.sh answers it, for this plugin only (Josh's choice).
The first start in this folder also asks Claude Code to trust it: attach
(below) and answer that one yourself, once.

The plugin was reviewed at commit e582724 (0.1.0). Before `claude plugin
update`, look over what changed: this session runs whatever it is.

Pair the phone: in a terminal on the Pi (the QR holds the channel's key, so
not over a shared screen),

```sh
~/.bun/bin/bun ~/.claude/plugins/cache/walkie-claudie/walkie/*/pair.ts
```

and in the app, Scan QR.

## Running it

```sh
tmux attach -t walkie              # watch her session (Ctrl-b d to leave it running)
sudo systemctl restart rp-walkie   # a fresh start (it carries on the last conversation)
journalctl -u rp-walkie            # the service's log
```

The key is in ~/.claude/channels/walkie/key; delete it and restart to
unpair every phone.
