# Super Speech desktop app

Start with the canonical project overview in [../README.md](../README.md).
This document covers development and packaging for the Electron app.

The desktop app has three parts. The renderer draws the window with plain
TypeScript and CSS. Electron main owns the window, tray, and installed app. The
bundled Python engine owns speech synthesis, queue order, replay, and the current
audio position.

The renderer cannot use Node.js or access files directly. A sandboxed preload
bridge gives it a small set of commands for status, playback, voice selection,
reordering, deletion, clearing, setup, and window controls. Electron main sends
those commands to the engine CLI. The engine returns the exact result and new
timeline after each change, so the renderer does not guess what happened.

The renderer projects one timeline with Waiting, Current, and History dividers,
under the Speechicles heading. It reveals a new current row once, then leaves
scrolling under user control while status polling preserves the existing row
nodes. The renderer stores its Dark or Light appearance choice in the app's
local profile; speech and queue state remain engine-owned.

The compact playback card renders the engine's exact active synthesis piece.
Clicking its text expands the card over the window, shows the complete Current
text, and highlights that same piece. The renderer applies the engine's Unicode
code-point offsets with code-point-aware string slicing, so non-BMP characters
do not shift the highlight. Escape collapses the card.

Current is the playback boundary rather than an audio-device event. Selecting a
History row makes it Current in place and promotes every row above it to Waiting.
The renderer rejects a runtime snapshot that contains Waiting without Current,
or Playing, Paused, or Idle states that contradict that boundary.

The shared terms and exact state rules live in
[Desktop architecture](../ARCHITECTURE.md#vocabulary). In short, a Speechicle is
one user-visible timeline item, a piece is one internal synthesis segment, and
Current is the boundary between Waiting and History.

## Code map

- `electron/main.ts` supervises the Python engine and owns tray, window, install
  manifest, and IPC behavior
- `electron/preload.ts` exposes the narrow sandboxed renderer bridge
- `src/runtime.ts` validates engine snapshots and defines the desktop contract
- `src/main.ts` renders the window and handles playback, menus, and gestures
- `src/queue-drag-model.ts` contains the pure drag state machine
- `src/*.test.ts` covers those data and gesture rules without audio
- `scripts/smoke_drag.mjs` drives the packaged UI with silent audio
- `scripts/smoke_installed.mjs` verifies the installed app and engine supervisor

The Python ownership map is in
[Desktop architecture](../ARCHITECTURE.md#code-map).

## Prerequisites

Release builds require Node.js 22+ and Python 3.12. These are build-time tools
only. The installed application bundles its runtime dependencies.

```powershell
npm install
```

## Focused development

Prepare the model and frozen sidecar, then run or check Electron:

```powershell
npm run resources
npm run dev
npm run check
npm run build
```

`SUPER_SPEECH_BUILD_PYTHON` can point at a specific Python 3.12 executable. On
Windows the resource script otherwise uses `py -3.12`; on macOS it uses
`python3`.

The app reads and writes the shared runtime under `~/.super-speech/`. Set
`SUPER_SPEECH_HOME` to an isolated directory for tests. The engine and model
paths can be overridden with `SUPER_SPEECH_ENGINE_PATH` and
`SUPER_SPEECH_MODEL_DIR`.

`npm test` covers status ownership, timeline order, and drag-state transitions
without starting Electron or touching an audio device. It exercises every queue
position plus adversarial cancellation and stale-pointer sequences. Renderer
screenshots can use the browser-only demo status produced when the preload
bridge is absent.

Run the Electron mouse test after changing queue interactions:

```powershell
npm run test:drag
```

It uses real pointer input to reorder Waiting and History cards and archive
Waiting cards, verifies single-click expansion, double-click-only playback,
voice changes, row action menus, and stable playback-control geometry. It also
verifies that Current is initially visible,
the three timeline sections remain explicit, and menus stay inside the visible
speech viewport. Waiting and History menus both use `Play`, `Change voice`, and
`Delete`; History deletion is permanent. The test then checks cancellation when
the pointer loses its primary button, the window loses focus, or polling
replaces the timeline. It also verifies compact and expanded follow-along text
and that Clear all moves
Current plus Waiting into History. The test runs against a temporary runtime
with silent audio and verifies the result in both the renderer and the engine
queue.

Runtime state has one transport authority. Engine liveness gates playback. An
explicit Play action is shown as Playing immediately, while a later Pause stays
authoritative during preparation. Play, move, archive, delete, and clear all use
one engine mutation contract. Each result carries the authoritative status
snapshot, so the renderer does not infer success or restore an older local copy.
A monotonic timeline revision prevents late status polls from undoing a
committed result.

## Engine verification

The engine smoke test synthesizes and plays a silent-timing Speechicle with an
isolated runtime. It still exercises model loading, phonemization, ONNX
inference, PortAudio, queue archival, and shutdown.

```powershell
py -3.12 scripts/smoke_engine.py
```

The Electron binary also supports `--smoke-test`. It waits for the bundled
engine to become healthy, terminates that engine unexpectedly, verifies that
the desktop supervisor starts a different engine process, and requires the
replacement to remain healthy for five seconds.

## Packaging

```powershell
npm run package:win
```

This single command installs pinned build dependencies, verifies model hashes,
freezes the engine, builds Electron, and creates a current-user NSIS installer.
The package contains the engine, model, voices, complete agent skill bundle,
third-party notices, and corresponding Super Speech engine source.

After installing that package, verify the installed files rather than the
repository build:

```powershell
npm run test:installed
```

This launches the installed app with an isolated runtime and silent audio, then
runs the engine supervision smoke test above. Treat this as a required release
check before reopening the app against the user's real queue.

Electron Builder is configured for a macOS arm64 DMG, but the native sidecar
must be built on Apple Silicon running macOS 14 or newer. Windows x64 is the
currently verified release target. Public releases still require code signing,
and macOS requires hardware smoke testing, nested signing, and notarization.
