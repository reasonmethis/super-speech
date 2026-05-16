# super-speech

Local text-to-speech voice replies for AI coding agents. Your agent speaks its
answers aloud through the **Kokoro** neural TTS engine running entirely on your
machine — no cloud service, no API key, no per-word billing.

It is the **base voice skill**. Two siblings build on it: **auto-podcast** (long
multi-chunk spoken segments) and **whatsapp-voice** (voice notes when you are
away from the computer). super-speech itself is voice-reply mode: short spoken
answers, turn by turn, through a local background drainer process.

Works with any capable coding agent — Claude Code, Codex, OpenCode — on Windows
out of the box; macOS/Linux with a small adaptation the setup playbook explains.

## Install

Clone the repo:

```bash
git clone https://github.com/reasonmethis/super-speech.git
```

Then tell your agent:

> **Set up super-speech by following SETUP.md in the cloned repo.**

`SETUP.md` is an agentic playbook — your agent runs it end to end: locate the
scripts, install the Python dependencies, download the Kokoro voice model,
verify audio works, self-heal any failures, and pick a default voice.

**Claude Code shortcut:** instead of cloning manually you can add the repo as a
marketplace — `claude plugin marketplace add reasonmethis/super-speech` — then
tell Claude *"Set up the super-speech marketplace."* SETUP.md handles the rest.

**First-run download:** the Kokoro v1.0 model files total about **338 MB**
(`kokoro-v1.0.onnx` ~311 MB + `voices-v1.0.bin` ~27 MB). They download once
during setup into `~/.super-speech/models/kokoro/` and are reused afterward.

## Usage

Once set up, just ask your agent to reply by voice ("use voice", "speak your
answer"). The `super-speech` skill handles chunking, the drainer lifecycle, and
voice selection. See `skills/super-speech/SKILL.md` for the full chunking
contract and TTS details.

## Platform support

`drainer-kokoro.py` (the TTS engine) and `speak.sh` are cross-platform.
`ensure-drainer.sh` — the drainer launcher — is written for Windows + git-bash;
on macOS/Linux the setup playbook tells the agent how to substitute the short
POSIX equivalents. The runtime home `~/.super-speech/` is the same on every OS.

## License

MIT — see `LICENSE`.
