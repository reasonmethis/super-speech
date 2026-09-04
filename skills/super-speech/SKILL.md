---
name: super-speech
description: Speak concise replies aloud with the local Super Speech engine. Use whenever the user asks for voice or audio replies, asks for Super Speech, names a Kokoro voice, or wants to install, configure, or troubleshoot Super Speech. Works with either the desktop app or the minimal headless engine. Default to af_heart unless the user asks for another voice.
metadata:
  managed_by: super-speech
  integration_version: 5
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

When several agent tasks share one engine, add a short source label:

```powershell
& "$skill\scripts\super-speech.ps1" speak 'Your spoken reply.' --voice af_heart --source 'Agent: Project task'
```

Choose the label on the first spoken reply from a task, then reuse it exactly
for later replies from that task. The app shows it beside the voice. Keep it at
80 characters or fewer, and do not include line breaks.

## Listen for replies from the app

Attaching an inbox is a promise that a new Reply will reach this same task and
cause it to run, including after the current turn ends. Before passing
`--inbox`, connect the inbox to the current agent host's supported way to send a
message to an existing task. This may be a task messaging API, an event hook, a
background watcher, or another host feature that can wake an idle task. A
detached listener whose output cannot reach the task does not meet this
contract. If the host cannot wake this task, do not pass `--inbox`.

Choose one private, unique absolute `.jsonl` path for the task and reuse it on
every spoken reply. Never share one inbox between tasks.

Start the portable listener before the first `speak` call. It creates the file,
prints one complete JSON message per stdout line, and keeps following new
messages. Connect that stream to the host's task messaging mechanism and wait
until stderr says the listener is ready:

```powershell
& "$skill\scripts\super-speech.ps1" listen-inbox $inbox
```

```bash
"$SKILL/scripts/super-speech.sh" listen-inbox "$INBOX"
```

The bridge between the listener and the agent host must:

- survive after the current turn ends
- keep the stable task or session identifier needed to address this same task
- send the message text as new user input through the host's supported task API
- persist handled message IDs so a restart does not repeat replies
- retry temporary host or transport failures without losing the message

Use the host's existing task owner. Do not start a competing agent process that
tries to open the same task state. Before claiming the inbox works, let the task
become idle and test that a Reply wakes it. Seeing a new line in the file is not
enough.

If the current host already has a packaged adapter in this skill, use it instead
of rebuilding the bridge. For Codex desktop on Windows, read
[references/codex-desktop-inbox.md](references/codex-desktop-inbox.md).

Then attach the same path to each Speechicle:

```powershell
& "$skill\scripts\super-speech.ps1" speak 'Your spoken reply.' --voice af_heart --source 'Agent: Project task' --inbox $inbox
```

```bash
"$SKILL/scripts/super-speech.sh" speak "Your spoken reply." --voice af_heart --source "Agent: Project task" --inbox "$INBOX"
```

Each stdout line from the listener is one complete JSON message:

```json
{"version":1,"kind":"user_message","id":"07ca7adc-12f2-4b7b-9e9e-48739da4194b","sent_at":"2026-08-31T12:00:00.000Z","speechicle_id":"sp_0123456789abcdef0123456789abcdef","source":"Agent: Project task","text":"Please check the retry path."}
```

Accept lines only when they have protocol `version` 1, kind `user_message`, a
new `id`, and string `text`. Treat that text as a user message for this task.
Use the ID to avoid handling a message twice after restarting a listener. By
default the listener first emits saved messages and then follows new ones. Add
`--from-end` only when the user has clearly chosen to ignore messages already
in the file.

Keep the bridge armed while the task still offers replies. The file preserves
messages, but it is storage, not a wake-up mechanism. Do not claim the inbox is
live after the listener or bridge has stopped.

The launcher uses the desktop engine when a valid app installation exists.
Otherwise it uses the headless engine inside this skill. Do not parse the
desktop manifest, locate an engine another way, or write runtime files directly.

Kokoro reads text literally. Rewrite symbols and awkward tokens for speech when
needed. For example, say "equals" instead of `=`. Keep the exact token in the
visible text.

## Voices

Use `af_heart` unless the user asks for another voice. Useful alternatives:

- US female: `af_aoede`, `af_bella`, `af_kore`, `af_nova`
- US male: `am_echo`
- UK female: `bf_emma`, `bf_lily`
- UK male: `bm_fable`, `bm_george`

## Playback commands

Run commands through the same platform launcher used for `speak`:

| Command | Effect |
|---|---|
| `status` | Print the engine's current timeline as JSON |
| `listen-inbox <file> [--from-end]` | Print complete replies saved by the app and follow new ones |
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

Treat IDs as random strings that must be copied exactly. Copy an ID from
`status`; never build one from a path or filename. Passing `--voice` to `play`
keeps the same text and timeline position.

In status JSON, `current` is the Speechicle at the playback boundary. It may be
playing, paused, being prepared, or stopped. `queue` contains only the
Speechicles waiting after it. Together they are the Queue described in the app
and documentation. A Speechicle created with `--source` or `--inbox` also has
the matching optional metadata field.

## Install or repair headless mode

The desktop package includes the engine, model, voices, and this skill. When the
app starts, it installs a missing skill or updates an unchanged app-managed
copy in each existing Codex or Claude directory. It preserves a locally edited
skill. Desktop mode does not require system Python.

For headless mode, run the bundled installer with Python 3.11, 3.12, or 3.13:

For a new Codex installation, run:

```powershell
py -3.12 "$skill\scripts\install.py" --agent codex
```

```bash
python3 "$SKILL/scripts/install.py" --agent codex
```

Use `--agent claude` for Claude Code. To repair the private environment or
models using the engine source already installed, point the installer back at
this exact directory:

```powershell
py -3.12 "$skill\scripts\install.py" --target "$skill"
```

```bash
python3 "$SKILL/scripts/install.py" --target "$SKILL"
```

This same-directory repair does not fetch newer skill code. To update the skill
itself, run `install.py` from a newer repository checkout or release copy and
use `--target` with this installed skill directory.

The headless installer keeps its private environment, models, queue, status,
and logs under this skill's `runtime/` directory.

For troubleshooting, run `status` through the launcher. Headless mode logs to
`<skill>/runtime/log.txt`. Desktop mode logs events to
`~/.super-speech/log.txt` and raw child output to
`~/.super-speech/engine.log`.
