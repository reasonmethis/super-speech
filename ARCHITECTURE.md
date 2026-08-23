# Desktop architecture

Start with the canonical project overview in [README.md](README.md). This
document explains the desktop process boundary and distribution decisions.

## Decision

Super Speech uses an Electron desktop app with a separate frozen Python speech
engine.

- The Python engine owns synthesis, queue order, playback, the current sample
  cursor, and the atomic `status.json` snapshot
- Electron main owns the window, tray, installer, agent integration, and engine
  supervision
- The renderer displays status and sends a narrow set of commands through a
  sandboxed preload bridge
- Queue files are the headless ingestion contract, so agents do not need a web
  server or a second playback state machine
- The persistent `PAUSE` file is the shared pause contract, including across
  engine restarts

The installer places the directory-style frozen engine, Kokoro model, and
voices outside `app.asar`. Mutable queue, signal, status, and log files stay in
`~/.super-speech/`. Electron starts only an engine it owns. If a compatible
legacy engine is already alive, Electron adopts its status and leaves that
process running when the app exits.

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

Version zero uses the atomic status file and persistent pause signal.
Interactive queue selection will require a small versioned command protocol.
Do not introduce HTTP, WebSocket, or a general service framework until those
commands need behavior that atomic files cannot express safely.

## Distribution boundaries

- Build the frozen engine independently for Windows x64, macOS x64, and macOS
  arm64
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
