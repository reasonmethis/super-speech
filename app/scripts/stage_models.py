import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_SOURCE = REPO_ROOT / "skills" / "super-speech" / "engine"
sys.path.insert(0, str(ENGINE_SOURCE))

from super_speech_engine import install_models  # noqa: E402


DESTINATION = Path(__file__).resolve().parents[1] / "build-resources" / "models" / "kokoro"


if __name__ == "__main__":
    install_models(DESTINATION)
