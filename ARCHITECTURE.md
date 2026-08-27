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

## Vocabulary

- A **Speechicle** is one `speak` request. It stays one timeline item and one
  replay target
- A **piece** is a sentence-sized part synthesized inside a Speechicle. Pieces
  reduce startup delay but never become separate timeline items
- **Current** is the one Speechicle at the playback boundary. It remains Current
  while it is being prepared, spoken, paused, or waiting between pieces
- **Waiting** contains the Speechicles that will play after Current
- **History** contains completed, skipped, and cleared Speechicles
- The **playback boundary** is Current's place in the ordered timeline. Moving
  that boundary changes which Speechicles are Waiting and which are History
- A **row** is only the card that represents a Speechicle in the desktop UI

## Code map

- `skills/super-speech/engine/super_speech_engine.py` owns the engine session,
  synthesis, playback, timeline persistence, commands, and status publication
- `skills/super-speech/engine/pauseable_audio.py` owns sample-accurate pause and
  resume inside the audio callback
- `skills/super-speech/engine/speechicle_identity.py` owns stable public IDs and
  migration from older filename-based identities
- `skills/super-speech/engine/mutation_protocol.py` validates the one timeline
  mutation wire format
- `app/electron/main.ts` owns the engine child process, tray, window, install
  manifest, and renderer IPC
- `app/src/runtime.ts` validates engine snapshots and defines the shared desktop
  data contract
- `app/src/main.ts` renders the window and handles playback, menus, and gestures
- `app/src/queue-drag-model.ts` is the pure drag-and-drop state machine
- `tests/test_pauseable_audio.py` covers the Python engine and its durable
  recovery paths
- `app/src/*.test.ts` covers the desktop data and gesture models without audio
- `app/scripts/smoke_*.mjs` covers packaged Electron behavior with silent audio

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

## Timeline and playback

### Status snapshot

`super-speech-engine status` publishes one version 12 snapshot. It contains
Current, the complete Waiting list, and up to 50 recent History rows. The total
`history_count` may be larger than that bounded History list. Every Speechicle
has an opaque ID such as `sp_0123456789abcdef0123456789abcdef`. Text filenames
and numeric storage sequences are private and may change without changing the
public ID.

Current stays present while the engine prepares audio, speaks, pauses, or waits
between pieces. Before its first piece starts, `piece` is 0 and `piece_start`
and `piece_end` are null. During a piece, those two fields give its zero-based,
end-exclusive Unicode code-point range inside the complete text. The renderer
uses that range for follow-along highlighting.

The status shape enforces these rules:

- Waiting cannot exist without Current
- Playing and Paused cannot exist without Current
- Idle cannot have Current
- Current, Waiting, and History cannot contain the same ID twice
- `queue_count` equals the number of Waiting rows

Loading, Setup required, and Stopped describe process lifecycle. They may still
show Current so the user does not lose the playback boundary while the engine is
starting or stopped. If status publication keeps failing, the engine writes
`status.failed` and stops. Electron treats that marker as invalidation, not as
permission to keep showing an older snapshot.

Each snapshot also has a `timeline_revision`. The engine increments it only when
row identity, row order, a row's voice, or the History total changes. Piece
progress, pause state, and timestamps do not increment it. The engine seeds the
counter from its last valid version 12 status when it restarts. The renderer
uses the revision first and `updated_at` second, so a late status poll cannot
replace a mutation result with an older timeline.

### Timeline mutations

Play, move, archive, delete, and clear all use one command shape and one durable
FIFO stream. The public CLI commands and the desktop app both enter that same
path. The engine claims one request at a time, applies it, publishes the new
status, and then publishes a matching result. Every result is one of:

- `committed`: the change took effect
- `rejected`: the engine made no requested change and explains why
- `unconfirmed`: storage may have changed but the engine cannot prove the final
  result, so it stops instead of continuing from an uncertain timeline

Every result includes its request ID and the authoritative status snapshot.
Play also returns the selected Speechicle ID. The renderer adopts that snapshot
instead of guessing success from matching text or rebuilding the timeline on
its own. A drag preview moves existing row nodes only for visual feedback; it
does not become application state.

`play <id> [--voice <voice>]` moves the playback boundary without changing the
visible row order:

- playing Current resumes it from the same sample when it is paused
- playing a Waiting row moves Current and each older Waiting row into History
- playing a History row makes that row Current and makes each row above it
  Waiting
- choosing another voice changes that Speechicle in place and keeps its ID and
  text

Changing the playback boundary discards buffered audio that belongs to the old
order. Current audio remains buffered when a simple Waiting or History reorder
does not change the boundary.

`move`, `move-history`, `archive`, `delete`, and `clear` use the same mutation
result contract. Queue and History order live in `queue-order.json` and
`history-order.json`, keyed by stable public IDs. The identity catalog in
`speechicle-index.json` maps those IDs to private storage sequences. New text is
written to a temporary file and becomes visible only through an atomic replace,
so concurrent `speak` calls cannot expose half-written rows.

`speak` does not enter the mutation stream. It makes one complete new
Speechicle visible at once. If the row is visible before the engine reads the
timeline, the mutation sees it. Otherwise the new row appears after the
mutation. No process ever sees part of a row.

Clear and History promotion can touch several files. Before either operation,
the engine writes the intended final layout to `timeline-intent.json`. Startup
finishes an interrupted intent before accepting another mutation. A known
in-process failure restores the old layout. An uncertain rollback keeps the
intent and stops the engine so the next process can recover it.

Pause, Resume, Skip, Stop, and Interrupt are immediate controls rather than
timeline mutations. Their signal files name the engine process that owns the
runtime lock, so a delayed control cannot affect a replacement process. A
mutation and a graceful Stop are ordered by publication time: a newer mutation
cancels an older Stop, while a newer Stop rejects the older mutation.

The engine stores Waiting in playback order, oldest first. The desktop reverses
that list, places Current below it, then places newest-first History below
Current. A row therefore crosses the Current-History divider without jumping
when playback finishes. A new Current row is scrolled into view once; piece
updates do not take scrolling away from the user. Current is not draggable.

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
