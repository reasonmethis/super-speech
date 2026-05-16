#!/usr/bin/env bash
# Ensure the Kokoro auto-podcast drainer process is running.
#
# Idempotent: if the drainer is already up, prints "running" and exits 0;
# otherwise starts it detached, waits up to ~12s for the cold start (Kokoro
# model load + warmup is ~5-7s), and prints "started". Chunks queued before
# the drainer finishes warming up still get played once it enters its loop.
#
# Used by speak.sh and by the super-speech skill before queueing any chunk,
# so a dead drainer never silently swallows speech.
set -u

DRAINER_PY="C:/Users/micro/AppData/Local/Programs/Python/Python312/python.exe"
# Resolve drainer-kokoro.py next to this script so the plugin is portable.
DRAINER_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/drainer-kokoro.py"

_drainer_running() {
  # True iff there is a python.exe process whose command line mentions drainer-kokoro.
  powershell -NoProfile -Command \
    "if (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { \$_.CommandLine -like '*drainer-kokoro*' }) { 'yes' }" \
    2>/dev/null | tr -d '\r\n '
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
