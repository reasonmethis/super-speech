#!/usr/bin/env bash
# Queue one chunk of text for the Kokoro drainer to speak.
#
# Ensures the drainer is running first (via ensure-drainer.sh), then writes the
# chunk as the next-numbered file in the shared queue dir so you don't have
# to manage chunk numbers yourself.
#
# Usage: speak.sh "<text to speak>" [voice] [gap_ms]
#   voice  - Kokoro voice id (default am_echo; e.g. bm_fable, am_michael, ...)
#   gap_ms - optional pre-play gap in ms (e.g. 500); omitted -> drainer default
#
# Call once per chunk, in order. Follow the chunking rules in the super-speech
# skill (first chunk short ~50-60 chars; each chunk < 2x its predecessor; cap ~600).
# Prints the queued file path.
set -eu

TEXT="${1:?usage: speak.sh \"<text to speak>\" [voice] [gap_ms]}"
VOICE="${2:-am_echo}"
GAP="${3:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Agent-neutral runtime home — shared by every install (Claude Code, Codex, ...)
# and by the drainer. Override with SUPER_SPEECH_HOME; keep in sync with the
# BASE constant in drainer-kokoro.py.
SPEECH_HOME="${SUPER_SPEECH_HOME:-$HOME/.super-speech}"
QUEUE="$SPEECH_HOME/queue"
SPOKEN="$SPEECH_HOME/spoken"
mkdir -p "$QUEUE" "$SPOKEN"

# 1. Make sure the drainer is alive (starts it + waits for warmup if not).
bash "$HERE/ensure-drainer.sh" >/dev/null

# 2. Next chunk number = 1 + the highest NNN currently in queue or spoken.
maxn=$( { ls "$QUEUE" "$SPOKEN" 2>/dev/null; } | sed -n 's/^0*\([0-9][0-9]*\)-.*/\1/p' | sort -n | tail -1 )
n=$(( ${maxn:-0} + 1 ))

# 3. Write the chunk; bump the number and retry if a parallel caller raced us to it.
for _ in 1 2 3 4 5; do
  nnn=$(printf '%03d' "$n")
  if [ -n "$GAP" ]; then
    f="$QUEUE/${nnn}-${VOICE}-g${GAP}-say.txt"
  else
    f="$QUEUE/${nnn}-${VOICE}-say.txt"
  fi
  if ( set -o noclobber; printf '%s' "$TEXT" > "$f" ) 2>/dev/null; then
    echo "$f"
    exit 0
  fi
  n=$(( n + 1 ))
done
echo "speak.sh: failed to queue chunk after retries" >&2
exit 1
