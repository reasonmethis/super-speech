# Desktop app backlog

This is the ordered backlog for taking the version-zero controller into a
public Super Speech app. Each section is meant to be runnable as a focused
work session without having to reconstruct the product decisions.

## Version zero

- [x] Windows and macOS-friendly desktop architecture
- [x] Tray app with a compact status window
- [x] Immediate pause with sample-position-preserving resume
- [x] Persistent pause state across engine restarts
- [x] Current voice, current text, and scrollable read-only upcoming queue
- [x] Electron architecture following Littlebird's packaged-helper precedent
- [x] Native Windows installer build through Electron
- [ ] Test the macOS bundle on Apple Silicon hardware

## P0: one-click public installation

- Bundle the Kokoro engine as a platform-specific sidecar so end users do not
  need a system Python installation
- Use a directory-style frozen engine, package it outside `app.asar`, and build
  it independently for Windows x64, macOS x64, and macOS arm64
- Download the 338 MB model and voice data on first run with visible progress,
  checksum verification, retry, and resume
- Install the Super Speech skill for supported coding agents from the app
- Replace the current agent-assisted setup guide with an in-app setup flow
- Add launch-at-login as an opt-in installer or settings choice
- Build signed Windows releases and signed, notarized macOS releases
- Add an automated release workflow for version tags
- Sign the Electron shell, installer, frozen engine, and bundled native
  libraries rather than signing only the outer app
- Produce third-party notices and complete a licensing review for the bundled
  eSpeak NG dependency before any closed-source commercial distribution

## P1: interactive queue

- Let a user select any upcoming chunk and start it immediately
- Let a user replay a past chunk without losing the remaining queue
- Keep the current chunk and sample position when the user only pauses
- Add previous, next, replay, remove, clear, and drag-to-reorder controls
- Show when each chunk was queued, when it started, and when it finished
- Preserve enough rendered audio history to make replay instant
- Define queue-selection behavior in tests before changing the file protocol
- Add black-box coverage for skip with buffered pieces, clear during playback,
  stop during gaps, failed synthesis, and persistent pause

## P1: source app and richer queue metadata

- Extend the queue protocol with a versioned metadata record instead of adding
  more meaning to the filename
- Record the source app, source task, queued timestamp, voice, requested gap,
  and original text for every chunk
- Keep `speak.sh` compatible for existing agents while adding optional source
  metadata flags
- Show a recognizable source-app icon only when the source is known
- Decide how much source history remains local and add a clear privacy control

## P2: playback and voice settings

- Voice picker with short local previews
- Default voice, speaking speed, output device, and volume controls
- Global pause/resume shortcut
- Optional mini-player that stays above other windows
- Light theme, high-contrast mode, reduced motion, and full keyboard navigation
- Friendly recovery for a missing audio device or a failed model load

## P2: maintenance

- Automatic updates with a user-controlled channel
- Crash reports that are local by default and exportable by the user
- Queue and engine diagnostics screen
- Model version management and cleanup
- Installer upgrade, rollback, and uninstall tests on Windows and macOS
