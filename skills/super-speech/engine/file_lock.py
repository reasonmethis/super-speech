"""Small cross-platform file lock shared by the engine and timeline storage."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class InterprocessFileLock:
    """Hold one cross-platform, one-byte advisory file lock."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> bool:
        lock_file: BinaryIO | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = self._path.open("a+b")
            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if lock_file is not None:
                try:
                    lock_file.close()
                except OSError:
                    pass
            return False
        self._file = lock_file
        return True

    def release(self) -> None:
        if self._file is None:
            return
        self._file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None

    @property
    def held(self) -> bool:
        return self._file is not None
