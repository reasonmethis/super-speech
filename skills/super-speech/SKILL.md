---
name: super-speech
description: Speak responses aloud as voice replies through the local Kokoro TTS drainer. Use whenever the user asks you to reply by voice/audio, says "use voice", or wants spoken answers turn by turn. Also use to install or set up super-speech — triggers like "install super-speech", "set up super-speech", or "super-speech voice not working" — in which case follow SETUP.md at the repo root. Covers the speak.sh / ensure-drainer.sh helpers (always make sure the drainer is alive before queueing — never just write to the queue), the chunking rules (short first chunk, strictly <1.5× growth, ~600 char cap), per-chunk gaps (gMMM), drainer signal files and their pitfalls, the restart procedure, and the Kokoro voices. This is the BASE voice skill — the auto-podcast skill (long multi-chunk podcasts) and the whatsapp-voice skill (reaching the user when they are away from the computer) both build on it. By default, speak with the `af_heart` voice unless the user explicitly asks for a different one.
---

# Super-Speech

The user listens, you speak. This is the **base voice skill** — it turns your replies into spoken audio on the user's machine, one short answer at a time. Two sibling skills build on it; reach for them when they fit better:

- **A long spoken segment — a "podcast", briefing, or multi-minute explainer** → use the **auto-podcast** skill. Many chunks planned as an arc, optionally fed by background researchers. It depends on this skill for the drainer and the chunking rules.
- **The user is away from the computer entirely** (out of the house, on their phone) → use the **whatsapp-voice** skill. The local drainer plays through the PC speakers — useless if nobody is there — so that skill delivers Kokoro voice notes over WhatsApp instead.

Everything below is **voice-reply mode**: short spoken answers, turn by turn, through the local drainer.

## Install / setup

Before first use, super-speech must be set up: follow **`SETUP.md` at the repo
root**. That agentic playbook locates the scripts, installs the Python deps,
downloads the Kokoro voice model, verifies audio works, and — crucially — writes
the scripts directory to `~/.super-speech/super-speech.paths` as a
`SCRIPTS_DIR=<abs path>` line. The four helper scripts (`speak.sh`,
`ensure-drainer.sh`, `drainer-kokoro.py`, `drainer.py`) live together in that
directory.

**Resolving the scripts directory.** The install path is not known ahead of
time, so do not hardcode it. Read `~/.super-speech/super-speech.paths`, take the
value of the `SCRIPTS_DIR=` line, and use that as `$SCRIPTS_DIR` when calling the
helpers. **If that file is missing, follow `SETUP.md` first** — it sits at the
root of the cloned super-speech repo.

**Runtime home.** The drainer's working files — `queue/`, `spoken/`, `log.txt`,
the signal files — and the downloaded Kokoro model under `models/kokoro/` all
live under one agent-neutral directory: `~/.super-speech/` by default, or
wherever the `SUPER_SPEECH_HOME` environment variable points. Nothing here is
tied to `~/.claude`, so the same drainer and model serve Claude Code, Codex,
OpenCode, or any other agent — only the per-install `SCRIPTS_DIR` differs.

## The drainer and `speak.sh`

The Kokoro drainer is a separate background process that synthesizes queued text chunks and plays them as natural voice — if it has died, raw queue writes just sit there silently. So **queue via `speak.sh`**: it ensures the drainer is running (starting it + waiting out the ~5-7s cold start if not) and auto-numbers the chunk for you.

```bash
bash "$SCRIPTS_DIR/speak.sh" "Your chunk text." bm_fable
```

(`$SCRIPTS_DIR` is the `SCRIPTS_DIR=` value from `~/.super-speech/super-speech.paths` — see Install / setup above.)

For the rarer case where you write queue files directly, run `bash "$SCRIPTS_DIR/ensure-drainer.sh"` once first (prints `running`/`started`) — same drainer check, without queueing anything.

## Chunking rules

These four rules are the **canonical chunking contract** — the auto-podcast and whatsapp-voice skills refer back here rather than restating them.

1. **First chunk is short.** Aim for one short sentence, ~90-110 chars. Kokoro synth runs at ~0.4× realtime, so a 100-char chunk gives first audio in ~2.5 seconds. Long first chunks blow up time-to-first-audio (a 700-char first chunk takes ~14s to synthesize before any sound is heard).

2. **Growth ratio strictly less than 1.5×.** No chunk may have a char count >= 1.5× its predecessor. Count chars before queueing. 1.46× is fine; 1.56× is not. If you're over, split the larger chunk.

3. **Cap chunks at ~600 characters** (roughly 3-4 sentences). Above that, you lose the ability to control rhetorical pauses inside a thought and the speech starts sounding monotone. Plateau under 600 is fine.

4. **Shrinking is fine.** A chunk can be smaller than its predecessor with no penalty.

