import assert from "node:assert/strict";
import test from "node:test";
import {
  ENGINE_STATUS_VERSION,
  compatibleEngineIsRunning,
  engineProcessIsLive,
  moveQueueItemBefore,
  pendingPlaybackState,
  playAcceptanceState,
  playbackPresentation,
  parseEngineStatus,
  parseEngineProcessStatus,
  parsePlayAcceptance,
  runtimeStateForSnapshot,
  statusForEngineProcess,
  statusAfterPauseCommand,
  timelineItems,
  type EngineStatus,
} from "./runtime.ts";

const status: EngineStatus = {
  version: ENGINE_STATUS_VERSION,
  state: "stopped",
  updated_at: 1,
  engine_pid: 41,
  current: null,
  recent_starts: [],
  queue_count: 0,
  queue: [],
  history_count: 0,
  history: [],
};

test("accepts status from the current engine process", () => {
  assert.equal(statusForEngineProcess(status, 41), status);
});

test("ignores status from a previous engine process", () => {
  assert.equal(statusForEngineProcess(status, 42), null);
});

test("ignores status until the new engine has published its process ID", () => {
  assert.equal(statusForEngineProcess(status, undefined), null);
});

test("does not treat a stale heartbeat as a live engine", () => {
  const process = { updated_at: 100, engine_pid: 41 };
  assert.equal(engineProcessIsLive(process, true, () => false, 101), false);
  assert.equal(engineProcessIsLive(process, false, () => true, 101), true);
  assert.equal(engineProcessIsLive(process, true, () => true, 1_000), true);
});

test("a stopped engine cannot be presented as active because work is queued", () => {
  assert.equal(runtimeStateForSnapshot(true, false, "playing"), "stopped");
  assert.equal(runtimeStateForSnapshot(true, true, "playing"), "playing");
  assert.equal(runtimeStateForSnapshot(false, false, undefined), "setup_required");
});

test("an incompatible external engine cannot leave the app loading forever", () => {
  assert.equal(compatibleEngineIsRunning(false, null, true), false);
  assert.equal(compatibleEngineIsRunning(false, status, true), true);
  assert.equal(compatibleEngineIsRunning(true, null, false), true);
});

test("accepts a complete current-version status", () => {
  assert.equal(parseEngineStatus(status), status);
});

test("rejects an older status while retaining its process metadata", () => {
  const older = { ...status, version: ENGINE_STATUS_VERSION - 1 };
  assert.equal(parseEngineStatus(older), null);
  assert.deepEqual(parseEngineProcessStatus(older), {
    engine_pid: status.engine_pid,
    updated_at: status.updated_at,
  });
});

test("rejects a partial current-version status instead of inventing missing fields", () => {
  const { history: _history, ...partial } = status;
  assert.equal(parseEngineStatus(partial), null);
});

test("rejects a status whose waiting count disagrees with its queue", () => {
  assert.equal(parseEngineStatus({ ...status, queue_count: 1 }), null);
});

test("normalizes an engine play acknowledgement", () => {
  assert.deepEqual(parsePlayAcceptance({ id: "008-bm_fable-say", accepted_at: 12.5 }), {
    id: "008-bm_fable-say",
    acceptedAt: 12.5,
  });
  assert.equal(parsePlayAcceptance({ id: "008-bm_fable-say" }), null);
});

