#!/usr/bin/env bash
# Ensure the Kokoro drainer process is running.
#
# PLATFORM: Windows + git-bash only — uses powershell, Start-Process,
# Get-CimInstance and `pwd -W`. On macOS/Linux do not run this script; see the
# "Platform note" in SETUP.md for the POSIX port (pgrep -f drainer-kokoro to
# detect; nohup "<python>" drainer-kokoro.py & to launch).
#
# Idempotent: if the drainer is already up, prints "running" and exits 0;
# otherwise starts it detached, waits up to ~12s for the cold start (Kokoro
# model load + warmup is ~5-7s), and prints "started". Chunks queued before
# the drainer finishes warming up still get played once it enters its loop.
#
# Used by speak.sh and by the super-speech skill before queueing any chunk,
# so a dead drainer never silently swallows speech.
set -u

# Locate a Python interpreter for the drainer (needs kokoro-onnx installed).
# Resolution order — no machine-specific path is ever baked in:
#   1. $SUPER_SPEECH_PYTHON                explicit override
#   2. PYTHON= line in super-speech.paths  recorded by SETUP.md
#   3. py / python3 / python on PATH       best-effort default
SPEECH_HOME="${SUPER_SPEECH_HOME:-$HOME/.super-speech}"
PATHS_FILE="$SPEECH_HOME/super-speech.paths"
HEARTBEAT="$SPEECH_HOME/drainer.alive"
HEARTBEAT_STALE_S=15   # a heartbeat newer than this => drainer is alive (cheap mtime read)

resolve_python() {
  if [ -n "${SUPER_SPEECH_PYTHON:-}" ]; then
    printf '%s\n' "$SUPER_SPEECH_PYTHON"; return 0
  fi
  if [ -f "$PATHS_FILE" ]; then
    recorded=$(sed -n 's/^PYTHON=//p' "$PATHS_FILE" | head -1)
    if [ -n "$recorded" ]; then printf '%s\n' "$recorded"; return 0; fi
  fi
  for c in py python3 python; do
    if command -v "$c" >/dev/null 2>&1; then printf '%s\n' "$c"; return 0; fi
  done
  return 1
}

if ! DRAINER_PY=$(resolve_python); then
  echo "ensure-drainer: no Python interpreter found." >&2
  echo "  Set SUPER_SPEECH_PYTHON, or add a 'PYTHON=<path>' line to" >&2
  echo "  $PATHS_FILE (SETUP.md does this automatically)." >&2
  exit 1
fi
# Resolve drainer-kokoro.py next to this script, as a Windows path (pwd -W).
# The native python.exe launched below via Start-Process cannot open a git-bash
# "/c/Users/..." path — Windows misreads the leading "/c/" as "C:\c\...".
DRAINER_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -W)/drainer-kokoro.py"

_drainer_scan() {
  # Authoritative but slow (~2-3s): scan every process for the drainer.
  powershell -NoProfile -Command \
    "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { \$_.CommandLine -like '*drainer-kokoro*' }) { 'yes' }" \
    2>/dev/null | tr -d '\r\n '
}

_drainer_running() {
  # Fast path: a heartbeat file freshly touched by the drainer means it's alive.
  # That's a microsecond mtime read instead of the ~2-3s process scan, so queueing
  # many chunks no longer pays the scan once per chunk. Only when the heartbeat is
  # missing or stale do we fall back to the authoritative scan -- so a long blocking
  # synth (which briefly pauses the heartbeat) never makes us launch a 2nd drainer.
  if [ -f "$HEARTBEAT" ]; then
    now=$(date +%s)
    mtime=$(stat -c %Y "$HEARTBEAT" 2>/dev/null)
    if [ -n "$mtime" ] && [ "$(( now - mtime ))" -lt "$HEARTBEAT_STALE_S" ]; then
      echo "yes"
      return
    fi
  fi
  _drainer_scan
}

if [ "$(_drainer_running)" = "yes" ]; then
  echo "running"
  exit 0
fi

echo "drainer not running -> starting it" >&2
powershell -NoProfile -Command \
  "Start-Process -FilePath '$DRAINER_PY' -ArgumentList '$DRAINER_SCRIPT' -WindowStyle Hidden" \
  >/dev/null 2>&1

for _ in $(seq 1 12); do
  sleep 1
  [ "$(_drainer_running)" = "yes" ] && break
done
echo "started"
