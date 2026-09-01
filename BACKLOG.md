# Desktop app backlog

Start with the canonical project overview in [README.md](README.md).

This roadmap tracks finished foundations and the remaining work for a public
Super Speech release. Checked items describe the current product. Unchecked
items are future work, ordered by priority.

## Version zero

- [x] Windows and macOS-friendly desktop architecture
- [x] Tray app with a compact status window
- [x] Immediate pause with sample-position-preserving resume
- [x] Persistent pause state across engine restarts
- [x] Current voice, current text, and scrollable queue with recent history
- [x] Compact and expanded follow-along text with active sentence highlighting
- [x] Recoverable Clear all for Current plus Waiting
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

- [x] Let a user select any upcoming Speechicle and start it immediately
- [x] Let a user move playback to any archived Speechicle without changing timeline order
- [x] Keep the current Speechicle and sample position when the user only pauses
- Add previous and next controls using the identifier-based selection command
- [x] Add replay through the same identifier-based selection command
- [x] Add per-item move-to-History controls
- [x] Add permanent deletion for individual History items
- [x] Add clear controls to the desktop UI
- [x] Add mouse and keyboard queue reordering
- [x] Add mouse and keyboard reordering for recent History
- [x] Change one item's voice and play the same text
- Show when each Speechicle was queued, when it started, and when it finished
- Preserve enough rendered audio history to make replay instant
- Record completed, skipped, and cleared outcomes before labeling history as played
- [x] Define queue-selection behavior in tests before changing the file protocol
- [x] Return an engine-owned acknowledgement for selection and replay
- [x] Add a silent full-loop test for archived replay and queue preservation
- [x] Let inbox-enabled agents receive a reply from a Speechicle's menu
- Add black-box coverage for skip with buffered pieces, clear during playback,
  stop during gaps, failed synthesis, and persistent pause

## P1: source app and richer queue metadata

- [x] Extend the queue protocol with a versioned metadata record instead of
  adding more meaning to the filename
- Record the source app, source task, queued timestamp, voice, requested gap,
  and original text for every Speechicle
- [x] Add optional source metadata flags to `super-speech-engine speak`
- Show a recognizable source-app icon only when the source is known
- Decide how much source history remains local and add a clear privacy control

## P2: playback and voice settings

- Add short local previews to the per-item voice picker
- Default voice, speaking speed, output device, and volume controls
- Global pause/resume shortcut
- Optional mini-player that stays above other windows
- [x] Persisted Dark and Light appearance themes
- High-contrast mode, reduced motion controls, and full keyboard navigation
- Friendly recovery for a missing audio device or a failed model load

## P2: maintenance

- Automatic updates with a user-controlled channel
- Crash reports that are local by default and exportable by the user
- Queue and engine diagnostics screen
- Model version management and cleanup
- Installer upgrade, rollback, and uninstall tests on Windows and macOS
