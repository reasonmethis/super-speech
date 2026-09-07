"""Render both voice families into samples for the same buffered audio player."""

from __future__ import annotations

import hashlib
import shutil
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


class SpeechSynthesizer:
    """Keep model choice separate from splitting, buffering, and sample-accurate pause."""

    def __init__(self, kokoro: Kokoro, models: Path):
        self.kokoro = kokoro
        self.models = models
        self.alba: sherpa_onnx.OfflineTts | None = None

    def create(
        self, text: str, *, voice: str, speed: float, lang: str
    ) -> tuple[NDArray[np.float32], int]:
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
