# super-speech — Setup Playbook

You are an AI coding agent reading `SETUP.md` for **super-speech**: local Kokoro
text-to-speech voice replies. The user cloned this repo and asked you to set it
up. Execute the phases below — actually run the commands, read the output, and
fix problems yourself. Do not just describe the steps to the user.

super-speech speaks an agent's answers aloud through a neural TTS engine running
entirely on the user's machine — no cloud, no API key.

This repo bundles four scripts at its **root**: `speak.sh`, `ensure-drainer.sh`,
`drainer-kokoro.py`, `drainer.py`. A core job of this playbook is to find that
directory and record it for the skill to use later.

## Platform note — read this first

Only one script is OS-specific:

- `drainer-kokoro.py` — the TTS engine — is **cross-platform** Python.
- `speak.sh` is **portable** bash; it delegates the drainer-liveness check to
  `ensure-drainer.sh`, so it works on any OS once that one script does.
- `ensure-drainer.sh` is written for **Windows + git-bash** (`powershell`,
  `Start-Process`, `Get-CimInstance`, `pwd -W`). On **macOS/Linux** it will not
  work — port it (it is short) so it does its two jobs with POSIX tools:
  - **Detect:** `pgrep -f drainer-kokoro` — non-empty output means the drainer
    is already running.
  - **Launch detached:** `nohup "<python>" "<scripts-dir>/drainer-kokoro.py" >/dev/null 2>&1 &`
    then wait ~6 s for the Kokoro cold start before queueing chunks.

  Wherever a later phase says to run `ensure-drainer.sh`, do the above instead.

The runtime home is `~/.super-speech/` on every OS (override it with the
`SUPER_SPEECH_HOME` environment variable).

## Phase 0 — Locate and record the scripts directory

The four scripts live together in one directory. Find its absolute path:

It is the repo root the user pointed you at — the directory that contains
`speak.sh` and `ensure-drainer.sh`. There is no install step; you already have
the files.

**Record it.** Create `~/.super-speech/` if needed and write the absolute path
into `~/.super-speech/super-speech.paths` as a single line:

```
SCRIPTS_DIR=<absolute path to the directory containing speak.sh>
```

Preserve any other lines already in that file (e.g. `PYTHON=`, `DEFAULT_VOICE=`).

## Phase 1 — Bootstrap (do this automatically, no prompts)

1. **Find a Python 3 interpreter.** Prefer Python 3.12. Try `py -3.12`,
   `python3.12`, `python3`, `python` (and on Windows the typical
   `...\Programs\Python\Python312\python.exe`). Pick the first that reports a
   Python 3 version and remember its absolute path. Record it in
   `~/.super-speech/super-speech.paths` as a `PYTHON=<absolute path>` line
   (preserving any existing `SCRIPTS_DIR=` line) — `ensure-drainer.sh` (and the
   macOS/Linux launcher) use exactly this interpreter rather than guessing one.

2. **Install the Python dependencies** with that interpreter:
   ```
   <python> -m pip install kokoro-onnx numpy soundfile sounddevice
   ```
   If `pip` is missing, bootstrap it (`<python> -m ensurepip --upgrade`) and retry.

3. **Ensure the Kokoro v1.0 model files exist** under
   `~/.super-speech/models/kokoro/`:
   - `kokoro-v1.0.onnx` (~311 MB)
   - `voices-v1.0.bin` (~27 MB)

   Create the directory if needed. If either file is missing (or clearly
   truncated — much smaller than the sizes above), download it. The files are
   published on the `thewh1teagle/kokoro-onnx` GitHub releases. Try first:
   - `https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx`
   - `https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin`

   If either URL returns 404, **web-search** for the current `kokoro-onnx` v1.0
   model and voices download URLs and use those. Verify the downloaded file
   sizes look right before continuing.

## Phase 2 — Verify (you do this, end to end)

1. Start the drainer — `bash "<SCRIPTS_DIR>/ensure-drainer.sh"` on Windows, or
   the POSIX launch from the Platform note on macOS/Linux. Expect it to report
   `running` / `started`.
2. Queue a short test chunk:
   `bash "<SCRIPTS_DIR>/speak.sh" "Super speech is now set up and working." bm_fable`
3. Watch `~/.super-speech/log.txt`: look for a `play` line for that chunk and
   confirm there are no errors/tracebacks around it.
4. Ask the user to confirm they **actually heard the audio**. If they did not,
   treat it as a failure and go to Phase 3.

## Phase 3 — Self-heal (loop until verification passes)

If any step above failed — a missing/incompatible Python, a pip install error, a
404 or corrupt download, a path or permission problem, no audio, a drainer
traceback — do not give up. Read the actual error message and the tail of
`~/.super-speech/log.txt`, diagnose the root cause, and fix it:

- Scripts not found → re-check Phase 0, correct `SCRIPTS_DIR=`.
- Dependency error → reinstall the dep, or try a different Python interpreter.
- Download 404 / corrupt file → retry, then web-search for an alternate URL.
- Wrong/missing path in `super-speech.paths` → correct it.
- Permission error → adjust the target directory or path.
- Drainer not detected / no audio → check the log; on macOS/Linux confirm your
  POSIX launcher actually started a process that survives (a common bug is
  passing the script path in a form the interpreter cannot open).

After each fix, re-run Phase 2. Repeat until verification passes cleanly.

## Phase 4 — Optional voice picker

Once verification passes, offer to pick a default voice. Synthesize and play one
short sample line in each of these six recommended voices, one at a time:
`af_aoede`, `af_bella`, `af_heart`, `am_echo`, `bm_fable`, `bm_george`. For example:
```
bash "<SCRIPTS_DIR>/speak.sh" "This is the bm_fable voice." bm_fable
```
Ask the user which they prefer, then save their choice to
`~/.super-speech/super-speech.paths` (preserving the `SCRIPTS_DIR=` line):
```
DEFAULT_VOICE=<chosen voice id>
```

## Phase 5 — Install super-speech as a skill

Phases 0-4 set up the runtime, but that alone won't make a *future* session
reach for super-speech unless the user points you at this repo again. So once
verification passes, finish the job: **install super-speech as a skill for
yourself**, using whatever skill mechanism your agent provides — you know your
own. Prefer a user-level (global) install so it works in every project.

The skill is `SKILL.md`, alongside the scripts under `skills/super-speech/`.
**Ask the user which of these two ways to install it, defaulting to the first:**

1. **Copy it in (default).** Copy `SKILL.md` into your agent's skills location —
   the normal install. This is the right choice when super-speech is used with a
   single coding agent.

2. **Link it (keep in sync).** Instead of copying, point your skills location at
   this repo's `skills/super-speech/` via a symlink (a directory *junction* on
   Windows — no admin needed). Choose this *only* when the user runs super-speech
   across **multiple** coding agents and wants one source of truth: an edit to
   the repo's `SKILL.md` then reaches every agent with no re-copying. Trade-off:
   the install now depends on the repo staying in place.

Either way, register it as a skill (its body loads on demand) rather than
pasting it into always-on instructions. Nothing here is agent-specific — the
runtime resolves through `~/.super-speech/super-speech.paths`.

When everything passes, tell the user setup is complete — the runtime works, and
(if Phase 5 ran) the skill is discoverable in new sessions — and that they can
now ask you to reply by voice.
