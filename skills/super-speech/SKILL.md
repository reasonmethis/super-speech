---
name: super-speech
description: Speak concise replies aloud with the local Super Speech engine. Use whenever the user asks for voice or audio replies, asks for Super Speech, names a Kokoro voice, or wants to install, configure, or troubleshoot Super Speech. Works with either the desktop app or the minimal headless engine, using the same CLI and queue semantics in both modes. Default to `af_heart` unless the user asks for another voice.
metadata:
  managed_by: super-speech
  integration_version: 1
---

# Super-Speech

The user listens, you speak. This is the **base voice skill** — it turns your replies into spoken audio on the user's machine, one short answer at a time. Two sibling skills build on it; reach for them when they fit better:

- **A long spoken segment — a "podcast", briefing, or multi-minute explainer** → use the **auto-podcast** skill. Many chunks planned as an arc, optionally fed by background researchers. It depends on this skill for the engine and the chunking rules.
- **The user is away from the computer entirely** (out of the house, on their phone) → use the **whatsapp-voice** skill. The local engine plays through the PC speakers — useless if nobody is there — so that skill delivers Kokoro voice notes over WhatsApp instead.

Everything below is **voice-reply mode**: short spoken answers, turn by turn, through the local engine.

## Voice is not writing — keep replies short

The single most important content rule: **do not dump a lot of information into a spoken reply.** A reader can skim, re-read, and take their time; a listener cannot. They absorb a spoken reply in one linear pass, and they will retain far less than you put in — often less than half of a long answer. A wall of voice is wasted: most of it doesn't land.

So optimize for the ear, not the page:

- **Answer the actual question and stop.** Lead with the one main point; deliver it in a few sentences, not a structured briefing.
- **One idea per reply by default.** Resist the urge to be comprehensive. Multi-part findings, lists of caveats, full reasoning chains, and side-notes belong in writing, not voice.
- **Offer to go deeper instead of front-loading.** End with a short "want me to explain X?" rather than pre-emptively explaining X, Y, and Z. Let the user pull more, don't push it all.
- **If something genuinely needs a lot of detail, say so and ask** — e.g. "there's a fair bit here — want it by voice in pieces, or written so you can read it?" Don't just narrate the whole thing.

This is the chunking rules' content counterpart: those keep each *chunk* short for low latency; this keeps the *whole reply* short for comprehension. When in doubt, say less.

**Treat spoken attention as scarce.** Before speaking any detail, ask whether the user needs to hear it now to understand the outcome or make the next decision. If not, omit it from the audio. Do not narrate file lists, command names, test counts, or other evidence merely to sound thorough. Compress routine verification to its useful meaning, such as "the usual checks passed," unless a failure or unusual result changes the answer. Keep exact supporting details in the visible text when they may be useful later.

**A one-sentence answer gets one sentence — even when nobody asked for brevity.** This is where verbose replies actually happen: not on open questions, but on closed ones. If the user asks "does X already cover this?", "am I right that…?", or challenges a point and they're right, the whole reply is the direct answer ("Yes — already covered, dropping it."). Do NOT append the reasoning that led there, the edge cases you considered and rejected, why your earlier suggestion existed, or a restatement of the updated plan. Conceding a point especially needs no justification tour. The text mirror is bounded the same way — a one-sentence spoken answer must not balloon into three written paragraphs.

**No side-trails in voice — the ear can't skim past them (2026-07-19).** A written reply can carry small supporting details harmlessly, because a reader skips what they don't need; a listener has to follow every clause, so each unasked-for detail actively costs them the main point. In voice, answer only what was asked and drop the trivia entirely: implementation mechanics nobody asked about (how a value is imported, which hook read it before), historical asides, secondary caveats, "and also" observations. The test per sentence: did the user ask this, or does it change the answer? If neither, it goes in the text mirror or nowhere. Multi-part questions get one direct answer per part — not one answer per part plus its backstory.

**Never introduce unexplained local shorthand (2026-07-20).** Do not casually name an implementation role or invented category as though the listener already recognizes it, such as "the Assistant Notes reader," "the migration gate," or "the workspace builder." First describe the concrete thing in plain language — for example, "an internal backend function that loads Assistant Notes" — and only then give it a short label if the rest of the explanation genuinely needs one. Prefer the concrete description alone when the label saves little. This applies even when the code has a similarly named function: code-local terminology is not automatically shared vocabulary with the user.

