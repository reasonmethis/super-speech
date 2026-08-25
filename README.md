# Super Speech

Super Speech gives AI coding agents private local voice replies. Kokoro speech
synthesis runs entirely on the user's computer with no cloud service, API key,
or per-word billing.

This README is the canonical project entry point. The narrower documents cover
[desktop architecture](ARCHITECTURE.md), [app development](app/README.md), the
[product backlog](BACKLOG.md), [headless setup](SETUP.md), and the
[agent-facing skill contract](skills/super-speech/SKILL.md).

## Install

On Windows, download and run the latest x64 setup file from
[GitHub Releases](https://github.com/reasonmethis/super-speech/releases/latest).
The installer includes:

- the Electron tray app and status window
- a frozen Python speech engine
- the Kokoro v1.0 model and all bundled voices
- ONNX Runtime, eSpeak NG, phonemization, and audio libraries
- the canonical Super Speech skill bundle for existing Codex and Claude installations

The installed app does not need Python, Node.js, Git Bash, Rust, this repository,
or a model download. First launch starts the engine and writes its local paths to
`~/.super-speech/install.json`. It installs the agent skill only when the
corresponding `.codex` or `.claude` directory already exists. Updates replace a
previously managed skill only when the user has not modified it.

The Windows x64 package is verified. Electron and the process boundary are
designed for macOS, but the pinned ONNX Runtime currently limits the Python
engine to Apple Silicon on macOS 14 or newer. That bundle has not yet been
tested on Apple Silicon hardware.

## Use

Ask your agent:

> Use super-speech for your replies until I tell you otherwise

The agent invokes `super-speech-engine speak`, which starts the installed engine
when needed and queues each speech item. One item stays one timeline row and one
replay target even though the engine renders it as smaller sentence pieces. The
desktop window and tray menu
control that same engine and can pause immediately at the current audio sample,
then resume from that exact point. The window also shows the current voice,
current text, and a scrollable queue. Click an upcoming row to start it
immediately. The interrupted item stays queued and restarts from its beginning
later. Recent archive entries appear under History and can be replayed without
changing the remaining queue or adding duplicate History rows. History includes
completed, skipped, and cleared items. Clear archives every waiting item without
changing the current item or playback state. A row's disclosure button expands
its full text.

In desktop mode, mutable state stays in `~/.super-speech/`. The installed model
and engine are read-only application resources. A headless installation keeps
its corresponding runtime inside the installed skill instead.

## Build from source

Building a release requires Node.js 22+ and Python 3.12. End users do not need
either runtime.

```powershell
cd app
npm install
npm run package:win
```

The package command installs pinned build dependencies, verifies or stages the
model files by SHA-256, freezes the engine, builds Electron, and creates the NSIS
installer. See [app/README.md](app/README.md) for focused build and smoke-test
commands. Users who do not want Electron can install the same engine and skill
without the app by following [SETUP.md](SETUP.md).

## Headless installation

Run `py -3.12 skills/super-speech/scripts/install.py --agent codex` from the
repository root. Python 3.11 and 3.13 are also supported. This copies the
complete skill bundle into the Codex skill directory, then creates its Python
environment, verified model assets, queue, and logs under that installed
skill's `runtime/` directory. The first install needs network access. For
Claude, run the same command with `--agent claude`. The desktop build and
headless skill still use the same authoritative engine source.

## Licensing

The Electron app and original Super Speech source are MIT licensed. The
self-contained installer is an aggregate containing a separate frozen engine
with GPL-licensed phonemization and eSpeak NG components, plus Apache-2.0 Kokoro
model assets and other permissively licensed libraries. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before redistribution or any
closed-source commercial edition.
