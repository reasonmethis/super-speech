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

`super-speech-engine status` publishes a version 8 snapshot with the current
speech item, the complete upcoming queue, and up to 50 recent archived items in
their saved display order. A
speech item is one `speak` invocation, one timeline row, and one replay target.
Sentence pieces are an internal synthesis and buffering detail, not separate
history entries. The current item remains active while the engine waits for its
next rendered piece. Every entry has an opaque `id`. The total `history_count`
can exceed the bounded
`history` array.
`queue_count` always matches the complete `queue` array. A bounded
`recent_starts` receipt list lets the renderer distinguish speech that actually
began from an archived replay that failed before its first audio piece.
The archive currently includes completed, skipped, and cleared items, so the
UI calls it History rather than claiming every entry played to completion.

`super-speech-engine play <id> [--voice <voice>]` is the one selection command.
The optional voice queues the same text and gap under that voice. The engine, not
Electron, validates the identifier and owns every queue or archive mutation:

- selecting the current item resumes it from the same sample when paused
- selecting an upcoming item archives the current item and every older waiting
  item before it, then plays the selection without reordering newer items
- selecting an archived item queues a working copy under the same ID, preserving
  the original archive and the untouched upcoming queue without adding another
  History row when replay finishes

Selection invalidates rendered pieces that no longer match the chosen order.
Each selection uses an atomic request file and a private acknowledgement that
reports the exact resulting queue ID or an engine rejection. If several
requests arrive before one poll, the engine rejects the superseded requests
and accepts the newest one. A request remains recoverable until its
acknowledgement is durable; an unclaimed timeout cancels the request before the
caller reports failure. No HTTP, WebSocket,
second queue, or renderer playback state machine is needed.
Immediate control files carry the lock owner's process ID. A delayed Stop,
Interrupt, Skip, or Clear command is discarded if a successor engine has taken
ownership, so a command cannot cross an engine restart boundary.

Queue order is stored separately from the opaque chunk filenames. The engine
filters that saved order against live queue files and appends new arrivals, so
dragging never renames an ID and concurrent `speak` calls remain safe.
`move <id> [before-id]` changes that order, while `archive <id>` moves one
waiting item into History and `delete <id>` permanently removes one History
item. History display order is likewise stored in `history-order.json`, and
`move-history <id> [before-id]` reorders the bounded recent History view without
renaming or moving archive files. These commands use unique request files and
exact acknowledgements. Queue text is written under a temporary name and becomes
visible to the worker only after an atomic replace. The engine
invalidates buffered waiting audio after queue-order mutations while preserving
every rendered piece of the current item. If a queue file cannot be archived,
the engine stops and leaves that durable file visible for the next engine process
to retry instead of retaining an unreleasable live claim.

The engine status keeps waiting items in playback order, oldest first. The
desktop timeline displays that list in reverse so new arrivals enter at the top,
then places the current item above newest-first History. Section dividers and a
Speechicles heading identify the timeline, and a new current ID is scrolled into
view once instead of on every status poll. When current playback
finishes, the same row therefore crosses the divider without changing its
position relative to the other rows. Waiting and History drags translate back
into `move` and `move-history` commands. The renderer applies the result
immediately and reloads the authoritative snapshot if the engine rejects the command. Current
speech is not reorderable. The status producer derives active state from the
same current and queue snapshot, and the renderer uses a discriminated playback
presentation, so Playing and Paused cannot exist without an active item.
The process lifecycle remains authoritative: a dead engine is Stopped even when
Waiting rows remain, and an accepted selection may change the displayed item but
cannot override Paused. Timeline mutations cannot target a selection that is
still starting.

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