### Give the listener the point before the path

Start with the actual answer, bad result, or decision. A sentence that only describes the first step in a process is not a summary if the listener still does not know why that step matters.

When the answer only applies to a specific group, configuration, or code path, name that scope near the start. Do not make a limited issue sound universal. When two or more conditions must line up, name the full set before explaining any one condition. Say "two things have to be true," then state the first and second conditions. After that, explain how they combine, in the same order. The listener should know where the explanation is going before hearing the walkthrough.

Keep established technical terms when they are the normal shared words. "Cached," "retry," and "transaction" are often clearer than improvised plain-English substitutes. Plain speech means simple sentence structure and familiar wording, not removing precise terms the listener already knows. Keep the timing accurate too; do not speak as though a planned or unreleased change already happened.

Use normal teammate language. Do not sound like a school essay or reach for formal words and balanced sentence shapes just to sound polished. Slightly imperfect conversational wording is usually easier to follow and sounds more intelligent than SAT-style prose.

For a comparison or a visible table, give its central takeaway first, then explain the rows or exceptions. The opening should let the listener place every later detail into a simple mental model.

**Concise means fewer topics, not denser sentences (2026-07-19).** Do not respond to a brevity request by packing the same information into compressed technical phrasing — a listener cannot parse identifiers, formats, and jargon delivered at reading density ("glues each chunk into a data URI, mime type plus base64, before handing it to play"). Cut WHAT you cover, not how gently you cover it: pick the one or two essential ideas, explain those in unhurried plain sentences a listener can absorb in real time, and omit the rest entirely — the user will ask if they want more. Whatever does get explained must actually be understandable by ear on first pass; if it needs symbols or exact names to make sense, it belongs in the text mirror, with the voice carrying only the idea.

**Brevity limits scope, not explanatory depth (2026-07-20).** Once you choose the point that answers the user's question, explain that point sufficiently: include the causal link or distinction the listener needs to understand why the conclusion follows. Do not replace an explanation with a telegraphic verdict, and do not toss a surprising qualification into one unexplained clause merely to stay short. If a qualification materially changes the answer, explain it in plain language; if it does not, omit it as an adjacent detail. The target is a complete explanation of fewer things, not an incomplete explanation of everything.

**Explain mechanisms chronologically and define local shorthand (2026-07-20).** Do not merely name abstractions such as a task, slot, replacement, buffer, backpressure, head, or window. First identify the concrete objects, then walk one example through each state change: what finishes, what waits, what is removed, what triggers the next action, and how order is preserved. If the listener must infer what is replaced, who waits, or when capacity becomes available, the explanation is incomplete. Concision should remove unrelated topics, not causal steps.

**"Very brief" means very brief — take it literally.** When the user asks for brief/quick/short answers, give the single answer sentence and stop. Do not add reinforcing restatements, the same point in different words, scope clarifications, or "also…" details. One sentence that answers the question is the whole reply. The mirrored text must be just as short — a brief request bounds the *text* too, not only the audio. If you think a caveat matters, offer it ("want the caveat?"), don't append it.

## Install / setup

Super Speech has two installations with one engine contract:

- The desktop installer bundles the engine, models, and UI. Its first launch writes `~/.super-speech/install.json` with the engine path
- The headless installer keeps the engine environment, models, queue, status, and logs in this skill's `runtime/` directory. It does not install Electron or write Super Speech files elsewhere

Both expose the same CLI and run the same engine source. Their mutable runtime directories are separate. Do not write queue or control files directly.

For a headless Codex installation, run the bundled installer with Python 3.11, 3.12, or 3.13. The first installation needs network access for Python packages and the two verified model files. On Windows, replace `3.12` below if another supported version is installed:

```powershell
$skill = '<absolute directory containing this SKILL.md>'
py -3.12 "$skill\scripts\install.py" --agent codex
```

```bash
SKILL='<absolute directory containing this SKILL.md>'
python3 "$SKILL/scripts/install.py" --agent codex
```

Use `--agent claude` for Claude Code, or `--target <skill-directory>` for a custom destination.

