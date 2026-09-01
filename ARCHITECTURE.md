# Architecture

## Scope

This is the main technical guide for both Super Speech installations. Start
with [README.md](README.md) for the product overview.

The desktop app and headless skill use one Python speech engine. Electron adds
the window, tray, installer, and process supervision. It does not have another
timeline storage or playback implementation.

## System at a glance

Four parts work together:

1. The platform launcher is the command that an agent uses
2. The Python engine stores speech, creates audio, and plays it
3. Electron main starts and watches the engine, then passes approved commands
   between the window and the engine
4. The renderer draws the window from engine status

The launcher uses the desktop engine when `~/.super-speech/install.json` points
to a valid installation. Otherwise, it uses the private engine inside the
headless skill.

## Words used in this guide

- A **Speechicle** is one complete spoken reply. It stays one timeline row and
  one replay target
- A **piece** is a smaller sentence-sized part prepared inside a Speechicle
- **Current** is the one Speechicle at the playback boundary
- **Waiting** contains the Speechicles that will play after Current
- **Queue** contains Current and all Waiting Speechicles
- **History** contains inactive Speechicles. Rows enter it after playback
  finishes, Skip, Clear all, manual archive, or a synthesis failure after
  playback began
- The **playback boundary** is Current's place between Waiting and History
- A **row** is the card that represents a Speechicle in the desktop window

Older command names and internal variables sometimes use `chunk` for a
Speechicle. A `piece` always means one smaller synthesis unit inside it.

## One spoken reply from start to finish

1. The agent calls the platform launcher once with the complete spoken reply
2. The launcher chooses the desktop or headless engine
3. The engine stores one Speechicle in Queue
4. The engine splits its text into pieces and prepares them in order
5. Playback begins as soon as the first piece is ready
6. Engine status tells the app which piece is current
7. After the last piece finishes, the stored file moves from Queue to `spoken/`
   and appears in History

Preparing smaller pieces reduces the delay before speech begins. The pieces do
not become separate rows and do not change replay behavior.

## Stored data

Desktop state lives in `~/.super-speech/`. Headless state lives in `runtime/`
inside the installed skill.

Each runtime has three speech directories:

- `queue/` owns Current and Waiting membership
- `spoken/` owns History membership
- `failed/` holds speech that could not be prepared

Optional source labels and agent inbox paths are stored in `sources/` as small
versioned JSON files keyed by public Speechicle ID. The directory keeps its
legacy name, but version 2 records both fields. Metadata therefore follows the
same Speechicle across voice changes and moves between Queue and History.
Version 1 source-only files remain readable. Missing or damaged metadata never
prevents playback.

A Queue filename looks like:

```text
001-sp_0123456789abcdef0000000000000001-af_heart-g250-say.txt
```

The filename stores a local sequence, public ID, voice, and optional gap. The
public ID stays with the Speechicle when it moves or changes voice. Callers copy
the ID exactly and do not need to understand the filename.

`next-sequence.json` stores a random installation ID and the next number to use.
Startup creates or repairs it after checking the complete stored timeline.
Normal `speak` calls then advance this small file without scanning History. The
installation ID and sequence together form each public ID. The counter advances
before its Queue file is published, so a crash can leave a skipped number but
cannot reuse the reserved public ID during normal operation.

Physical directories decide which section contains a Speechicle.
`queue-order.json` and `history-order.json` store only the relative display
order inside their sections. Startup can repair a valid order file that missed
a completed file move. It can also remove one proven exact Queue and History
copy left by an old interrupted playback. It stops on malformed data, duplicate
live sequences, or any duplicate ID whose ownership is unclear.

Older installations may contain `speechicle-index.json`. It is read only while
upgrading old filenames, then deleted after the new files and order data have
been checked.

## Timeline and playback rules

The visible window keeps newest Waiting rows above Current and History below
Current. When Current finishes, the same row crosses into the top of History
without changing the order of the cards around it.

Selecting a row moves the playback boundary without reordering the list:

- Selecting Current resumes it from the same sample when paused
- Selecting a Waiting row moves Current and the older Waiting rows below it
  into History
- Selecting a History row makes it Current and makes the rows above it Waiting
- Selecting another voice keeps the same ID, text, and row position

Only a drag changes relative order. Reordering Waiting or History does not
replace Current or discard its audio.

