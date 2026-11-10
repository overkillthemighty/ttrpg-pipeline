# Roadmap

Ideas agreed on but not built yet. Nothing here is promised for a date.

## Linux recorder

A Linux version of the laptop side (record, sync, submit), so a Linux laptop or a small always-on box
(e.g. a Raspberry Pi by the table) can be the recorder.

Things to work out:

- **Language:** PowerShell 7 runs on Linux, so the existing scripts could be ported rather than rewritten.
  Most of the logic (segments, manifests, resume, sync, upload) is already cross-platform; the
  Windows-only parts are listed below.
- **Audio capture:** `dshow` is Windows-only. Linux would use ALSA (`-f alsa -i hw:2,0`) or
  PulseAudio/PipeWire (`-f pulse`). Device listing changes too (`arecord -l`, `pactl list sources`).
  Stable device names matter for the saved-device check: ALSA card names or `/dev/snd/by-id` rather
  than card numbers, which can change between boots or USB ports.
- **Hidden background recording:** on Windows ffmpeg runs in its own hidden console so closing the window
  doesn't stop it. On Linux the equivalent is `setsid`/`nohup`, or a systemd user service, which would also
  allow starting/stopping from a phone or the dashboard.
- **Keyboard and stop handling:** `[Console]::KeyAvailable` and Ctrl+C behave differently in Linux
  terminals and over SSH; the `stop_recording.flag` file already works everywhere.
- **Stereo receivers:** check that both channels of each receiver come through untouched (no downmix by
  PulseAudio/PipeWire profiles).
- **Testing:** needs the real receivers on a Linux machine.

## Other ideas

- Room mic alongside the lavs (recorded by the laptop script, not used in processing yet).
- Optionally map diarized speakers (room mic only) to player names from a short enrollment clip.