Resolve the engine once per reply. Set the skill directory to the absolute directory containing this loaded `SKILL.md`. On Windows, prefer a valid desktop engine and otherwise use this skill's headless engine:

```powershell
$skill = '<absolute directory containing this SKILL.md>'
$desktopRuntime = if ($env:SUPER_SPEECH_HOME) { $env:SUPER_SPEECH_HOME } else { Join-Path $env:USERPROFILE '.super-speech' }
$manifest = Join-Path $desktopRuntime 'install.json'
$engine = $null
if (Test-Path -LiteralPath $manifest) {
  try {
    $desktopEngine = (Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json).engine_path
    if ($desktopEngine -and (Test-Path -LiteralPath $desktopEngine)) { $engine = $desktopEngine }
  } catch {}
}
$headlessRuntime = Join-Path $skill 'runtime'
$headlessEngine = Join-Path $headlessRuntime 'venv\Scripts\super-speech-engine.exe'
if (-not $engine -and (Test-Path -LiteralPath $headlessEngine)) {
  $engine = $headlessEngine
  $env:SUPER_SPEECH_HOME = $headlessRuntime
  $env:SUPER_SPEECH_MODEL_DIR = Join-Path $headlessRuntime 'models\kokoro'
}
if (-not $engine) { throw 'Super Speech is not installed. Run this skill''s scripts/install.py.' }
```

On macOS, prefer the desktop manifest and otherwise use the headless virtual environment:

```bash
SKILL='<absolute directory containing this SKILL.md>'
DESKTOP_RUNTIME="${SUPER_SPEECH_HOME:-$HOME/.super-speech}"
ENGINE=""
if [ -f "$DESKTOP_RUNTIME/install.json" ]; then
  DESKTOP_ENGINE="$(plutil -extract engine_path raw "$DESKTOP_RUNTIME/install.json" 2>/dev/null || true)"
  [ -x "$DESKTOP_ENGINE" ] && ENGINE="$DESKTOP_ENGINE"
fi
if [ -z "$ENGINE" ]; then
  ENGINE="$SKILL/runtime/venv/bin/super-speech-engine"
  export SUPER_SPEECH_HOME="$SKILL/runtime"
  export SUPER_SPEECH_MODEL_DIR="$SKILL/runtime/models/kokoro"
fi
test -x "$ENGINE" || { echo "Super Speech is not installed. Run this skill's scripts/install.py." >&2; exit 1; }
```

## The shared engine CLI

Queue every chunk through `super-speech-engine speak`. That command starts the selected runtime's single engine process when needed and reserves the next queue number atomically. When the desktop app is running, it supervises the engine process it starts; otherwise the CLI starts the same engine itself.

```powershell
& $engine speak 'Your chunk text.' --voice bm_fable
```

```bash
"$ENGINE" speak "Your chunk text." --voice bm_fable
```

## Chunking rules

These rules are the **canonical chunking contract**. The auto-podcast and whatsapp-voice skills refer back here rather than restating them.

1. **First chunk is short.** Aim for one short sentence, ~90-110 chars. A 100-char chunk synthesizes in ~1-2s — that's your time-to-first-audio.

2. **Cap chunks at ~600 characters** (roughly 3-4 sentences). Above that, you lose the ability to control rhetorical pauses inside a thought (per-chunk `gMMM` gaps apply at chunk boundaries only) and the speech starts sounding monotone.

3. **Chunk-to-chunk growth doesn't matter.** (The old "strictly <1.5× growth" rule is retired as of 2026-06-10 — don't contort replies to satisfy it, and don't flag other agents' chunking for violating it. The growth *principle* survives one level up, between Bash calls — see workflow step 2.)

**Why growth stopped mattering:** the engine now splits every chunk at sentence boundaries into ≤250-char pieces and banks each piece in the playback buffer the moment it renders. A chunk starts playing once its FIRST piece is ready (~1-2s), not when the whole chunk is done, so a 70-char opener followed by a 700-char body — the classic gap-maker, ~10s of dead air under the old whole-chunk design — now transitions in under 0.5s (measured: 384ms live, with ~100-200ms pauses at intra-chunk sentence boundaries, which read as natural rhythm). Synth runs at ~3.5-4× realtime (6 ONNX threads on 8 cores — measured faster than the oversubscribed default), so the cushion only ever needs to cover one sentence. The engine logs every audible boundary as `boundary kind=chunk|piece silence=NNNms`; if a transition ever sounds wrong, that line is the ground truth.

