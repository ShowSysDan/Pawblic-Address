# Pawblic Address: project rules

Pawblic Address (PA) streams a phone's microphone from a web page to a Q-SYS Core:
browser → WebSocket → Flask → ffmpeg → RTP → Media Stream Receiver. `main` is the base
branch.

## Every change gets a version number

- Bump `VERSION` in `version.py` with every change that lands on `main`, using semantic
  versioning:
  - **patch** (0.4.0 → 0.4.1): fixes, docs, and tweaks with no change in behaviour;
  - **minor** (0.4.0 → 0.5.0): new features, or changed behaviour or defaults (ports,
    timeouts, events);
  - **major**: reserved for 1.0 and after, for changes that break existing installs.
- Add a matching entry at the top of `CHANGELOG.md`: `## [x.y.z] - YYYY-MM-DD`, then
  Added / Changed / Fixed / Security. `tests/test_version.py` fails if the two disagree.
- The version shows in the page footer and goes out in syslog. Static files are loaded
  with `?v=<version>`, so phones pick up new JS after an update. That's another reason
  never to skip a bump.

## Before pushing

- `pytest` passes.
- If you touched `app.py`, `relay.py` or anything about the audio path or process
  lifetime, also run `python tests/soak.py` and check it says PASS.
- Commit messages say what changed and why.

## Rules for the code

- **No orphaned ffmpeg.** Every ffmpeg process is started by `relay.FFmpegRelay` and
  stopped by its `stop()` in a `finally`. Don't start subprocesses anywhere else.
- **Never block the WebSocket thread.** Writes to ffmpeg go through the relay's queue.
  Anything that waits needs a timeout.
- **No shell.** Pass ffmpeg its arguments as a list. Every setting is validated in
  `settings.py` before it can reach a command line or URL.
- **Refuse other sites.** Anything that changes state (POST, the audio WebSocket) checks
  `_same_origin()`. Settings are JSON only.
- **Events are an interface.** If you add, rename or change fields on an `events.emit()`
  event, update the event table in the README; syslog consumers rely on it.
- **Latency first.** Don't undo the latency choices listed in the README ("Things already
  done for you"): no MediaRecorder, no browser DSP, `-probesize 32 -analyzeduration 0`,
  no `-fflags nobuffer`, one MP3 frame per RTP packet, and drop audio rather than queue it.
- **Installs are a service.** If you change how the app starts (files it needs, ports,
  environment variables), update `deploy/` and the README's install section to match.
  The install script has to stay safe to re-run, since that's how updates are done.
- Keep the page usable at phone width, and keep the branding (amber `--brand`, the paw
  logo).
