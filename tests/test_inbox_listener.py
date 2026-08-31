from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ENGINE_SOURCE = Path(__file__).parents[1] / "skills" / "super-speech" / "engine"
sys.path.insert(0, str(ENGINE_SOURCE))

from inbox_listener import inbox_lines


def test_listener_emits_existing_and_new_complete_messages(tmp_path: Path) -> None:
    inbox = tmp_path / "session" / "inbox.jsonl"
    inbox.parent.mkdir()
    inbox.write_text('{"id":"first"}\n{"id":"partial"', encoding="utf-8")
    stop = threading.Event()
    received: list[str] = []

    def listen() -> None:
        for line in inbox_lines(inbox, stop=stop, poll_interval=0.01):
            received.append(line)
            if len(received) == 3:
                stop.set()

    listener = threading.Thread(target=listen)
    listener.start()
    deadline = time.monotonic() + 2
    while received != ['{"id":"first"}'] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert received == ['{"id":"first"}']

    with inbox.open("a", encoding="utf-8") as output:
        output.write('}\n{"id":"second"}\n')
        output.flush()

    listener.join(timeout=2)
    assert not listener.is_alive()
    assert received == [
        '{"id":"first"}',
        '{"id":"partial"}',
        '{"id":"second"}',
    ]


def test_listener_can_ignore_existing_messages(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.jsonl"
    inbox.write_text(json.dumps({"id": "old"}) + "\n", encoding="utf-8")
    stop = threading.Event()
    ready = threading.Event()
    received: list[str] = []

    def listen() -> None:
        received.append(
            next(
                inbox_lines(
                    inbox,
                    from_end=True,
                    stop=stop,
                    poll_interval=0.01,
                    on_ready=lambda _path: ready.set(),
                )
            )
        )

    listener = threading.Thread(target=listen)
    listener.start()
    assert ready.wait(timeout=2)
    with inbox.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"id": "new"}) + "\n")
        output.flush()

    listener.join(timeout=2)
    stop.set()
    assert not listener.is_alive()
    assert received == [json.dumps({"id": "new"})]