test("does not mistake an existing History row for started playback", () => {
  const archived = {
    id: "008-bm_fable-say",
    filename: "008-bm_fable-say.txt",
    text: "Play this again",
    voice: "bm_fable",
  };
  const acceptance = { id: archived.id, acceptedAt: 12 };

  assert.equal(
    playAcceptanceState(
      { ...status, state: "paused", updated_at: 11, history_count: 1, history: [archived] },
      acceptance,
    ),
    "pending",
  );
  assert.equal(
    playAcceptanceState(
      { ...status, state: "playing", updated_at: 13, queue_count: 1, queue: [archived], history_count: 1, history: [archived] },
      acceptance,
    ),
    "pending",
  );
  assert.equal(
    playAcceptanceState(
      { ...status, state: "stopped", updated_at: 13, queue_count: 1, queue: [archived] },
      acceptance,
    ),
    "failed",
  );
  assert.equal(
    playAcceptanceState(
      { ...status, state: "playing", updated_at: 13, current: { ...archived, piece: 1, piece_count: 1, elapsed_seconds: 0 }, history_count: 1, history: [archived] },
      acceptance,
    ),
    "pending",
  );
  assert.equal(
    playAcceptanceState(
      { ...status, updated_at: 13, history_count: 1, history: [archived] },
      acceptance,
    ),
    "failed",
  );
  assert.equal(
    playAcceptanceState(
      {
        ...status,
        updated_at: 13,
        recent_starts: [
          { id: "009-af_heart-say", started_at: 13 },
          { id: archived.id, started_at: 12.5 },
        ],
        history_count: 1,
        history: [archived],
      },
      acceptance,
    ),
    "applied",
  );
});

test("makes active playback states impossible without active speech", () => {
  const waiting = {
    id: "002-af_heart-say",
    filename: "002-af_heart-say.txt",
    text: "Waiting",
    voice: "af_heart",
  };

  assert.deepEqual(
    playbackPresentation({ ...status, state: "idle" }, null),
    { state: "idle", item: null },
  );
  assert.deepEqual(
    playbackPresentation({ ...status, state: "paused" }, null),
    { state: "idle", item: null },
  );
  assert.deepEqual(
    playbackPresentation({ ...status, state: "playing" }, null),
    { state: "idle", item: null },
  );
  assert.deepEqual(
    playbackPresentation({
      ...status,
      state: "idle",
      queue_count: 1,
      queue: [waiting],
    }, null),
    { state: "playing", item: waiting },
  );
});

test("reflects pause commands immediately without creating an empty paused state", () => {
  const waiting = {
    id: "002-af_heart-say",
    filename: "002-af_heart-say.txt",
    text: "Waiting",
    voice: "af_heart",
  };
  const active = {
    ...status,
    state: "playing" as const,
    engine_running: true,
    installed: true,
    queue_count: 1,
    queue: [waiting],
  };

  assert.equal(statusAfterPauseCommand(active, true).state, "paused");
  assert.equal(statusAfterPauseCommand({ ...active, queue_count: 0, queue: [] }, true).state, "idle");
  assert.equal(statusAfterPauseCommand({ ...active, state: "loading" }, true).state, "loading");
  assert.equal(statusAfterPauseCommand({ ...active, engine_running: false }, true).state, "stopped");
  assert.equal(
    statusAfterPauseCommand({ ...active, state: "loading", engine_running: false }, true).state,
    "stopped",
  );
});

test("keeps an accepted selection paused until the user resumes it", () => {
  const selected = {
    id: "007-bm_fable-say",
    filename: "007-bm_fable-say.txt",
    text: "Selected",
    voice: "bm_fable",
  };

  assert.deepEqual(
    playbackPresentation({
      ...status,
      state: "paused",
      history_count: 1,
      history: [selected],
    }, { item: selected, state: "paused" }),
    { state: "paused", item: selected },
  );
});

test("a fresh external transport command updates an accepted selection", () => {
  const acceptance = { id: "007-bm_fable-say", acceptedAt: 12 };

  assert.equal(
    pendingPlaybackState({ ...status, state: "paused", updated_at: 11 }, acceptance, "playing"),
    "playing",
  );
  assert.equal(
    pendingPlaybackState({ ...status, state: "paused", updated_at: 13 }, acceptance, "playing"),
    "paused",
  );
  assert.equal(
    pendingPlaybackState({ ...status, state: "playing", updated_at: 14 }, acceptance, "paused"),
    "playing",
  );
});