**Why <1.5× is the rule:** the drainer pre-synthesizes chunk N+1 *during* chunk N's playback. Synth at 0.4× realtime means synth time = 0.4 × audio duration. The hard ceiling for no gap is 2× — there synth(N+1) = 0.8 × duration(N), only just finishing in time. Keeping growth under 1.5× means synth(N+1) < 0.6 × duration(N), so synthesis finishes well before chunk N ends, with margin to spare. At 2× and above you risk falling behind and an audible gap. (Char count is a good proxy for audio duration; both scale at ~17 chars/second of speech for am_echo.)

## Voice-reply workflow

1. **Decide chunk count.**
   - 1 chunk for one-sentence replies
   - 2-4 chunks for moderate replies, following the growth rule
   - More only if the reply is genuinely long enough to warrant it

2. **Queue ALL chunks of a reply in ONE Bash call.** Chain `speak.sh` invocations with `&&` in a single Bash tool call — *not* one Bash call per chunk. Each Bash tool call in Claude Code has ~15 s of round-trip latency; one chunk per call drips chunks into the queue slower than the drainer plays them, so chunk N+1 isn't in `queue/` yet when N starts playing, synth-ahead never engages, and the drainer goes idle between every chunk. Symptom: a `tone-to-audio gap: X.XXs` line in `~/.claude/podcast/log.txt` after every single chunk. Batching all chunks in one call fixes it (gap line only on the first chunk, cold-start).
   ```bash
   bash "$SCRIPTS_DIR/speak.sh" "First short chunk." bm_fable && \
   bash "$SCRIPTS_DIR/speak.sh" "Second, somewhat longer chunk, following the growth rule." bm_fable 600 && \
   bash "$SCRIPTS_DIR/speak.sh" "Third chunk." bm_fable 800
   ```
   `speak.sh` ensures the drainer is running first and auto-picks the next chunk number, so you don't manage numbers and the drainer can't be silently dead.
   - Args: `speak.sh "<text>" [voice] [gap_ms]`. `voice` defaults to `bm_fable` (male) — use `af_aoede` when picking a female voice, `bm_fable` when picking a male voice. Override with any other voice ID when the user asks for one specifically. `gap_ms` is the optional pre-play gap (`g500`-style; omit for the default). Pass the digits only — `speak.sh` prepends the `g` itself; passing `g600` yields filename `gg600` which fails to parse and falls back to the default 1.0 s gap.
   - **PowerShell / Codex on Windows:** avoid nested `bash -lc` quoting for `speak.sh`; it can split or truncate the text so only the first word is spoken. Call Git Bash directly with `speak.sh` as the script and pass the text as a PowerShell argument:
     ```powershell
     $text = 'Full sentence to speak.'
     & 'C:\Program Files\Git\bin\bash.exe' 'C:/Users/micro/Documents/Projects/super-speech/speak.sh' $text 'bm_fable'
     ```
   - Write the chunk text for the ear (see TTS pronunciation gotchas) and obey the chunking rules (first chunk ~90-110 chars; each chunk < 1.5× its predecessor; cap ~600).
   - Apply per-chunk gaps via the `gap_ms` arg, same meanings as the `gMMM` filename token below.

3. **Minimal text in the visible reply.** A one-line acknowledgement is enough — the audio is the answer.

(If you ever bypass `speak.sh` and write queue files directly: still run `ensure-drainer.sh` first, and follow the filename convention `NNN-voice-gMMM-slug.txt` with a 3+-digit `NNN` continuing from the highest in `queue/`+`spoken/`. But `speak.sh` is the path — use it.)

## TTS pronunciation gotchas

Kokoro reads literally. Spell things out in the chunk text:

- Method paths: `tools-slash-call`, not `tools/call`
- URLs: `file colon slash slash`, not `file://`
- Math: `equals`, `plus`, not `=`, `+`
- Avoid emoji and special chars like →, ←, ✓ — Windows cp1252 can also choke on those if not encoded as UTF-8
- Punctuation matters for prosody: commas, periods, em dashes all shape pacing

## Per-chunk gap

The pause before each chunk plays can be specified in the filename:

`NNN-voice-gMMM-slug.txt`

where `gMMM` is the gap in milliseconds before this chunk's audio starts. Examples:

- `265-am_echo-g500-r1.txt` → 0.5s gap
- `266-am_echo-g2000-r2.txt` → 2.0s gap
- `267-am_echo-g0-r3.txt` → no gap (tight transition)
- `268-am_echo-r4.txt` → no gMMM token → uses default `CHUNK_GAP_S` (currently 1.0s)

**Choosing values:**

- `g0`: no gap at all — chunks flow as if one stream
- `g500` to `g1200`: **normal pauses** (typical conversational pacing — pick within this range for most transitions)
- `g1500`: **maximum** — reserved for significant rhetorical pauses (end of one section, start of another)
- **Do not exceed `g1500`.** Anything longer feels like dead air.

