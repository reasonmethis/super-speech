# Desktop architecture

## Decision

Super Speech uses an Electron desktop app with an independent Python speech
engine.

- Python owns synthesis, queue order, playback, the current sample cursor, and
  engine status
- Electron main owns the window, tray, setup flow, updates, and future engine
  supervision
- The renderer displays status and sends a narrow set of commands through an
  isolated preload bridge
- Queue files remain the headless ingestion contract, so coding agents can add
  speech without the app being open
- The engine publishes one atomic `status.json` snapshot. Electron does not
  maintain a second playback state machine

Version zero uses the configured local Python installation. A public build will
ship the same engine as a platform-specific standalone folder outside
`app.asar`. The model will download once into `~/.super-speech` with checksum
verification and resumable transfer.

## Options considered

### Python-only app

This would use one language and could share playback state directly. A polished
cross-platform tray UI would still require Qt/QML or another substantial UI
runtime. Qt adds a separate packaging and licensing surface, while SpotKey's
Tkinter and pystray pattern is Windows-oriented and does not meet this app's UI
goal.

### Electron plus Python

This preserves the working Kokoro, ONNX Runtime, eSpeak, NumPy, and PortAudio
pipeline. Packaging a standalone helper is normal Electron distribution work
and follows Littlebird's current pattern for native executables. It also keeps
inference crashes and audio lifetime independent from the renderer.

This is the selected design.

### Electron and TypeScript only

This would remove Python source but not the native runtime problem. A port would
still need ONNX Runtime, eSpeak phonemization, waveform trimming, an audio
service, queue backpressure, device recovery, and sample-preserving pause. It is
a speech-engine rewrite rather than an installer simplification.

Reconsider it only after a focused prototype matches voice quality,
pronunciation, cold-start time, real-time synthesis, queue gaps, exact resume,
and signed Windows and macOS packaging.

### Tauri plus Python

Tauri added Rust as a third implementation language and duplicated the engine's
status shape without owning a separate product responsibility. Littlebird also
migrated its desktop app from Tauri to Electron in 2025. Keeping Tauri would
retain a known maintenance risk while saving little relative to the model and
inference runtime sizes.

## Protocol growth

Version zero needs only the atomic status file and persistent `PAUSE` signal.
Interactive queue selection will require a small versioned command protocol.
Do not introduce HTTP, WebSocket, or a general service framework until those
commands need behavior that cannot be expressed safely through atomic files.

## Distribution constraints

- Build the frozen Python engine separately for Windows x64, macOS x64, and
  macOS arm64
- Use a directory-style build so native libraries do not unpack on every start
- Keep models and mutable data outside the installed app
- Sign the Electron shell, installer, engine, and bundled native libraries
- Notarize the complete macOS app after nested code signing
- Audit transitive speech dependencies, especially eSpeak NG, before any
  closed-source distribution
