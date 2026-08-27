# Super Speech desktop app

Start with the canonical project overview in [../README.md](../README.md).
This document covers development and packaging for the Electron app.

The renderer is plain TypeScript and CSS. A sandboxed preload bridge exposes
status, pause, identifier-based playback and voice selection, Waiting and
History reordering, archival, deletion, clearing, setup, and window controls
without giving the renderer Node.js or
filesystem access. Electron forwards those commands to the engine CLI; the
frozen Python sidecar remains authoritative for synthesis, queue order, replay,
and the current sample cursor. Playback selection returns the engine's exact
resulting queue ID, so the renderer never infers acceptance from matching text.
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
voice changes, row action menus, and stable
playback-control geometry. It also verifies that Current is initially visible,
the three timeline sections remain explicit, and menus stay inside the visible
speech viewport. Waiting and History menus both use `Play`, `Change voice`, and
`Delete`; History deletion is permanent. The test then checks cancellation when the pointer loses its
primary button, the window loses focus, or polling replaces the timeline. It
also verifies compact and expanded follow-along text and that Clear all moves
Current plus Waiting into History. The test runs against a temporary runtime
with silent audio and verifies the result in both the renderer and the engine
queue.

Runtime state has one transport authority. Engine liveness gates playback. An
explicit Play action starts in Playing immediately, while a later Pause remains
authoritative during preparation. The engine publishes a first-piece receipt so
an archived row alone cannot be mistaken for successful replay. Timeline
mutations cannot remove or reorder the item currently being selected.
Failed optimistic mutations reconcile from the engine rather than restoring an
older renderer snapshot.

## Engine verification

The engine smoke test synthesizes and plays a silent-timing chunk with an
isolated runtime. It still exercises model loading, phonemization, ONNX
inference, PortAudio, queue archival, and shutdown.

```powershell
py -3.12 scripts/smoke_engine.py
```

The Electron binary also supports `--smoke-test`. It exits successfully only
after the supervised engine reaches idle with an empty queue.

## Packaging

```powershell
npm run package:win
```

This single command installs pinned build dependencies, verifies model hashes,
freezes the engine, builds Electron, and creates a current-user NSIS installer.
The package contains the engine, model, voices, complete agent skill bundle,
third-party notices, and corresponding Super Speech engine source.

Electron Builder is configured for a macOS arm64 DMG, but the native sidecar
must be built on Apple Silicon running macOS 14 or newer. Windows x64 is the
currently verified release target. Public releases still require code signing,
and macOS requires hardware smoke testing, nested signing, and notarization.