test("an explicit selection starts playing even from a stale paused snapshot", () => {
  const selected = {
    id: "007-bm_fable-say",
    filename: "007-bm_fable-say.txt",
    text: "Selected",
    voice: "bm_fable",
  };

  assert.deepEqual(
    playbackPresentation({ ...status, state: "paused" }, { item: selected, state: "playing" }),
    { state: "playing", item: selected },
  );
});

test("presents a selection immediately while a stopped engine restarts", () => {
  const selected = {
    id: "007-bm_fable-say",
    filename: "007-bm_fable-say.txt",
    text: "Selected",
    voice: "bm_fable",
  };
  assert.deepEqual(
    playbackPresentation(status, { item: selected, state: "playing" }),
    { state: "playing", item: selected },
  );
});

test("keeps newest waiting speech above current speech and history", () => {
  const current = {
    id: "002-af_heart-say",
    filename: "002-af_heart-say.txt",
    text: "Now",
    voice: "af_heart",
    piece: 1,
    piece_count: 1,
    elapsed_seconds: 0,
  };
  const next = { ...current, id: "003-af_heart-say", text: "Next" };
  const newest = { ...current, id: "004-af_heart-say", text: "Newest" };
  const earlier = { ...current, id: "001-af_heart-say", text: "Earlier" };

  assert.deepEqual(
    timelineItems({ current, queue: [next, newest], history: [earlier] }),
    [
      {
        id: newest.id,
        filename: newest.filename,
        text: newest.text,
        voice: newest.voice,
        kind: "upcoming",
        position: 2,
      },
      {
        id: next.id,
        filename: next.filename,
        text: next.text,
        voice: next.voice,
        kind: "upcoming",
        position: 1,
      },
      {
        id: current.id,
        filename: current.filename,
        text: current.text,
        voice: current.voice,
        kind: "current",
        position: null,
      },
      {
        id: earlier.id,
        filename: earlier.filename,
        text: earlier.text,
        voice: earlier.voice,
        kind: "history",
        position: null,
      },
    ],
  );
});

test("shows an archived replay only in its active timeline position", () => {
  const replay = {
    id: "007-bm_fable-say",
    filename: "007-bm_fable-say.txt",
    text: "Replay me",
    voice: "bm_fable",
    piece: 1,
    piece_count: 1,
    elapsed_seconds: 0,
  };

  assert.deepEqual(
    timelineItems({ current: replay, queue: [], history: [replay] }).map(
      ({ id, kind }) => ({ id, kind }),
    ),
    [{ id: replay.id, kind: "current" }],
  );
});

test("keeps row order stable when current speech enters history", () => {
  const current = {
    id: "002-af_heart-say",
    filename: "002-af_heart-say.txt",
    text: "Now",
    voice: "af_heart",
    piece: 1,
    piece_count: 1,
    elapsed_seconds: 0,
  };
  const next = { ...current, id: "003-af_heart-say", text: "Next" };
  const newest = { ...current, id: "004-af_heart-say", text: "Newest" };
  const earlier = { ...current, id: "001-af_heart-say", text: "Earlier" };
  const before = timelineItems({ current, queue: [next, newest], history: [earlier] });
  const after = timelineItems({
    current: null,
    queue: [next, newest],
    history: [current, earlier],
  });

  assert.deepEqual(
    before.map(({ id }) => id),
    after.map(({ id }) => id),
  );
});

test("moves a queue item before a stable ID or to the end", () => {
  const items = [{ id: "one" }, { id: "two" }, { id: "three" }];

  assert.deepEqual(
    moveQueueItemBefore(items, "three", "one").map(({ id }) => id),
    ["three", "one", "two"],
  );
  assert.deepEqual(
    moveQueueItemBefore(items, "one", null).map(({ id }) => id),
    ["two", "three", "one"],
  );
  assert.deepEqual(
    moveQueueItemBefore(items, "two", "missing").map(({ id }) => id),
    ["one", "two", "three"],
  );
});
