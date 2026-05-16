# super-speech — Setup Playbook

You are reading `SETUP.md` for the **super-speech** Claude Code plugin. The user
has added this plugin's marketplace and asked you to set it up. Execute the
phases below — actually run the commands, read the output, and fix problems
yourself. Do not just describe the steps to the user.

super-speech is local Kokoro text-to-speech voice replies for Claude Code: Claude
speaks its answers aloud through a neural TTS engine running entirely on the
user's machine — no cloud, no API key.

The plugin bundles four scripts at the **plugin root**: `speak.sh`,
`ensure-drainer.sh`, `drainer-kokoro.py`, `drainer.py`. The plugin's installed
absolute path is not known ahead of time, so a core job of this playbook is to
resolve that path and record it for the skill to use later.

## Phase 0 — Install the plugin

The user only ran `claude plugin marketplace add` — that registered the
marketplace and cloned this repo into `~/.claude/plugins/marketplaces/super-speech/`,
but the **plugin itself is not installed yet**.

1. **Install the plugin:**
   ```
   claude plugin install super-speech@super-speech
   ```

2. **Resolve the plugin's installed scripts directory.** Once installed, the
   plugin's files (including `speak.sh` and `ensure-drainer.sh`) live under the
   plugin cache, typically:
   ```
   ~/.claude/plugins/cache/super-speech/super-speech/<version>/
   ```
   Locate the directory that actually contains `speak.sh` and `ensure-drainer.sh`
   — search under `~/.claude/plugins/cache/` if the exact path differs (the
   `<version>` segment will match `plugin.json`'s `version`). Get its absolute
   path.

3. **Record the scripts directory.** Create `~/.claude/podcast/` if needed and
   write the resolved path into `~/.claude/podcast/super-speech.paths` as a
   single line:
   ```
   SCRIPTS_DIR=<absolute path to the installed plugin directory>
   ```
   If the file already has other lines (e.g. `DEFAULT_VOICE=`), preserve them
   and just update/add the `SCRIPTS_DIR=` line.

## Phase 1 — Bootstrap (do this automatically, no prompts)

1. **Find a Python 3 interpreter.** Prefer Python 3.12. Try `py -3.12`,
   `python3.12`, `python3`, `python` (and on Windows the typical
   `...\Programs\Python\Python312\python.exe`). Pick the first that reports a
   Python 3 version and remember its absolute path.

2. **Install the Python dependencies** with that interpreter:
   ```
   <python> -m pip install kokoro-onnx numpy soundfile
   ```
   If `pip` is missing, bootstrap it (`<python> -m ensurepip --upgrade`) and retry.

3. **Ensure the Kokoro v1.0 model files exist** under `~/.claude/models/kokoro/`:
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

1. Start the drainer: `bash "<SCRIPTS_DIR>/ensure-drainer.sh"` — expect it to
   print `running` or `started`.
2. Queue a short test chunk:
   `bash "<SCRIPTS_DIR>/speak.sh" "Super speech is now set up and working." bm_fable`
3. Watch `~/.claude/podcast/log.txt`: look for a `play` line for that chunk and
   confirm there are no errors/tracebacks around it.
4. Ask the user to confirm they **actually heard the audio**. If they did not,
   treat it as a failure and go to Phase 3.

## Phase 3 — Self-heal (loop until verification passes)

If any step above failed — a missing/incompatible Python, a pip install error, a
404 or corrupt download, a path or permission problem, no audio, a drainer
traceback — do not give up. Read the actual error message and the tail of
`~/.claude/podcast/log.txt`, diagnose the root cause, and fix it:

- Plugin not installed / scripts not found → re-run `claude plugin install`,
  re-resolve the scripts directory, correct `SCRIPTS_DIR=`.
- Dependency error → reinstall the dep, or try a different Python interpreter.
- Download 404 / corrupt file → retry, then web-search for an alternate URL.
- Wrong/missing path in `super-speech.paths` → correct it.
- Permission error → adjust the target directory or path.
- Drainer not detected / no audio → check the log, restart the drainer.

After each fix, re-run Phase 2. Repeat until verification passes cleanly.

## Phase 4 — Optional voice picker

Once verification passes, offer to pick a default voice. Synthesize and play one
short sample line in each of these five recommended voices, one at a time:
`bm_fable`, `af_aoede`, `af_bella`, `am_michael`, `bm_george`. For example:
```
bash "<SCRIPTS_DIR>/speak.sh" "This is the bm_fable voice." bm_fable
```
Ask the user which they prefer, then save their choice to
`~/.claude/podcast/super-speech.paths` (preserving the `SCRIPTS_DIR=` line):
```
DEFAULT_VOICE=<chosen voice id>
```

When everything passes, tell the user setup is complete and that they can now
ask you to reply by voice.
