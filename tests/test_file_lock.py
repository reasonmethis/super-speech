from __future__ import annotations

import sys
from pathlib import Path

import pytest


ENGINE_SOURCE = Path(__file__).parents[1] / "skills" / "super-speech" / "engine"
sys.path.insert(0, str(ENGINE_SOURCE))

from file_lock import InterprocessFileLock


def test_lock_reports_an_unavailable_file_as_not_acquired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lock_path = tmp_path / "engine.lock"
    real_open = Path.open

    def deny_lock_file(path: Path, *args, **kwargs):
        if path == lock_path:
            raise PermissionError("temporarily unavailable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_lock_file)

    lock = InterprocessFileLock(lock_path)

    assert not lock.acquire()
    assert not lock.held
