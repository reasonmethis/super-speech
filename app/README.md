# Super Speech desktop app

Start with the canonical project overview in [../README.md](../README.md).
This document covers development and packaging for the Electron app.

The renderer is plain TypeScript and CSS. A sandboxed preload bridge exposes
status, pause, setup, and window controls without giving the renderer Node.js
or filesystem access. The frozen Python sidecar remains authoritative for
synthesis, queue order, playback, and the current sample cursor.

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
The package contains the engine, model, voices, agent skill, third-party
notices, and corresponding Super Speech engine source.

Electron Builder is configured for macOS x64 and arm64 DMGs, but each native
sidecar must be built on its target platform. Windows x64 is the currently
verified release target. Public releases still require code signing, and macOS
requires nested signing and notarization.
