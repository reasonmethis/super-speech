# Desktop architecture

Start with the canonical project overview in [README.md](README.md). This
document explains the desktop process boundary and distribution decisions.

## Decision

Super Speech uses an Electron desktop app with a separate frozen Python speech
engine.

- The Python engine owns synthesis, queue order, playback, the current sample
  cursor, daemon startup, playback commands, and the atomic `status.json`
  snapshot
- Electron main owns the window, tray, installer, and agent integration. While
  the app is running, it also supervises the engine process it starts
- Electron main adapts the engine's private status and signal files into a
  narrow, sandboxed renderer bridge
- The renderer displays status and sends playback commands through that bridge
- `super-speech-engine` is the public contract for the app, skills, and headless
  users. Its `speak` command starts the one engine process and reserves queue
  numbers atomically
- Runtime files remain a private, local protocol owned by the engine and read
  by Electron main. No web server or second playback state machine is needed

The desktop installer places the directory-style frozen engine, Kokoro model,
and voices outside `app.asar`. Its mutable queue, signal, status, and log files
stay in `~/.super-speech/`. Electron normally stops only a child process it
owns. At startup, it may interrupt an incompatible older engine holding the
desktop runtime lock so the bundled engine can replace it safely.

The headless installer creates `runtime/` inside the installed skill. That
directory contains its private Python environment, models, queue, status, and
logs. It installs the same module that is frozen into the desktop sidecar. The
app is UI and supervision on top of the engine, not a second drainer.

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

## Playback protocol

`super-speech-engine status` publishes a version 4 snapshot with the current
speech item, the complete upcoming queue, and up to 50 newest archived items. A
speech item is one `speak` invocation, one timeline row, and one replay target.
Sentence pieces are an internal synthesis and buffering detail, not separate
history entries. The current item remains active while the engine waits for its
next rendered piece. Every entry has an opaque `id`. The total `history_count`
can exceed the bounded
`history` array.
The archive currently includes completed, skipped, and cleared items, so the
UI calls it History rather than claiming every entry played to completion.

`super-speech-engine play <id>` is the one selection command. The engine, not
Electron, validates the identifier and owns every queue or archive mutation:

- selecting the current item resumes it from the same sample when paused
- selecting an upcoming item preempts the current output, plays the selection,
  then later restarts the interrupted item from its beginning
- selecting an archived item queues a working copy under the same ID, preserving
  the original archive and the untouched upcoming queue without adding another
  History row when replay finishes

Selection invalidates rendered pieces that no longer match the chosen order.
Each selection uses an atomic request file and a private acknowledgement that
reports the exact resulting queue ID or an engine rejection. If several
requests arrive before one poll, the engine rejects the superseded requests
and accepts the newest one, so every CLI caller terminates. No HTTP, WebSocket,
second queue, or renderer playback state machine is needed.

## Distribution boundaries

- Build the frozen engine independently for Windows x64 and macOS arm64. The
  pinned ONNX Runtime requires macOS 14 or newer and does not provide an Intel
  Mac wheel
- Use a directory-style sidecar so native libraries do not unpack on every
  start
- Keep desktop code and models read-only, with mutable desktop state in the
  user's home directory. Keep every headless component inside its skill folder
- Ship corresponding engine source, dependency notices, and license files
- Sign the Electron shell, installer, engine, and native libraries for public
  Windows releases
- Notarize the complete macOS app after nested code signing
- Treat the frozen engine as GPL-covered because it combines GPL phonemization
  components; keep the Electron frontend as a separate MIT process
