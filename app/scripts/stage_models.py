from __future__ import annotations

import hashlib
import os
import shutil
import urllib.request
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
DESTINATION = APP_DIR / "build-resources" / "models" / "kokoro"
RELEASE_ROOT = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0"
)
ARTIFACTS = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def valid(path: Path, expected_hash: str) -> bool:
    return path.is_file() and file_hash(path) == expected_hash


def source_directory() -> Path:
    configured = os.environ.get("SUPER_SPEECH_MODEL_SOURCE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".super-speech" / "models" / "kokoro"


def stage(name: str, expected_hash: str) -> None:
    destination = DESTINATION / name
    if valid(destination, expected_hash):
        print(f"verified {destination}")
        return

    partial = destination.with_suffix(f"{destination.suffix}.partial")
    partial.unlink(missing_ok=True)
    local_source = source_directory() / name
    if valid(local_source, expected_hash):
        shutil.copyfile(local_source, partial)
    else:
        urllib.request.urlretrieve(f"{RELEASE_ROOT}/{name}", partial)

    actual_hash = file_hash(partial)
    if actual_hash != expected_hash:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"hash mismatch for {name}: expected {expected_hash}, got {actual_hash}"
        )
    os.replace(partial, destination)
    print(f"staged {destination}")


def main() -> None:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for name, expected_hash in ARTIFACTS.items():
        stage(name, expected_hash)


if __name__ == "__main__":
    main()
