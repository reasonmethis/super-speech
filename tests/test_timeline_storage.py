from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ENGINE_SOURCE = Path(__file__).parents[1] / "skills" / "super-speech" / "engine"
sys.path.insert(0, str(ENGINE_SOURCE))

import timeline_storage as timeline_storage_module
from speechicle_identity import SpeechicleFilename
from timeline_storage import TimelinePaths, TimelineStorage


def prepared_storage(tmp_path: Path) -> TimelineStorage:
    storage = TimelineStorage(TimelinePaths(tmp_path), "af_bella")
    storage.prepare(SimpleNamespace(held=True))
    return storage


def test_prepare_requires_the_engine_owner_lock(tmp_path: Path) -> None:
    storage = TimelineStorage(TimelinePaths(tmp_path), "af_bella")

    with pytest.raises(RuntimeError, match="held engine instance lock"):
        storage.prepare(SimpleNamespace(held=False))


def test_reserve_uses_only_the_counter_after_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = prepared_storage(tmp_path)
    real_glob = Path.glob

    def reject_archive_scan(path: Path, pattern: str):
        if path in {storage.paths.history, storage.paths.failed}:
            raise AssertionError("steady-state reserve scanned archived storage")
        return real_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", reject_archive_scan)
    monkeypatch.setattr(
        storage,
        "canonical_inventory",
        lambda: (_ for _ in ()).throw(
            AssertionError("steady-state reserve rebuilt the inventory")
        ),
    )
    monkeypatch.setattr(
        timeline_storage_module,
        "plan_embed_public_ids",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("steady-state reserve ran upgrade planning")
        ),
    )

    queued = storage.reserve("af_heart", None, "Hello")

    assert queued.read_text(encoding="utf-8") == "Hello"
    assert SpeechicleFilename.parse(queued.name).sequence == 1


def test_source_label_follows_one_id_through_voice_history_and_delete(
    tmp_path: Path,
) -> None:
    storage = prepared_storage(tmp_path)
    queued = storage.reserve("af_heart", None, "Hello", "Codex UI task")
    speechicle_id = storage.public_id(queued)

    changed = storage.replace_queue_voice(queued, "bm_fable")
    assert storage.source_label(speechicle_id) == "Codex UI task"

    assert storage.archive_many([changed])
    assert storage.history_snapshot(50)[1] == [
        {
            "id": speechicle_id,
            "text": "Hello",
            "voice": "bm_fable",
            "source": "Codex UI task",
        }
    ]

    assert storage.delete_history(speechicle_id) is not None
    assert storage.source_label(speechicle_id) is None


def test_reserve_rejects_invalid_source_metadata(tmp_path: Path) -> None:
    storage = prepared_storage(tmp_path)

    with pytest.raises(ValueError, match="invalid source label"):
        storage.reserve("af_heart", None, "Hello", "first line\nsecond line")


def test_counter_gap_after_failed_file_publish_is_never_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = prepared_storage(tmp_path)
    real_replace = timeline_storage_module.os.replace
    failed_once = False

    def fail_first_speech_publish(source: Path, target: Path) -> None:
        nonlocal failed_once
        if Path(target).suffix == ".txt" and not failed_once:
            failed_once = True
            raise PermissionError("simulated publish failure")
        real_replace(source, target)

    monkeypatch.setattr(timeline_storage_module.os, "replace", fail_first_speech_publish)

    with pytest.raises(PermissionError, match="simulated publish failure"):
        storage.reserve("af_heart", None, "Lost before publish")
    assert not list(storage.paths.queue.glob("*.txt"))
    assert not list(storage.paths.queue.glob("*.tmp"))

    queued = storage.reserve("af_heart", None, "Next")

    filename = SpeechicleFilename.parse(queued.name)
    assert filename.sequence == 2
    assert filename.public_id.endswith("0000000000000002")
    assert list(storage.paths.queue.glob("*.txt")) == [queued]


def test_committed_queue_publish_survives_a_reported_replace_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = prepared_storage(tmp_path)
    real_replace = timeline_storage_module.os.replace
    reported_once = False

    def commit_then_report_error(source: Path, target: Path) -> None:
        nonlocal reported_once
        real_replace(source, target)
        if Path(target).suffix == ".txt" and not reported_once:
            reported_once = True
            raise PermissionError("simulated late publish error")

    monkeypatch.setattr(
        timeline_storage_module.os, "replace", commit_then_report_error
    )

    queued = storage.reserve("af_heart", None, "Published once")

    assert queued.read_text(encoding="utf-8") == "Published once"
    assert list(storage.paths.queue.glob("*.txt")) == [queued]
    assert not list(storage.paths.queue.glob("*.tmp"))


