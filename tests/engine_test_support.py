from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ENGINE_SOURCE = Path(__file__).parents[1] / "skills" / "super-speech" / "engine"

from speechicle_identity import IdentityCatalog, write_catalog


class CallbackStop(Exception):
    pass


def load_engine(module_name: str):
    module_path = ENGINE_SOURCE / "super_speech_engine.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec and spec.loader
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    return engine


def configure_runtime(
    engine, tmp_path: Path, *, create_directories: bool = True
) -> None:
    engine.BASE = tmp_path
    engine.TIMELINE_PATHS = engine.TimelinePaths(tmp_path)
    engine.timeline = engine.TimelineStorage(engine.TIMELINE_PATHS, engine.DEFAULT_VOICE)
    engine.QUEUE = engine.timeline.paths.queue
    engine.SPOKEN = engine.timeline.paths.history
    engine.FAILED = engine.timeline.paths.failed
    engine.LOG = tmp_path / "log.txt"
    engine.PAUSE = tmp_path / "PAUSE"
    engine.STOP = tmp_path / "STOP"
    engine.INTERRUPT = tmp_path / "INTERRUPT"
    engine.SKIP = tmp_path / "SKIP"
    engine.CONTINUE = tmp_path / "CONTINUE"
    engine.MUTATION = tmp_path / "MUTATION.json"
    engine.WARMUP = tmp_path / "WARMUP"
    engine.HEARTBEAT = tmp_path / "engine.alive"
    engine.STATUS = tmp_path / "status.json"
    engine.STATUS_FAILURE = tmp_path / "status.failed"
    engine.STORAGE_READY = tmp_path / "storage-ready.json"
    engine.INSTANCE_LOCK = tmp_path / "engine.lock"
    engine.PLAYBACK_COMMAND_LOCK = tmp_path / "playback-command.lock"
    engine.PLAYBACK_COMMAND_SEQUENCE = tmp_path / "playback-command-sequence.json"
    if create_directories:
        engine.QUEUE.mkdir(exist_ok=True)
        engine.SPOKEN.mkdir(exist_ok=True)
        engine.timeline.paths.sources.mkdir(exist_ok=True)


def set_current(
    engine,
    state,
    path: Path,
    *,
    text: str | None = None,
    piece: int = 0,
    piece_start: int | None = None,
    piece_end: int | None = None,
) -> None:
    current_text = path.read_text(encoding="utf-8") if text is None else text
    active_piece = (
        None
        if piece == 0
        else engine.ActivePiece(
            piece,
            0 if piece_start is None else piece_start,
            len(current_text) if piece_end is None else piece_end,
        )
    )
    state.current_projection = engine.CurrentProjection(
        path.name,
        current_text,
        active_piece,
    )


def buffered_piece(
    engine,
    path: Path,
    audio: object,
    *,
    generation: int = 0,
    first: bool = True,
    last: bool = True,
    piece: int = 1,
) -> object:
    text = path.read_text(encoding="utf-8")
    return engine.BufferedPiece(
        path=path,
        audio=audio,
        sample_rate=1000,
        is_first_piece=first,
        is_last_piece=last,
        piece_number=piece,
        speechicle_text=text,
        piece_start=0,
        piece_end=len(text),
        claim_generation=generation,
    )


def claim_next_speechicle(engine, state) -> Path | None:
    claim = engine.claim_next_queued_chunk_with_generation(state)
    return claim[0] if claim is not None else None


def speechicle_id(engine, path: Path) -> str:
    return engine.public_id_for_path(path)


def prepare_timeline(engine) -> None:
    instance_lock = engine.EngineInstanceLock()
    assert instance_lock.acquire()
    try:
        engine.prepare_timeline_storage(instance_lock)
    finally:
        instance_lock.release()


def write_upgrade_catalog(engine, *paths: Path) -> None:
    filenames = [engine.SpeechicleFilename.parse(path.name) for path in paths]
    write_catalog(
        engine.timeline.paths.legacy_identity_index,
        IdentityCatalog(
            max((filename.sequence for filename in filenames), default=0) + 1,
            {
                filename.sequence: filename.public_id
                for filename in filenames
            },
        ),
    )


def loading_status(engine, engine_pid: int, updated_at: float = 0) -> dict[str, object]:
    return {
        "version": engine.STATUS_VERSION,
        "timeline_revision": 0,
        "state": "loading",
        "updated_at": updated_at,
        "engine_pid": engine_pid,
        "current": None,
        "queue_count": 0,
        "queue": [],
        "history_count": 0,
        "history": [],
    }


def write_storage_ready(engine, engine_pid: int) -> None:
    engine.STORAGE_READY.write_text(
        json.dumps({"engine_pid": engine_pid}), encoding="utf-8"
    )


def request_mutation(engine, mutation_type: str, **fields: object) -> str:
    return engine.request_mutation(
        engine.build_mutation_request(mutation_type, **fields)
    )


def build_mutation(engine, mutation_type: str, **fields: object):
    return engine.build_mutation_request(mutation_type, **fields)


def committed_result(engine, request_id: str) -> dict[str, object]:
    result = engine.wait_for_mutation_result(request_id, timeout=0.1)
    assert result["outcome"] == "committed"
    return result


def rejected_result(engine, request_id: str) -> dict[str, object]:
    result = engine.wait_for_mutation_result(request_id, timeout=0.1)
    assert result["outcome"] == "rejected"
    return result
