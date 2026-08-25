# Desktop app backlog

Start with the canonical project overview in [README.md](README.md).

This is the ordered backlog for taking the version-zero controller into a
public Super Speech app. Each section is meant to be runnable as a focused
work session without having to reconstruct the product decisions.

## Version zero

- [x] Windows and macOS-friendly desktop architecture
- [x] Tray app with a compact status window
- [x] Immediate pause with sample-position-preserving resume
- [x] Persistent pause state across engine restarts
- [x] Current voice, current text, and scrollable queue with recent history
- [x] Electron architecture following Littlebird's packaged-helper precedent
- [x] Native Windows installer build through Electron
- [x] Self-contained Windows installer with the frozen engine, model, and voices
- [x] First-launch Codex and Claude skill installation without overwriting custom skills
- [x] Packaged-app and frozen-engine smoke tests against isolated runtime homes
- [x] One self-starting engine CLI shared by desktop and headless installations
- [x] Minimal headless installer with no repository path file
- [ ] Test the macOS bundle on Apple Silicon hardware

## P0: one-click public installation

- [x] Bundle the Kokoro engine as a directory-style sidecar outside `app.asar`
- [x] Bundle SHA-256-verified Kokoro model and voice data for offline first use
- [x] Install the Super Speech skill for existing Codex and Claude setups
- [x] Replace agent-assisted setup as the primary installation path
- Add an optional smaller installer that downloads verified model assets with
  visible progress, retry, and resume
- Add launch-at-login as an opt-in installer or settings choice
- Build signed Windows releases and signed, notarized macOS releases
- Add an automated release workflow for version tags
- Sign the Electron shell, installer, frozen engine, and bundled native
  libraries rather than signing only the outer app
- [x] Produce third-party notices and ship corresponding engine source
- Complete legal review before any closed-source commercial distribution

## P1: interactive queue

- [x] Let a user select any upcoming chunk and start it immediately
- [x] Let a user replay an archived chunk without losing the remaining queue
- [x] Keep the current chunk and sample position when the user only pauses
- Add previous and next controls using the identifier-based selection command
- [x] Add replay through the same identifier-based selection command
- Add remove controls
- [x] Add clear controls to the desktop UI
- Add drag-to-reorder controls
- Show when each chunk was queued, when it started, and when it finished
- Preserve enough rendered audio history to make replay instant
- Record completed, skipped, and cleared outcomes before labeling history as played
- [x] Define queue-selection behavior in tests before changing the file protocol
- Add black-box coverage for skip with buffered pieces, clear during playback,
  stop during gaps, failed synthesis, and persistent pause

## P1: source app and richer queue metadata

- Extend the queue protocol with a versioned metadata record instead of adding
  more meaning to the filename
- Record the source app, source task, queued timestamp, voice, requested gap,
  and original text for every chunk
- Add optional source metadata flags to `super-speech-engine speak`
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
