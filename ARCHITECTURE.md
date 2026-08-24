# Desktop architecture

Start with the canonical project overview in [README.md](README.md). This
document explains the desktop process boundary and distribution decisions.

## Decision

Super Speech uses an Electron desktop app with a separate frozen Python speech
engine.

- The Python engine owns synthesis, queue order, playback, the current sample
  cursor, daemon startup, playback commands, and the atomic `status.json`
  snapshot
- Electron main owns the window, tray, installer, agent integration, and engine
  supervision
- The renderer displays status and sends a narrow set of commands through a
  sandboxed preload bridge
- `super-speech-engine` is the public contract for the app, skills, and headless
  users. Its `speak` command starts the one engine process and reserves queue
  numbers atomically
- Runtime files remain a private, local protocol between engine processes. No
  web server or second playback state machine is needed

The installer places the directory-style frozen engine, Kokoro model, and
voices outside `app.asar`. Mutable queue, signal, status, and log files stay in
`~/.super-speech/`. An engine lock prevents the app and headless CLI from
starting competing processes. Electron stops only a child process it owns.

The headless installer creates a private Python environment and installs the
same module that is frozen into the desktop sidecar. The app is UI and
supervision on top of the engine, not a second drainer.

## Options considered

### Python-only app

This would use one implementation language, but a polished cross-platform tray
UI would still require Qt/QML or another substantial UI runtime. Qt adds a
separate packaging and licensing surface. SpotKey's Tkinter and pystray pattern
is Windows-oriented and does not meet this app's UI goal.

### Electron plus Python

This preserves the working Kokoro, ONNX Runtime, eSpeak, NumPy, and PortAudio
pipeline. A standalone helper is normal Electron distribution work and keeps
inference crashes and audio lifetime independent from the renderer.

This is the selected design.

### Electron and TypeScript only

This would remove Python source but not the native runtime problem. A port would
still need ONNX Runtime, phonemization, waveform trimming, an audio service,
queue backpressure, device recovery, and sample-preserving pause. It is a speech
engine rewrite rather than an installer simplification.

Reconsider it only after a focused prototype matches voice quality,
pronunciation, cold-start time, synthesis speed, queue gaps, exact resume, and
signed Windows and macOS packaging.

### Tauri plus Python

Tauri added Rust as a third implementation language and duplicated the engine's
status shape without owning a separate product responsibility. Littlebird also
migrated its desktop app from Tauri to Electron. Keeping Tauri would retain a
known maintenance risk while saving little relative to model and inference
runtime sizes.

## Protocol growth

Version zero uses CLI commands plus the atomic status file. Interactive queue
selection should extend that CLI with explicit chunk identifiers. Do not
introduce HTTP, WebSocket, or a general service framework until those commands
need behavior that the local process protocol cannot express safely.

## Distribution boundaries

- Build the frozen engine independently for Windows x64 and macOS arm64. The
  pinned ONNX Runtime requires macOS 14 or newer and does not provide an Intel
  Mac wheel
- Use a directory-style sidecar so native libraries do not unpack on every
  start
- Keep installed code and models read-only and mutable runtime state in the
  user's home directory
- Ship corresponding engine source, dependency notices, and license files
- Sign the Electron shell, installer, engine, and native libraries for public
  Windows releases
- Notarize the complete macOS app after nested code signing
- Treat the frozen engine as GPL-covered because it combines GPL phonemization
  components; keep the Electron frontend as a separate MIT process
