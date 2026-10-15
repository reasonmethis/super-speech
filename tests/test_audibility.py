from __future__ import annotations

import json
import time
from pathlib import Path

from audibility import SPAN_KEEP_S, AudibleSpanFile


def read_spans(path: Path) -> list[list[float | None]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    return payload["spans"]


def test_one_reply_is_published_as_one_wall_clock_span(tmp_path: Path) -> None:
    path = tmp_path / "audible-spans.json"
    spans = AudibleSpanFile(path, lambda _message: None)
    spans.begin()
    started = time.monotonic()

    spans.update(True, started)
    [[start, end]] = read_spans(path)
    assert end is None
    assert abs(start - time.time()) < 0.1

    spans.update(True, started + 1.0)
    spans.update(False, started + 2.0)
    [[start_again, end]] = read_spans(path)
    assert start_again == start
    assert end is not None
    assert abs((end - start) - 2.0) < 0.05


def test_open_span_is_refreshed_while_speech_continues(tmp_path: Path) -> None:
    path = tmp_path / "audible-spans.json"
    spans = AudibleSpanFile(path, lambda _message: None, refresh=0.0)
    spans.begin()
    spans.update(True, time.monotonic())
    first = json.loads(path.read_text(encoding="utf-8"))["updated"]
    time.sleep(0.02)

    spans.update(True, time.monotonic())

    assert json.loads(path.read_text(encoding="utf-8"))["updated"] > first


def test_stopping_closes_the_open_span(tmp_path: Path) -> None:
    path = tmp_path / "audible-spans.json"
    spans = AudibleSpanFile(path, lambda _message: None)
    spans.begin()
    spans.update(True, time.monotonic())

    spans.end()

    [[_start, end]] = read_spans(path)
    assert end is not None


def test_starting_discards_a_previous_engines_spans(tmp_path: Path) -> None:
    path = tmp_path / "audible-spans.json"
    path.write_text(
        json.dumps({"version": 1, "updated": 0, "spans": [[0, None]]}),
        encoding="utf-8",
    )

    AudibleSpanFile(path, lambda _message: None).begin()

    assert not path.exists()


def test_spans_older_than_the_longest_take_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "audible-spans.json"
    spans = AudibleSpanFile(path, lambda _message: None)
    spans.begin()
    now = time.monotonic()
    spans.update(True, now - SPAN_KEEP_S - 10)
    spans.update(False, now - SPAN_KEEP_S - 5)

    spans.update(True, now)

    assert len(read_spans(path)) == 1


def test_a_blocked_write_is_retried_on_the_next_sample(tmp_path: Path) -> None:
    path = tmp_path / "audible-spans.json"
    messages: list[str] = []
    spans = AudibleSpanFile(path, messages.append)
    spans.begin()
    path.mkdir()  # replacing a directory with a file fails like a locked file

    spans.update(True, time.monotonic())
    spans.update(True, time.monotonic())
    path.rmdir()
    spans.update(True, time.monotonic())

    assert len(messages) == 1
    assert read_spans(path)[0][1] is None
