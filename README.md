# super-speech

Local text-to-speech voice replies for Claude Code. Claude speaks its answers
aloud through the **Kokoro** neural TTS engine running entirely on your machine
— no cloud service, no API key, no per-word billing.

It is the **base voice skill**. Two siblings build on it: **auto-podcast** (long
multi-chunk spoken segments) and **whatsapp-voice** (voice notes when you are
away from the computer). super-speech itself is voice-reply mode: short spoken
answers, turn by turn, through a local background drainer process.

## Install

One official command — add the marketplace:

```bash
claude plugin marketplace add reasonmethis/super-speech
```

## Setup

After adding the marketplace, tell Claude this exact prompt:

> **Set up the super-speech marketplace.**

That phrase is all you need. The word "marketplace" tells Claude exactly where
to look: `claude plugin marketplace add` clones the whole repo into
`~/.claude/plugins/marketplaces/super-speech/`, so Claude finds `SETUP.md` there
and follows it. Nothing to memorize, nothing piped from the internet.

`SETUP.md` is an agentic playbook — Claude runs it for you in five phases:

0. **Install the plugin** — runs `claude plugin install super-speech@super-speech`
   (the `marketplace add` step only registers the marketplace), resolves the
   installed plugin directory, and records its script paths to
   `~/.claude/podcast/super-speech.paths`.
1. **Bootstrap** — finds a Python 3 interpreter, installs the Python deps
   (`kokoro-onnx`, `numpy`, `soundfile`), and downloads the Kokoro v1.0 voice
   model into `~/.claude/models/kokoro/`.
2. **Verify** — starts the drainer, queues a test chunk, checks the log, and
   asks you to confirm you heard audio.
3. **Self-heal** — if anything fails, Claude reads the error, fixes it, and
   re-runs verification until it passes.
4. **Voice picker** — optionally samples five recommended voices and saves your
   favorite as the default.

**First-run download:** the Kokoro v1.0 model files total about **338 MB**
(`kokoro-v1.0.onnx` ~311 MB + `voices-v1.0.bin` ~27 MB). They download once
during setup and are reused afterward.

## Usage

Once set up, just ask Claude to reply by voice ("use voice", "speak your
answer"). The `super-speech` skill handles chunking, the drainer lifecycle, and
voice selection. See `skills/super-speech/SKILL.md` for the full chunking
contract and TTS details.

## License

MIT — see `LICENSE`.
