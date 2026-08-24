# Super Speech setup

Super Speech has one engine with two installation sizes. The desktop installer includes Electron, the engine, models, and agent skill. The headless installer includes only the Python engine, models, and skill. Both expose the same `super-speech-engine` command and use `~/.super-speech/` for runtime state.

## Desktop installation

On Windows, run the installer described in [README.md](README.md). It does not require Python, Node.js, Git Bash, or a separate model download. First launch starts the engine and installs the Super Speech skill for existing Codex and Claude installations.

## Minimal headless installation

Use this when the user wants spoken agent replies without the desktop app. Python 3.11, 3.12, or 3.13 is required. Windows is verified. macOS headless installation currently requires Apple Silicon and macOS 14 or newer because of the pinned ONNX Runtime wheel.

From the repository root, run:

```powershell
py -3 .\install_headless.py
```

On macOS:

```bash
python3 install_headless.py
```

The installer:

- creates a private virtual environment at `~/.super-speech/engine/`
- installs `super_speech_engine.py` and its pinned runtime dependencies
- downloads both Kokoro model files and verifies their SHA-256 hashes
- copies the canonical Super Speech skill into existing Codex and Claude skill directories

It does not write a repository path file or require the repository after installation.

## Verify end to end

On Windows:

```powershell
$runtime = if ($env:SUPER_SPEECH_HOME) { $env:SUPER_SPEECH_HOME } else { Join-Path $env:USERPROFILE '.super-speech' }
$engine = Join-Path $runtime 'engine\Scripts\super-speech-engine.exe'
& $engine speak 'Super Speech is set up and working.' --voice af_heart
& $engine status
```

On macOS:

```bash
RUNTIME="${SUPER_SPEECH_HOME:-$HOME/.super-speech}"
ENGINE="$RUNTIME/engine/bin/super-speech-engine"
"$ENGINE" speak "Super Speech is set up and working." --voice af_heart
"$ENGINE" status
```

Confirm that the chunk moves from `~/.super-speech/queue/` to `spoken/` and that audio is audible. If playback fails, inspect `~/.super-speech/log.txt`, fix the reported dependency, model, or audio-device error, and retry the same `speak` command.

## Switching between headless and desktop modes

No queue migration or configuration file is required. Both installations use the same queue and engine protocol. Installing the desktop app makes its bundled engine the preferred path in the skill. If the app is removed and its manifest remains, the skill ignores the missing desktop executable and falls back to the headless engine when it exists.

Only one engine process can hold the runtime lock. If an older Super Speech process is still running during an upgrade, stop it before verification, then invoke `speak`; the new CLI starts the installed engine automatically.
