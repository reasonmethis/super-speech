# Headless setup

This guide installs Super Speech without the Electron app. Start with
[README.md](README.md) if you are still choosing between desktop and headless
installations.

## Requirements

- Python 3.11, 3.12, or 3.13
- Network access during the first installation
- Windows, or Apple Silicon running macOS 14 or newer

Windows is tested. The macOS requirement comes from the pinned ONNX Runtime
package, and the macOS build has not yet been tested on hardware.

## Install the skill

From the repository root, install for Codex on Windows:

```powershell
py -3.12 .\skills\super-speech\scripts\install.py --agent codex
```

On macOS:

```bash
python3 skills/super-speech/scripts/install.py --agent codex
```

Use `--agent claude` for Claude Code. To install into a specific skill folder,
use `--target <skill-directory>`.

## What the installer creates

The installer copies the complete Super Speech skill into the selected agent's
skill directory. Inside that directory it creates:

- `engine/` with the shared speech engine
- `scripts/` with the platform launcher and installer
- `runtime/venv/` with a private Python environment
- `runtime/models/kokoro/` with two model files verified by SHA-256
- `runtime/` storage for the queue, History, status, controls, and logs

The installation does not depend on the repository after it finishes. It does
not put Super Speech runtime files elsewhere on the machine.

## Verify the installation

On Windows, set `$skill` to the installed directory that contains `SKILL.md`:

```powershell
& "$skill\scripts\super-speech.ps1" speak 'Super Speech is set up and working.' --voice af_heart
& "$skill\scripts\super-speech.ps1" status
```

On macOS, set `SKILL` to that directory:

```bash
"$SKILL/scripts/super-speech.sh" speak "Super Speech is set up and working." --voice af_heart
"$SKILL/scripts/super-speech.sh" status
```

Use the launcher for normal commands. Do not set model or runtime environment
variables manually and do not call the private virtual-environment executable
directly.

## Update or repair

Run the installed skill's installer again and point it back at the same skill
folder:

```powershell
py -3.12 "$skill\scripts\install.py" --target "$skill"
```

```bash
python3 "$SKILL/scripts/install.py" --target "$SKILL"
```

Reinstallation stops the headless engine, replaces the files supplied by Super
Speech, updates Python packages, verifies the models, and preserves `runtime/`.

## If the desktop app is installed later

The launcher prefers a valid desktop installation when it finds one. Desktop
and headless modes use the same engine code but keep separate runtime folders.
Existing headless speech does not move into the desktop timeline.

## Troubleshooting

Run `status` through the launcher first. The headless event log is
`<skill>/runtime/log.txt`.

Desktop mode uses `~/.super-speech/log.txt`. It writes the child process's raw
output to `~/.super-speech/engine.log` for the child's entire lifetime.

See [skills/super-speech/SKILL.md](skills/super-speech/SKILL.md) for the exact
playback commands and voice-writing rules.
