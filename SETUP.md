# Super Speech setup

[README.md](README.md) is the canonical installation and product guide. This
document covers the narrower headless workflow.

Super Speech has one engine implementation with two installation sizes. The desktop installer includes Electron, the engine, models, and agent skill. The headless installer includes only the Python engine, models, and skill. Both expose the same `super-speech-engine` command.

## Desktop installation

On Windows, run the installer described in [README.md](README.md). It does not require Python, Node.js, Git Bash, or a separate model download. First launch starts the engine and installs the Super Speech skill for existing Codex and Claude installations.

## Minimal headless installation

Use this when the user wants spoken agent replies without the desktop app. Python 3.11, 3.12, or 3.13 and network access for the initial package and model downloads are required. Windows is verified. macOS headless installation currently requires Apple Silicon and macOS 14 or newer because of the pinned ONNX Runtime wheel.

From the repository root, install the Codex skill:

```powershell
py -3.12 .\skills\super-speech\scripts\install.py --agent codex
```

On macOS:

```bash
python3 skills/super-speech/scripts/install.py --agent codex
```

For Claude Code, use `--agent claude`. An agent that has already copied the
skill folder can pass its absolute skill directory through `--target`. Replace
`3.12` in the Windows command if Python 3.11 or 3.13 is the installed version.

The installer:

- copies the complete skill bundle into the selected agent's skill directory
- creates a private virtual environment at `runtime/venv/` inside that skill
- installs `engine/super_speech_engine.py` and its pinned runtime dependencies
- downloads both Kokoro model files into `runtime/models/` and verifies their SHA-256 hashes

The queue, archive, status, controls, and logs are also created under
that `runtime/` directory when the engine runs. The installation does not
retain a repository path or write Super Speech files elsewhere.

Re-running the bundled installer stops the local headless engine, updates the
immutable skill files and Python packages, and preserves `runtime/`. Replacing
the whole skill directory with another installer will also replace that local
runtime unless the installer preserves it.

## Verify end to end

On Windows:

```powershell
$agentHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$skill = Join-Path $agentHome 'skills\super-speech'
$env:SUPER_SPEECH_HOME = Join-Path $skill 'runtime'
$env:SUPER_SPEECH_MODEL_DIR = Join-Path $env:SUPER_SPEECH_HOME 'models\kokoro'
$engine = Join-Path $skill 'runtime\venv\Scripts\super-speech-engine.exe'
& $engine speak 'Super Speech is set up and working.' --voice af_heart
& $engine status
```

On macOS:

```bash
SKILL="${CODEX_HOME:-$HOME/.codex}/skills/super-speech"
export SUPER_SPEECH_HOME="$SKILL/runtime"
export SUPER_SPEECH_MODEL_DIR="$SKILL/runtime/models/kokoro"
ENGINE="$SKILL/runtime/venv/bin/super-speech-engine"
"$ENGINE" speak "Super Speech is set up and working." --voice af_heart
"$ENGINE" status
```

Confirm that the speech item moves from the skill's `runtime/queue/` to
`runtime/spoken/` and that audio is audible. If playback fails, inspect
`runtime/log.txt`, fix the reported dependency, model, or audio-device error,
and retry the same `speak` command.

## Switching between headless and desktop modes

The skill prefers a valid desktop engine manifest when the app is installed.
Otherwise it uses the engine inside its own `runtime/venv/`. The two modes use
the same commands and engine implementation, but keep separate runtime state.
Queued headless items do not migrate into the desktop app.

Each runtime allows one engine process to hold its lock. If an older process is
still running during an upgrade, stop it before verification, then invoke
`speak`; the new CLI starts the installed engine automatically.

## Select or replay a speech item

Run `super-speech-engine status` and use an exact `id` from `current`, `queue`,
or the bounded `history` list:

```powershell
& $engine play '014-af_heart-say'
& $engine play '014-af_heart-say' --voice bm_fable
```

The same command works in desktop and headless installations. Selecting an
upcoming item jumps to that point: the current item and every older waiting item
before the selection move to History, while newer waiting items keep their order.
Selecting a history entry queues a working copy under the same ID without
changing the remaining queue, the original archive, or the number of History
rows. Selection is distinct from pause and resume: pause preserves the exact
audio sample, while selecting another item ends that paused position. The
command waits for engine acknowledgement and prints the resulting ID as JSON.

History is an archive, not a completion log. It can contain items that
finished, were skipped, or were cleared before playback.

Use exact IDs to reorder Waiting or recent History items, archive Waiting, or
delete History:

```powershell
& $engine move '016-af_bella-say' '015-bm_fable-say'
& $engine move '015-bm_fable-say'
& $engine move-history '014-af_heart-say' '013-bm_george-say'
& $engine archive '016-af_bella-say'
& $engine delete '014-af_heart-say'
```

The first command inserts one item before another. Omitting the second ID moves
the item to the end. `archive` moves only that waiting item into History. These
commands never rename IDs or change current playback. `move-history` persists
manual ordering within the recent History view. `delete` permanently
removes one exact History ID without changing the waiting queue.