Use varied gaps within the normal range to give speech a natural rhythm. Sitting at the default for everything makes it monotone, but reaching for `g1500` too often dilutes its impact.

### Filename parsing rules

- Field 0 (NNN): sequence number
- Field 1 (voice): Kokoro voice ID. Parsed as a voice only if it starts with `a`/`b` and contains `_`.
- Field 2 (optional gMMM): pre-play gap in milliseconds. Format: literal `g` then digits. If field 2 doesn't match this pattern, it's treated as part of the slug and default gap applies.
- Remaining: free-form slug for human-readable hint.

## Drainer signals

Signal files under `~/.super-speech/` (create via `touch`):

| Signal | Effect |
|---|---|
| `STOP` | Finish current chunk, then exit cleanly |
| `INTERRUPT` | Cut playback immediately, exit |
| `SKIP` | Stop current chunk, archive it, continue with next |
| `CLEAR` | Drop all non-playing queued chunks |

### Critical pitfall: stale signal files

Signal files persist on disk until a running drainer consumes them. If you `touch INTERRUPT` to exit the drainer, **do NOT also `touch CLEAR`** — the drainer exits on INTERRUPT before consuming CLEAR, leaving the CLEAR file behind. The *next* drainer eats CLEAR at startup and silently drops any chunks queued in the meantime.

For a clean restart: `touch INTERRUPT`, wait for "exiting" in the log, start new drainer. That's it. No CLEAR.

If you ever truly need to drop queued chunks before exit, do it BEFORE INTERRUPT and verify the drainer logged `CLEAR (...); dropped N queued chunk(s)` first.

## Restart procedure

**If the drainer is just dead** (not running), don't do this dance — `ensure-drainer.sh` / `speak.sh` start it for you. This procedure is for a **forced restart** of a *running* drainer (e.g. you changed a config knob in `drainer-kokoro.py`, or it's wedged):

```bash
touch "$HOME/.super-speech/INTERRUPT"   # or STOP for graceful
# wait briefly for log to show "exiting"
# then re-run ensure-drainer.sh (it does the detached launch + warmup wait):
bash "$SCRIPTS_DIR/ensure-drainer.sh"
```

Invoke the launch via Bash with `run_in_background: true` and a long timeout (or just run `ensure-drainer.sh` once the old one has logged "exiting" — it does the detached launch + warmup wait for you). Cold start takes ~5-7s (Kokoro model load + warmup). You can queue chunks before the drainer finishes loading; it picks them up as soon as it enters the poll loop.

## Configuration knobs in drainer-kokoro.py

| Constant | Purpose | Current |
|---|---|---|
| `CHUNK_GAP_S` | Default silence between chunks when filename has no `gMMM` segment | 1.0s |
| `PLAY_TONE` | Whether `play_tone()` actually plays its winsound blip; False mutes, code path preserved | `False` |
| `DEFAULT_VOICE` | Voice when filename doesn't parse a valid ID | `af_bella` |
| `POLL_INTERVAL` | Idle poll cadence | 0.2s |
| `SIGNAL_TICK` | Signal check cadence during playback / gap | 0.05s |

Tuning requires a process restart; the model itself does not reload.

## Voice cheat sheet (Kokoro v1.0)

- `af_*` US female: alloy, aoede, bella, heart, jessica, kore, nicole, nova, river, sarah, sky
- `am_*` US male: adam, echo, eric, fenrir, liam, michael, onyx, puck, santa
- `bf_*` UK female: alice, emma, isabella, lily
- `bm_*` UK male: daniel, fable, george, lewis

Defaults for conversational replies:
- **Default**: `af_heart` (use this unless the user asks for another voice)
- **Male**: `bm_fable`
- **Female**: `af_aoede`

Use any other voice only when the user asks for it specifically.

## Bundled scripts

This skill is part of the **super-speech** repo and bundles its own helpers
alongside it:

- `drainer-kokoro.py` — the active Kokoro drainer.
- `drainer.py` — legacy ElevenLabs drainer.
- `ensure-drainer.sh` — idempotent: starts the drainer if it isn't running, waits out the cold start.
- `speak.sh` — `speak.sh "<text>" [voice] [gap_ms]`; calls `ensure-drainer.sh`, then queues the text as the next-numbered chunk.

Their directory is not hardcoded — `SETUP.md` records it to
`~/.super-speech/super-speech.paths` as `SCRIPTS_DIR=<abs path>`. Read that
line and use it as `$SCRIPTS_DIR` (see Install / setup at the top). If the file
is missing, follow `SETUP.md` at the repo root.

**Sibling skills that build on this one:** `auto-podcast` (long multi-chunk
podcasts) and `whatsapp-voice` (voice notes when the user is away from the
computer).
