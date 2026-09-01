from __future__ import annotations

from pathlib import Path

import pytest

from file_lock import InterprocessFileLock


def test_lock_excludes_another_owner_until_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "engine.lock"
    first = InterprocessFileLock(lock_path)
    second = InterprocessFileLock(lock_path)

    assert first.acquire()
    assert first.held
    assert not second.acquire()
    assert not second.held

    first.release()
    assert not first.held
    assert second.acquire()
    assert second.held
    second.release()


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
