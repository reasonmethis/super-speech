"""Follow complete messages appended to one Super Speech agent inbox."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from timeline_storage import normalize_inbox_path


def inbox_lines(
    inbox: str | Path,
    *,
    from_end: bool = False,
    stop: threading.Event | None = None,
    poll_interval: float = 0.1,
    on_ready: Callable[[Path], None] | None = None,
) -> Iterator[str]:
    """Yield newline-terminated entries without exposing partial appends."""
    normalized = normalize_inbox_path(str(inbox))
    if normalized is None:
        raise ValueError("inbox path is required")
    path = Path(normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8", newline="") as inbox_file:
        inbox_file.seek(0, 2 if from_end else 0)
        if on_ready is not None:
            on_ready(path)
        pending = ""
        while stop is None or not stop.is_set():
            content = inbox_file.read()
            if content:
                pending += content
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if line.endswith("\r"):
                        line = line[:-1]
                    if line:
                        yield line
                continue
            if stop is None:
                time.sleep(poll_interval)
            else:
                stop.wait(poll_interval)


def listen_inbox(inbox: str | Path, *, from_end: bool = False) -> None:
    """Print existing and future inbox messages until the caller stops listening."""
    normalized = normalize_inbox_path(str(inbox))
    if normalized is None:
        raise ValueError("inbox path is required")
    def report_ready(path: Path) -> None:
        print(f"Listening for Super Speech messages at {path}", file=sys.stderr)

    for line in inbox_lines(normalized, from_end=from_end, on_ready=report_ready):
        print(line, flush=True)