def test_prepare_rejects_an_exhausted_sequence_space(tmp_path: Path) -> None:
    paths = TimelinePaths(tmp_path)
    paths.queue.mkdir()
    paths.history.mkdir()
    paths.failed.mkdir()
    exhausted = SpeechicleFilename(
        0xFFFFFFFFFFFFFFFF,
        "sp_ffffffffffffffffffffffffffffffff",
        "af_heart",
        None,
    )
    (paths.history / exhausted.render()).write_text("Last", encoding="utf-8")
    storage = TimelineStorage(paths, "af_bella")

    with pytest.raises(RuntimeError, match="speech sequence counter is exhausted"):
        storage.prepare(SimpleNamespace(held=True))

    assert not paths.sequence_counter.exists()


def test_pending_upgrade_blocks_steady_state_reserve(tmp_path: Path) -> None:
    storage = prepared_storage(tmp_path)
    before = storage.paths.sequence_counter.read_bytes()
    storage.paths.intent.write_text(
        json.dumps({"version": 3, "operation": "embed_public_ids"}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="requires an engine restart"):
        storage.reserve("af_heart", None, "Wait for recovery")

    assert storage.paths.sequence_counter.read_bytes() == before
    assert not list(storage.paths.queue.glob("*.txt"))


def test_resident_archive_survives_concurrent_enqueue_with_large_history(
    tmp_path: Path,
) -> None:
    cli_storage = prepared_storage(tmp_path)
    resident_storage = TimelineStorage(TimelinePaths(tmp_path), "af_bella")
    current = cli_storage.reserve("af_heart", None, "Current")
    for sequence in range(10_000, 12_000):
        archived = cli_storage.paths.history / (
            f"{sequence}-sp_{sequence:032x}-af_heart-say.txt"
        )
        archived.write_text(f"History {sequence}", encoding="utf-8")

    entered = tmp_path / "child-entered"
    release = tmp_path / "release-child"
    child_code = f"""
import json
import os
import time
from pathlib import Path
from timeline_storage import TimelinePaths, TimelineStorage

root = Path({str(tmp_path)!r})
entered = Path({str(entered)!r})
release = Path({str(release)!r})
storage = TimelineStorage(TimelinePaths(root), "af_bella")
write_counter = storage._write_sequence_counter

def hold_after_counter_advance(namespace, next_sequence):
    write_counter(namespace, next_sequence)
    entered.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not release.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("parent did not release child")
        time.sleep(0.01)

storage._write_sequence_counter = hold_after_counter_advance
queued = storage.reserve("af_heart", None, "Next")
print(json.dumps({{"pid": os.getpid(), "path": str(queued)}}))
"""
    environment = {**os.environ, "PYTHONPATH": str(ENGINE_SOURCE)}
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    deadline = time.monotonic() + 5
    while not entered.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert entered.exists()

    archive_results: list[bool] = []

    def finish_current() -> None:
        archive_results.append(resident_storage.archive(current))

    finisher = threading.Thread(target=finish_current)
    finisher.start()
    assert finisher.is_alive()
    release.write_text("go", encoding="utf-8")
    stdout, stderr = child.communicate(timeout=10)
    finisher.join(10)

    assert child.returncode == 0, stderr
    assert not finisher.is_alive()
    child_result = json.loads(stdout)
    assert child_result["pid"] != os.getpid()
    enqueued = Path(child_result["path"])
    assert archive_results == [True]
    assert not current.exists()
    assert enqueued.exists()
    assert (resident_storage.paths.history / current.name).exists()
    assert len(list(resident_storage.paths.history.glob("*.txt"))) == 2_001


def test_external_reserve_has_a_finite_timeline_lock_wait(tmp_path: Path) -> None:
    storage = prepared_storage(tmp_path)
    child_code = f"""
from pathlib import Path
import timeline_storage as module
from timeline_storage import TimelinePaths, TimelineStorage

storage = TimelineStorage(TimelinePaths(Path({str(tmp_path)!r})), "af_bella")
times = iter((0.0, 11.0))
module.time.monotonic = lambda: next(times, 11.0)
try:
    storage.reserve("af_heart", None, "Blocked")
except RuntimeError as error:
    print(error)
else:
    raise AssertionError("reserve waited without a time limit")
"""
    environment = {**os.environ, "PYTHONPATH": str(ENGINE_SOURCE)}

    with storage.mutation(timeout=None):
        child = subprocess.run(
            [sys.executable, "-c", child_code],
            text=True,
            capture_output=True,
            timeout=5,
            env=environment,
            check=False,
        )

    assert child.returncode == 0, child.stderr
    assert "timed out waiting for the speech timeline lock" in child.stdout
    assert not list(storage.paths.queue.glob("*.txt"))
