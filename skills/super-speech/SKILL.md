---
name: super-speech
description: Speak concise replies aloud with the local Super Speech engine. Use whenever the user asks for voice or audio replies, asks for Super Speech, names a Kokoro voice, or wants to install, configure, or troubleshoot Super Speech. Works with either the desktop app or the minimal headless engine. Default to af_heart unless the user asks for another voice.
metadata:
  managed_by: super-speech
  integration_version: 2
---

# Super Speech

Use Super Speech for short, turn-by-turn voice replies. The user must be able to
understand the answer in one listen.

## Write for the ear

- Answer the question first and stop when it is answered
- Prefer one clear idea over a comprehensive briefing
- Use short sentences and familiar words
- Explain one useful cause or distinction when the conclusion needs it
- Omit file lists, command names, test counts, and implementation details unless
  the user asked for them or they change the answer
- If a detailed answer is unavoidable, ask whether the user wants it spoken in
  parts or written
- If the user asks for a brief answer, speak one answer sentence and stop

Do not use unexplained local shorthand. Describe the concrete thing first. Use
an internal name only when it helps the rest of the explanation.

## Keep speech and visible text aligned

Every conclusion, recommendation, question, commitment, correction, and caveat
that changes the answer must appear in both speech and visible text.

Visible text may add only details that are easier to read than hear, such as
exact numbers, identifiers, file paths, links, code, test counts, and supporting
evidence that does not change the answer. It must not hide a different decision
or a new action from a listener who is away from the screen.

Write the spoken answer first. Then write the visible reply as the same answer
rendered for the eye.

## Speak one reply

One agent reply should normally be one Speechicle. Pass the whole concise spoken
answer to one `speak` command. Do not split it into separate queue items. The
engine splits it into sentence-sized pieces for low-latency synthesis while the
app keeps it as one row and one replay target.

On Windows, set `$skill` to the absolute directory containing this `SKILL.md`:

```powershell
& "$skill\scripts\super-speech.ps1" speak 'Your spoken reply.' --voice af_heart
```

On macOS, set `SKILL` to the absolute directory containing this `SKILL.md`:

```bash
"$SKILL/scripts/super-speech.sh" speak "Your spoken reply." --voice af_heart
```

The launcher uses the desktop engine when a valid app installation exists.
Otherwise it uses the headless engine inside this skill. Do not parse the
desktop manifest, locate an engine another way, or write runtime files directly.

Kokoro reads text literally. Rewrite symbols and awkward tokens for speech when
needed. For example, say "equals" instead of `=`. Keep the exact token in the
visible text.

## Voices

Use `af_heart` unless the user asks for another voice. Useful alternatives:

- US female: `af_aoede`, `af_bella`, `af_kore`, `af_nova`
- US male: `am_echo`, `am_fenrir`, `am_puck`
- UK female: `bf_emma`, `bf_lily`
- UK male: `bm_fable`, `bm_george`

The full Kokoro voice list is available in the desktop voice picker.

## Playback commands

Run commands through the same platform launcher used for `speak`:

| Command | Effect |
|---|---|
| `status` | Print the authoritative timeline as JSON |
| `pause` | Pause at the current audio sample |
| `resume` | Continue from the same sample |
| `play <id> [--voice VOICE]` | Play one exact Speechicle, optionally with another voice |
| `move <id> [before-id]` | Reorder a Waiting Speechicle |
| `move-history <id> [before-id]` | Reorder a recent History Speechicle |
| `archive <id>` | Move one Waiting Speechicle into History |
| `delete <id>` | Permanently remove one History Speechicle |
| `skip` | Archive Current and continue |
| `clear` | Stop and archive Current plus Waiting |
| `stop` | Finish Current and stop the engine |
| `interrupt` | Stop playback and the engine immediately |

Treat IDs as opaque. Copy an exact ID from `status`; never derive one from a
path or filename. Passing `--voice` to `play` keeps the same text and timeline
position.

## Install or repair headless mode

The desktop installer already includes the engine, model, voices, and this
skill. It does not require system Python.

For headless mode, run the bundled installer with Python 3.11, 3.12, or 3.13:

```powershell
py -3.12 "$skill\scripts\install.py" --agent codex
```

```bash
python3 "$SKILL/scripts/install.py" --agent codex
```

Use `--agent claude` for Claude Code or `--target <skill-directory>` for a
custom destination. The headless installer keeps its private environment,
models, queue, status, and logs under this skill's `runtime/` directory.

For troubleshooting, run `status` through the launcher. The engine event log is
`runtime/log.txt`. Desktop mode also has `engine.log`, which captures the child
process's raw output when it exits before writing a normal event.
