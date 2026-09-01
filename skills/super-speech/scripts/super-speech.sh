#!/bin/sh

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
skill_directory=$(dirname -- "$script_directory")
desktop_runtime=${SUPER_SPEECH_HOME:-"$HOME/.super-speech"}
manifest_path="$desktop_runtime/install.json"
engine_path=

if [ -f "$manifest_path" ] && command -v plutil >/dev/null 2>&1; then
    desktop_engine=$(plutil -extract engine_path raw "$manifest_path" 2>/dev/null) || desktop_engine=
    if [ -n "$desktop_engine" ] && [ -x "$desktop_engine" ]; then
        engine_path=$desktop_engine
    fi
fi

if [ -n "$engine_path" ]; then
    exec "$engine_path" "$@"
fi

headless_runtime="$skill_directory/runtime"
headless_engine="$headless_runtime/venv/bin/super-speech-engine"
if [ ! -x "$headless_engine" ]; then
    echo "Super Speech is not installed. Run this skill's scripts/install.py." >&2
    exit 1
fi

SUPER_SPEECH_HOME=$headless_runtime
SUPER_SPEECH_MODEL_DIR="$headless_runtime/models/kokoro"
export SUPER_SPEECH_HOME SUPER_SPEECH_MODEL_DIR
exec "$headless_engine" "$@"
