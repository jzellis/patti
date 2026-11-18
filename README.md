# Patti

Patti is a small home robot: a Raspberry Pi 4 brain with a face, eyes, ears and a voice, a
helper PC ("friday") that does the heavy thinking (speech, vision, her library, talking to
Claude), and a few Picos and ESP32 boards running her screens, eyes, motors and arm.

This is her **software**, shared as-is from a personal project. It's written for one
household's hardware, so expect to read and adapt rather than install and run.

- `brain/`: her mind on the Pi: voice commands, memory, moods and drives, conversation, timers,
  music, notes, weather, news, people she knows, and the web page to talk to her.
- `friday/`: the helper PC: understanding what's said to her, answers, reading books, her corpus.
- `eyes/`, `face/`, `cyd/`: her round-screen eyes, e-paper face and chest screen.
- `vision/`: face tracking (camera gimbal on a Pico).
- `motors/`, `arm/pico/`: wheels and the arm, driven from Picos.
- `pi/`: the Pi's services: backups, music, boot status, the Sense HAT, a phone walkie-talkie.

Names of people, pets and places in examples and prompts are placeholders. Keys, passwords,
recordings, photos and her memories are not in this repository: they live on the machines.