Current remains Current while its first piece is being prepared, while it is
playing, while paused, and during a gap between pieces. Idle means there is no
Current and no Waiting. These rules keep states such as Paused without Current,
or Waiting without Current, out of engine status and out of the window model.

## Status sent to the app

The engine writes a versioned `status.json` snapshot. Source code owns the exact
version number and field checks.

Each snapshot contains:

- Current, all Waiting rows, and up to 50 recent History rows
- The total History count, which may be larger than the visible list
- Engine process ID, lifecycle state, and publication time
- A timeline revision that increases when visible identity, order, voice, or
  source label, inbox availability, or History count changes
- The current piece number and its Unicode text offsets

For historical protocol compatibility, JSON `current` contains Current while
JSON `queue` contains Waiting only. The conceptual Queue is both fields
together.

The renderer uses the piece offsets to show the current piece without
changing the text's line breaks. It uses the revision before the publication
time, so a late status read cannot undo a newer command result.

The engine writes status to a temporary file and then swaps it into place, so
the app never reads half a snapshot. Short Windows replacement collisions are
retried. A persistent failure creates `status.failed`; the app then stops
trusting the previous snapshot.

## Commands and saved timeline changes

Headless callers send commands through the engine CLI. Electron sends desktop
commands to an authenticated loopback endpoint owned by the already-running
engine. This avoids starting a Python process for each click while keeping
command behavior in Python. The endpoint file includes a random token and the
owning process ID, so Electron rejects stale endpoints after an engine restart.

Pause and Resume use an ordered `PAUSE` marker, so a paused timeline stays
paused across an engine restart. Skip, Stop, and Interrupt name the engine
process they belong to, so an old destructive command cannot affect a
replacement process.

Pause and Resume return only after the live audio object acknowledges the
applied state. A single ordered background writer then saves the compatibility
marker, so disk latency is not part of the click-to-audio path. The renderer
does not predict the result. Clear all owns a separate Clearing state. It closes
the live audio gate before publishing the saved timeline transaction and keeps
the old stream silent until that stream detaches. Clearing never writes a Pause
marker or exposes Paused, and Ready appears only after the engine confirms the
commit.

Enqueue from the desktop, Play, move, archive, delete, voice change, and clear
use one kind of saved change file. Requests are stored on disk and handled one
at a time in creation order. Each result says:

- `committed` when the change finished
- `rejected` when nothing changed and the engine can explain why
- `unconfirmed` when storage may have changed but the engine cannot prove the
  result

An unconfirmed result stops further timeline changes until startup recovery
finishes the saved plan. The app adopts the engine's result snapshot instead of
guessing from local UI state.

## Agent inbox messages

An agent may pass one absolute file path with `speak --inbox`. The path becomes
optional metadata for that Speechicle. It is not part of Queue order or
playback state.

For an inbox-enabled row, the renderer can ask Electron main to send a message
using only the public Speechicle ID and text. Electron main reads checked engine
status again, finds the matching inbox itself, and appends one JSON object plus
a newline. The renderer never chooses a destination path. Appends are
serialized, flushed before success is reported, and do not change the timeline
revision.

Every message has protocol version 1, kind `user_message`, a unique message ID,
UTC time, the Speechicle ID, optional source label, and user text. The engine's
`listen-inbox` command creates the file, emits complete saved lines, and then
follows complete new lines. JSON Lines keeps message boundaries clear and lets
an agent deduplicate after restarting its listener.

The inbox is durable delivery, not an agent wake-up service. The receiving task
must keep a listener running or inspect saved messages when it resumes.

See [skills/super-speech/SKILL.md](skills/super-speech/SKILL.md) for the exact
agent commands.

## Startup, locking, and crash recovery

Only the process holding `engine.lock` may prepare the runtime. It first removes
stale command markers from the previous process, then publishes Loading status
and a heartbeat. The app can show a real startup state even when an old timeline
takes time to upgrade.

Preparation runs before normal work:

1. Finish an interrupted old identity or timeline change
2. Upgrade old filenames and order files when needed
3. Check the current files and repair safe order gaps
4. Create or repair the sequence counter
5. Remove the old identity index
6. Open the runtime to commands

Agent commands wait for startup checks to finish before touching the timeline.

