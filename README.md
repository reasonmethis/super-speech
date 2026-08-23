# Super Speech

Super Speech gives AI coding agents private local voice replies. Kokoro speech
synthesis runs entirely on the user's computer with no cloud service, API key,
or per-word billing.

This README is the canonical project entry point. The narrower documents cover
[desktop architecture](ARCHITECTURE.md), [app development](app/README.md), the
[product backlog](BACKLOG.md), and [legacy source setup](SETUP.md).

## Install

On Windows, run `Super-Speech-Win-x64-0.1.0-Setup.exe`. The installer includes:

- the Electron tray app and status window
- a frozen Python speech engine
- the Kokoro v1.0 model and all bundled voices
- ONNX Runtime, eSpeak NG, phonemization, and audio libraries
- compact Super Speech skills for existing Codex and Claude installations

The installed app does not need Python, Node.js, Git Bash, Rust, this repository,
or a model download. First launch starts the engine and writes its local paths to
`~/.super-speech/install.json`. It installs the agent skill only when the
corresponding `.codex` or `.claude` directory already exists, and it never
overwrites an existing Super Speech skill.

The Windows x64 package is verified. Electron and the process boundary are
designed for macOS, but macOS sidecars must be built on each target architecture
and have not yet been tested on Apple Silicon hardware.

## Use

Ask your agent:

> Use super-speech for your replies until I tell you otherwise

The agent launches Super Speech quietly and queues each spoken chunk through the
installed engine. The desktop window and tray menu can pause immediately at the
current audio sample and resume from that exact point. The window also shows the
current voice, current text, and a scrollable upcoming queue.

Mutable state stays in `~/.super-speech/`. The installed model and engine are
read-only application resources.

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
commands. Developers who intentionally want the older repository-driven Python
workflow can use [SETUP.md](SETUP.md).

## Licensing

The Electron app and original Super Speech source are MIT licensed. The
self-contained installer is an aggregate containing a separate frozen engine
with GPL-licensed phonemization and eSpeak NG components, plus Apache-2.0 Kokoro
model assets and other permissively licensed libraries. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before redistribution or any
closed-source commercial edition.
