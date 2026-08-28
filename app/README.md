# Super Speech desktop app

This guide covers Electron development, testing, and packaging. Start with the
[project README](../README.md) for installation choices and
[ARCHITECTURE.md](../ARCHITECTURE.md) for the engine and timeline design.

Electron owns the window, tray, installer, and engine supervision. The shared
Python engine owns synthesis, playback, and stored speech.

Run the commands in this guide from the `app/` directory unless a section says
otherwise.

## Code map

- `electron/main.ts` owns the window, tray, engine process, install manifest,
  and calls from the renderer
- `electron/atomic-file.ts` replaces the install manifest without exposing a
  half-written file
- `electron/managed-skill.ts` owns agent-skill hashing and safe updates
- `electron/tray-menu.ts` maps engine state to the tray playback action
- `electron/preload.ts` exposes a small approved API to the window
- `src/runtime.ts` checks engine status and mutation results
- `src/main.ts` renders the window and handles controls, menus, and gestures
- `src/timeline-drag-model.ts` contains the drag state machine
- `src/*.test.ts` covers status and drag logic without Electron or audio
- `scripts/smoke_engine.py` checks the packaged engine with silent audio
- `scripts/smoke_drag.mjs` drives real Electron pointer input against an
  isolated silent runtime
- `scripts/smoke_installed.mjs` checks the installed executable and bundled
  engine

## Prerequisites

Release builds require Node.js 22.12 or newer and Python 3.12. End users do not
need either runtime.

## Install development dependencies

```powershell
npm install
py -3.12 -m pip install -r ..\requirements-build.txt
py -3.12 -m pip install pytest
```

On macOS, use `python3.12` in place of `py -3.12`:

```bash
npm install
python3.12 -m pip install -r ../requirements-build.txt
python3.12 -m pip install pytest
```

## Run the app locally

Prepare the engine and model resources, then start the development server:

```powershell
npm run resources
npm run dev
```

On macOS, select Python 3.12 explicitly when preparing resources:

```bash
SUPER_SPEECH_BUILD_PYTHON=python3.12 npm run resources
npm run dev
```

Resource preparation verifies the model hashes and builds the standalone
engine used by Electron.

## Test changes

Run the fast checks first:

```powershell
py -3.12 -m pytest -q ..\tests
npm test
npm run check
npm run build
```

- `pytest` runs the Python storage, playback, upgrade, and command tests
- `npm test` runs TypeScript status, drag, and atomic-file tests
- `npm run check` runs TypeScript type checking
- `npm run build` creates the production renderer and Electron files

After engine or packaging changes, run the silent engine smoke test:

```powershell
npm run resources
py -3.12 scripts/smoke_engine.py
```

After changing renderer interactions, status handling, or engine supervision,
run the Electron pointer test:

```powershell
npm run resources
npm run test:drag
```

This test uses the repository build, a staged engine, real Electron pointer
events, and a temporary runtime. Audio timing is exercised without sending
sound to the user's speakers.

## Build the installer

```powershell
npm run package:win
```

On Apple Silicon macOS 14 or newer, build the untested DMG target with:

```bash
SUPER_SPEECH_BUILD_PYTHON=python3.12 npm run package:mac
```

These commands prepare resources, build Electron, and write the Windows NSIS
installer or macOS DMG under `release/<version>/`. Node dependencies must
already be installed.

The package contains the Electron app, the standalone engine, Kokoro model and
voices, the agent skill, source required for redistribution, licenses, and
third-party notices.

## Release checks

On Windows, install the newly built package, then test the installed executable
rather than the repository build:

```powershell
npm run test:installed
```

On macOS, point the same test at the installed app executable:

```bash
SUPER_SPEECH_INSTALLED_APP="/Applications/Super Speech.app/Contents/MacOS/Super Speech" npm run test:installed
```

The installed smoke test uses an isolated runtime and silent audio. It checks
that the packaged app starts its bundled engine and replaces it after an
unexpected exit.

Windows x64 is the tested package target. Public Windows releases still need
code signing. The macOS arm64 target requires Apple Silicon running macOS 14 or
newer, hardware testing, nested signing, and notarization. This repository does
not currently publish either package automatically.

## Test-only environment overrides

- `SUPER_SPEECH_BUILD_PYTHON` selects the Python 3.12 executable used to build
  the standalone engine
- `SUPER_SPEECH_HOME` selects an isolated runtime directory
- `SUPER_SPEECH_ENGINE_PATH` selects a staged engine executable
- `SUPER_SPEECH_MODEL_DIR` selects the Kokoro model directory
- `SUPER_SPEECH_INSTALLED_APP` selects the executable tested by
  `npm run test:installed`; without it, the test uses the standard Windows
  current-user install path

Use these overrides for development and tests. Normal installed use reads the
paths written by the desktop installer.