`TimelineStorage` uses one thread lock and one cross-process `timeline.lock` for
changes that must agree across several files. Normal enqueue reads only the
small counter and writes Queue, so its lock time does not grow with History.
Playback-side moves wait for the current writer rather than killing the engine
after an arbitrary timeout. Separate command processes still have a time limit,
so a truly stuck command can report an error.

Multi-file changes first save their intended final layout in
`timeline-intent.json`. Startup checks that plan and finishes it before another
timeline change can begin.

## Desktop process boundary

Electron main starts the standalone engine and watches it. If an agent command
started a compatible engine first, the app uses that same process. If it later
exits, the app starts a replacement with increasing delays between repeated
failures.

The preload script exposes a small list of approved functions to the window.
The renderer cannot read files, start programs, or use Node.js. It receives
checked status and sends commands through Electron main.

## Code map

- `skills/super-speech/engine/timeline_storage.py` owns stored speech files,
  section order, allocation, locks, upgrade plans, and crash recovery
- `skills/super-speech/engine/file_lock.py` provides the small cross-process lock
  used by the engine and timeline storage
- `skills/super-speech/engine/engine_control.py` owns the authenticated local
  endpoint and audio-state acknowledgement
- `skills/super-speech/engine/super_speech_engine.py` owns synthesis, playback,
  commands, engine lifecycle, and status
- `skills/super-speech/engine/pauseable_audio.py` owns sample-accurate pause and
  resume in the audio callback
- `skills/super-speech/engine/speechicle_identity.py` describes old-to-current
  filename upgrades
- `skills/super-speech/engine/mutation_protocol.py` checks timeline mutation
  requests and results
- `skills/super-speech/engine/inbox_listener.py` follows complete agent inbox
  messages
- `app/electron/main.ts` owns the window, tray, engine supervision, installer
  integration, and calls from the renderer
- `app/electron/agent-inbox.ts` validates and appends user messages without
  accepting a path from the renderer
- `app/electron/engine-control.ts` validates the endpoint and sends desktop
  commands to the running engine
- `app/electron/atomic-file.ts` safely replaces the desktop install manifest
- `app/electron/managed-skill.ts` preserves or updates app-managed agent skills
- `app/electron/tray-menu.ts` maps engine state to the tray playback action
- `app/src/runtime.ts` checks engine status and defines the desktop data shapes
- `app/src/main.ts` renders the window and handles its controls
- `app/src/timeline-drag-model.ts` contains the pure drag state machine
- `tests/test_timeline_storage.py` covers storage directly
- `tests/engine_test_support.py` provides shared, silent engine test fixtures
- `tests/test_engine_protocol.py` covers public status and mutation shapes
- `tests/test_engine_lifecycle.py` covers commands, status, startup, and controls
- `tests/test_engine_playback.py` covers selection, synthesis, and playback
- `tests/test_engine_timeline.py` covers engine-level mutations and recovery
- `app/src/*.test.ts` covers desktop status and drag logic without audio
- `app/scripts/smoke_*.mjs` covers real Electron behavior with silent audio

## Distribution and licensing boundaries

- Build the standalone engine separately for Windows x64 and macOS arm64
- Keep installed code and models read-only, with mutable desktop state in the
  user's home directory
- Keep every headless component inside its installed skill folder
- Ship engine source, dependency notices, and license files with the installer
- Sign the Electron app, installer, engine, and native libraries for a public
  Windows release
- Sign nested macOS code, then notarize the complete app

The standalone engine contains GPL-covered phonemization components. The
Electron frontend remains a separate MIT process. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for dependency details.

## Options considered

### Python-only app

This would use one language, but a polished cross-platform tray UI would still
need Qt/QML or another large UI runtime. The earlier Tkinter and pystray
prototype was Windows-focused and did not meet this app's UI goal.

### Electron plus Python

This keeps the working Kokoro, ONNX Runtime, eSpeak, NumPy, PortAudio, and
sample-accurate pause code. The standalone engine also keeps synthesis and audio
failures outside the renderer.

This is the selected design.

### Electron and TypeScript only

This removes Python source but still needs native inference, phonemization,
audio, device recovery, buffering, and pause behavior. It is an engine rewrite,
not a packaging shortcut.

### Tauri plus Python

This adds Rust as a third language without removing the Python engine. An
earlier Tauri attempt also had enough desktop integration problems that this
project chose Electron instead.
