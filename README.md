# super-speech

Local text-to-speech voice replies for AI coding agents. Your agent speaks its
answers aloud through the **Kokoro** neural TTS engine running entirely on your
machine — no cloud service, no API key, no per-word billing.

Works with any coding agent like Claude Code, Codex, OpenCode, etc.

## Install

Tell your agent:

> Set up super-speech from github.com/reasonmethis/super-speech

`SETUP.md` is an agentic playbook — your agent runs it end to end: locate the
scripts, install the Python dependencies, download the Kokoro voice model,
verify audio works, self-heal any failures, and pick a default voice.

**First-run download:** the Kokoro v1.0 model files total about **338 MB**
(`kokoro-v1.0.onnx` ~311 MB + `voices-v1.0.bin` ~27 MB). They download once
during setup into `~/.super-speech/models/kokoro/` and are reused afterward.

## Usage

Once set up, just ask your agent to reply using super-speech, e.g.

> Use super-speech for your replies until i tell you otherwise

The `super-speech` skill handles chunking, the drainer lifecycle, and
voice selection. See `skills/super-speech/SKILL.md` for the full chunking
contract and TTS details.

## Desktop app

The version-zero desktop controller lives in `app/`. It has a tray icon, a
large Pause/Resume control, current speech and voice details, and a scrollable,
read-only upcoming queue. Pausing stops the active audio without advancing its
sample cursor, and resume continues from the next sample.

The controller is built with Electron and is designed for Windows and macOS.
The existing Python engine remains the single owner of synthesis, queue order,
and audio playback. Electron owns only the window, tray, and setup lifecycle.
For now, install the speech engine through the normal setup above before using
the controller. A fully bundled engine and first-run model download are tracked
in `BACKLOG.md`.

Run it from source:

```powershell
cd app
npm install
npm run dev
```

Build the Windows installer:

```powershell
cd app
npm install
npm run package:win
```

The build requires Node.js 22+. End users of the built installer do not need
Node.js. The public one-click release will also bundle the Python engine; that
packaging work is tracked in the backlog.

See `ARCHITECTURE.md` for the framework and process-boundary decision.

## Platform support

`drainer-kokoro.py` (the TTS engine) and `speak.sh` are cross-platform.
`ensure-drainer.sh` — the drainer launcher — is written for Windows + git-bash;
on macOS/Linux the setup playbook tells the agent how to substitute the short
POSIX equivalents. The runtime home `~/.super-speech/` is the same on every OS.

## License

MIT — see `LICENSE`.