## Voice-reply workflow

1. **Decide chunk count.**
   - 1 chunk for one-sentence replies
   - 2-4 chunks for moderate replies
   - More only if the reply is genuinely long enough to warrant it

2. **Queue in few, laddered CLI calls.** Two latencies compete here. Each tool call has ~10-15 s of round-trip latency, so one call per chunk can drip chunks in slower than the engine plays them. But composing one giant call delays first audio by however long the whole reply takes to write. The rule that balances both: **every call's queued audio must outlast the time it takes you to deliver the next call.**
   - Reply fits in ~2-4 chunks (≲900 chars total): queue it ALL in one call.
   - Longer reply: ladder the calls so speech starts before the full reply is composed — call 1 carries ~300 chars (~18 s of audio; a hard floor, not a style choice — a one-sentence first call buys ~5 s against the next call's ~13 s delivery and guarantees a gap), call 2 at most ~1.5× call 1, call 3 onward the rest (20+ s of cushion is banked by then and size stops mattering).
   - Why 1.5× and not more (measured 2026-06-10): per-call delivery is ~12-14 s fixed + ~80-100 chars/s of composition, so call 2 is the binding step — a 2.3× second step survived with <1 s of margin on a quiet machine and opened a 4.5 s gap on a loaded one (synth speed on this box swings ~1×-4× realtime with ambient CPU load). Never send a non-final call under ~250 chars.
   - Within each call, invoke the engine once per chunk:
   ```powershell
   & $engine speak 'First short chunk.' --voice bm_fable
   & $engine speak 'Second, somewhat longer chunk.' --voice bm_fable --gap-ms 600
   & $engine speak 'Third chunk.' --voice bm_fable --gap-ms 800
   ```
   The `speak` command starts the engine if needed and auto-picks the next chunk number.
   - Args: `speak "<text>" [--voice VOICE] [--gap-ms MILLISECONDS]`. The voice defaults to `af_heart`. For a male reply use `am_echo` or `bm_fable`; for another female voice use `af_aoede`. Override with any voice the user requests. Pass gap milliseconds as digits from 0 to 1500.
   - Write the chunk text for the ear (see TTS pronunciation gotchas) and obey the chunking rules (first chunk ~90-110 chars; cap ~600).
   - Apply per-chunk gaps via the `gap_ms` arg, same meanings as the `gMMM` filename token below.

3. **Mirror the spoken answer in the visible text — same content, or slightly more.** Not a bare one-line acknowledgement. When the user returns to the screen, reading the reply should give them *at least* what they heard, so nothing is lost if they missed part of the audio. Keep it a tight summary (the brevity principle still holds — don't expand it into a wall), but make it self-contained, and **include the specifics that are awkward in speech**: exact numbers, symbols, code/operators, file names, paths, URLs. The voice carries the gist; the text carries the gist *plus* the precise tokens speech mangles.

   **The text may add precision. It must never add, drop, or change substance (2026-07-27).** The only legitimate difference is representation: things speech mangles or cannot carry — exact figures, identifiers, paths, code blocks, tables, links. Everything else must match what was spoken, claim for claim.

   Specifically, NEVER let these exist in only one channel:
   - **A question or decision point.** If the text asks "A or B?" and the audio doesn't, a listener away from the screen has no idea they were asked, and silence looks like agreement. Every ask goes in the audio, phrased so it can be answered out loud.
   - **A conclusion or recommendation.** Do not end the audio on analysis and put "so this leans toward X" only in the text. The listener needs the conclusion most.
   - **A commitment about what happens next.** If the audio says "I'll do these three" and the text lists four, the user cannot tell which is real. State the same scope in both, with the same count.
   - **A correction or caveat that changes the answer.**

   Write the spoken chunks first, then write the text as that same content re-rendered for the eye. If while writing the text you find yourself adding a new point, stop — either it belongs in the audio too, or it does not belong at all. A mismatch here is not a style slip: it silently destroys the user's ability to act on the reply.

Do not bypass the CLI or write queue files directly. The CLI is the concurrency and startup boundary shared by the app and headless mode.

## TTS pronunciation gotchas

Kokoro reads literally. Spell things out in the chunk text:

- Method paths: `tools-slash-call`, not `tools/call`
- URLs: `file colon slash slash`, not `file://`
- Math: `equals`, `plus`, not `=`, `+`
- Avoid emoji and special chars like →, ←, ✓ — Windows cp1252 can also choke on those if not encoded as UTF-8
- Punctuation matters for prosody: commas, periods, em dashes all shape pacing

## Gaps and playback controls

Use `--gap-ms` for the pause before a chunk. Values from 500 to 1200 milliseconds sound conversational; reserve 1500 for a larger transition. Use 0 when chunks should flow together. Do not exceed 1500.

The CLI owns playback controls. Do not create or remove runtime files directly:

| Command | Effect |
|---|---|
| `pause` | Pause immediately at the current audio sample |
| `resume` | Resume from the same sample |
| `play <id>` | Play an exact `current`, `queue`, or `history` ID from `status` |
| `move <id> [before-id]` | Reorder a waiting item; omit `before-id` to move it last |
| `archive <id>` | Move one waiting item into History |
| `delete <id>` | Permanently remove one History item |
| `skip` | Archive the current chunk and continue |
| `clear` | Archive every queued chunk except the one currently playing |
| `stop` | Finish the current chunk and stop the engine |
| `interrupt` | Stop playback and the engine immediately |
| `status` | Print the current queue and playback status as JSON |

Selecting another upcoming chunk jumps to that point: the current chunk and
every older waiting chunk before the selection move to History, while newer
waiting chunks keep their order. Selecting a `history` ID queues a working copy
under the same ID, so
the original archive remains available and replay does not add duplicate History
rows. Selecting while paused resumes playback. Never derive an
ID from a path or mutate runtime files directly; use the exact opaque ID from
`status`.

The command waits for the engine to accept or reject the ID. On success it
prints JSON containing the exact resulting queue ID and acceptance time; a
missing ID exits with an error.

The `history` array is an archive of completed, skipped, and cleared chunks. Do
not assume that every history entry played to completion.

For a forced restart, run `interrupt`, then use `speak` normally. The next `speak` command starts the engine before it queues text.

## Configuration knobs in `engine/super_speech_engine.py`

| Constant | Purpose | Current |
|---|---|---|
| `CHUNK_GAP_S` | Default silence between chunks when filename has no `gMMM` segment | 0.2s |
| `SPLIT_CHARS` | Sentence-piece target for split synthesis (env `SUPER_SPEECH_SPLIT_CHARS`; 0 disables) | 250 |
| `DEFAULT_VOICE` | Voice when filename doesn't parse a valid ID | `af_bella` |
| `POLL_INTERVAL` | Idle poll cadence | 0.2s |
| `SIGNAL_TICK` | Signal check cadence during playback / gap | 0.02s |

Tuning requires a process restart; the model itself does not reload.

## Voice cheat sheet (Kokoro v1.0)

- `af_*` US female: alloy, aoede, bella, heart, jessica, kore, nicole, nova, river, sarah, sky
- `am_*` US male: adam, echo, eric, fenrir, liam, michael, onyx, puck, santa
- `bf_*` UK female: alice, emma, isabella, lily
- `bm_*` UK male: daniel, fable, george, lewis

Defaults for conversational replies:
- **Default**: `af_heart` (use this unless the user asks for another voice)
- **Male**: `am_echo` (`bm_fable` is also acceptable)
- **Female**: `af_aoede` (`af_kore`, `af_nova`, and `af_jessica` are also acceptable)

Use any other voice only when the user asks for it specifically.

## One engine implementation in both installations

`engine/super_speech_engine.py` is the authoritative implementation. The desktop build freezes that module into its bundled executable. The headless installer installs the same module into `runtime/venv/` beside this file. The skill invokes the resulting `super-speech-engine` command directly, so queueing, startup, controls, and audio behavior are not duplicated.

**Sibling skills that build on this one:** `auto-podcast` (long multi-chunk
podcasts) and `whatsapp-voice` (voice notes when the user is away from the
computer).
