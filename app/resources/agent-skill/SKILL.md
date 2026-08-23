---
name: super-speech
description: Speak concise replies aloud with the installed Super Speech desktop app
---

# Super Speech

Use this skill when the user asks for spoken replies, Super Speech, voice
responses, or a named Kokoro voice. Super Speech is installed locally and owns
its engine, models, queue, and playback UI.

## Queue speech

Keep the visible answer and spoken answer substantively identical. The visible
answer may add exact paths, code, links, or symbols that speech handles poorly.

Prefer one to four chunks. Start with a short sentence and keep each chunk under
600 characters. The default voice is `af_heart`. Useful alternatives are
`am_echo`, `bm_fable`, and `af_aoede`. Optional gaps are 0 to 1500 milliseconds.

On Windows, launch the app quietly and call its bundled engine:

```powershell
$install = Get-Content -Raw "$env:USERPROFILE\.super-speech\install.json" | ConvertFrom-Json
Start-Process -FilePath $install.app_path -ArgumentList '--hidden' -WindowStyle Hidden
& $install.engine_path --enqueue 'Text to speak.' --voice af_heart --gap-ms 500
```

On macOS, launch the app quietly and read the bundled engine path with `plutil`:

```bash
INSTALL="$HOME/.super-speech/install.json"
ENGINE="$(plutil -extract engine_path raw "$INSTALL")"
open -gj -a "Super Speech" --args --hidden
"$ENGINE" --enqueue "Text to speak." --voice af_heart --gap-ms 500
```

Omit `--gap-ms` to use the natural default. Queue chunks in speaking order. The
engine reserves queue numbers atomically, so callers do not manage filenames.

If `install.json` is missing, ask the user to open Super Speech once. Do not
fall back to repository scripts or a system Python installation.

## Playback controls

The desktop app and tray menu pause immediately at the current audio sample and
resume from that exact point. The queue remains visible while paused.
