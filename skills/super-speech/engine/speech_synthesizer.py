"""Render every voice family into samples for the same buffered audio player."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    import sherpa_onnx
    from kokoro_onnx import Kokoro
    from numpy.typing import NDArray

ALBA_VOICE = "piper_alba"
ALBA_DIRECTORY = "piper-alba"
ALBA_ARCHIVE_ROOT = "vits-piper-en_GB-alba-medium"
ALBA_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
    f"{ALBA_ARCHIVE_ROOT}.tar.bz2"
)
ALBA_SHA256 = "fcd45962906933eec4431d3688f7d74aaac8713c87c6717f91fd3b23463aa1a1"

# The voice chosen in Windows' Text to Speech settings, rendered by System.Speech
WINDOWS_VOICE = "win_default"
WINDOWS_VOICE_AVAILABLE = sys.platform == "win32"
WINDOWS_VOICE_UNAVAILABLE = f"{WINDOWS_VOICE} uses Windows' built-in speech and works only on Windows"
WINDOWS_SAMPLE_RATE = 22050
# Reads one base64 UTF-8 line per request and answers with one base64 line of
# 16-bit mono PCM. Any error ends the helper, so a failed request never looks
# like silence.
WINDOWS_HELPER_SCRIPT = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo({WINDOWS_SAMPLE_RATE}, 'Sixteen', 'Mono')
while ($null -ne ($line = [Console]::In.ReadLine())) {{
    $stream = New-Object System.IO.MemoryStream
    $synth.SetOutputToAudioStream($stream, $format)
    $synth.Speak([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line)))
    $synth.SetOutputToNull()
    [Console]::Out.WriteLine([Convert]::ToBase64String($stream.ToArray()))
}}
"""


def install_alba_model(destination: Path) -> None:
    """Install the pinned Piper bundle, including its phonemizer data and notices."""
    marker = destination / "archive.sha256"
    if marker.is_file() and marker.read_text() == ALBA_SHA256:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        archive = Path(temporary) / "alba.tar.bz2"
        with urllib.request.urlopen(ALBA_URL, timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        with archive.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != ALBA_SHA256:
            raise RuntimeError("Alba model download failed its SHA-256 check")
        with tarfile.open(archive) as bundle:
            bundle.extractall(temporary, filter="data")
        shutil.copytree(Path(temporary) / ALBA_ARCHIVE_ROOT, destination, dirs_exist_ok=True)
        # Write completion only after all model and phonemizer files are in place
        marker.write_text(ALBA_SHA256)


class WindowsSpeech:
    """Keep one hidden PowerShell process so each piece skips its startup cost."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None

    def generate(self, text: str) -> tuple[NDArray[np.float32], int]:
        import numpy as np

        if not WINDOWS_VOICE_AVAILABLE:
            raise RuntimeError(WINDOWS_VOICE_UNAVAILABLE)
        if self.process is None:
            # Windows PowerShell 5.1 ships with Windows and includes System.Speech
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            self.process = subprocess.Popen(
                [
                    str(system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    WINDOWS_HELPER_SCRIPT,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                encoding="ascii",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        process = self.process
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(base64.b64encode(text.encode()).decode() + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("Windows speech helper exited")
        except (OSError, RuntimeError):
            # Start a fresh helper for the next piece
            self.process = None
            process.kill()
            process.wait()
            raise
        pcm = np.frombuffer(base64.b64decode(line), dtype="<i2")
        return pcm.astype(np.float32) / 32768, WINDOWS_SAMPLE_RATE


class SpeechSynthesizer:
    """Keep model choice separate from splitting, buffering, and sample-accurate pause."""

    def __init__(self, kokoro: Kokoro, models: Path):
        self.kokoro = kokoro
        self.models = models
        self.alba: sherpa_onnx.OfflineTts | None = None
        self.windows = WindowsSpeech()

    def create(
        self, text: str, *, voice: str, speed: float, lang: str
    ) -> tuple[NDArray[np.float32], int]:
        if voice == WINDOWS_VOICE:
            # The engine always requests speed 1.0, which is the Windows default rate
            return self.windows.generate(text)
        if voice != ALBA_VOICE:
            return self.kokoro.create(text, voice=voice, speed=speed, lang=lang)
        if self.alba is None:
            import sherpa_onnx as so

            directory = self.models / ALBA_DIRECTORY
            # Load Alba on first use; cap its CPU work independently of Kokoro
            self.alba = so.OfflineTts(so.OfflineTtsConfig(
                model=so.OfflineTtsModelConfig(
                    vits=so.OfflineTtsVitsModelConfig(
                        model=str(directory / "en_GB-alba-medium.onnx"),
                        tokens=str(directory / "tokens.txt"),
                        data_dir=str(directory / "espeak-ng-data"),
                    ),
                    num_threads=2,
                    provider="cpu",
                ),
            ))
        audio = self.alba.generate(text, sid=0, speed=speed)
        import numpy as np

        # Convert sherpa's Python list to the float32 array used by the audio player
        return np.asarray(audio.samples, dtype=np.float32), audio.sample_rate
