import assert from "node:assert/strict";
import test from "node:test";
import {
  ENGINE_STATUS_VERSION,
  adoptTimelineSnapshot,
  compatibleEngineIsRunning,
  currentPieceSegments,
  engineProcessIsLive,
  isSpeechicleId,
  moveQueueItemBefore,
  playbackPresentation,
  parseEngineStatus,
  parseEngineProcessStatus,
  parseTimelineMutation,
  parseTimelineMutationResult,
  runtimeStatusForMutationSnapshot,
  runtimeStateForSnapshot,
  statusAfterTransientRead,
  statusForEngineProcess,
  statusAfterPauseCommand,
  timelineItems,
  type EngineStatus,
  type RuntimeStatus,
} from "./runtime.ts";

function speechicleId(value: number): string {
  return `sp_${value.toString(16).padStart(32, "0")}`;
}

const status: EngineStatus = {
  version: ENGINE_STATUS_VERSION,
  timeline_revision: 3,
  state: "stopped",
  updated_at: 1,
  engine_pid: 41,
  current: null,
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
  assert.equal(ENGINE_STATUS_VERSION, 12);
  assert.equal(parseEngineStatus(status), status);
});

test("requires a nonnegative integer timeline revision", () => {
  assert.equal(parseEngineStatus({ ...status, timeline_revision: -1 }), null);
  assert.equal(parseEngineStatus({ ...status, timeline_revision: 1.5 }), null);
  const { timeline_revision: _revision, ...missingRevision } = status;
  assert.equal(parseEngineStatus(missingRevision), null);
});

test("accepts only stable public Speechicle IDs", () => {
  assert.equal(isSpeechicleId(speechicleId(1)), true);
  assert.equal(isSpeechicleId("sp_0123456789abcdef0123456789abcdef"), true);
  assert.equal(isSpeechicleId("sp_0123456789ABCDEF0123456789ABCDEF"), false);
  assert.equal(isSpeechicleId("001-af_heart-say"), false);
  assert.equal(isSpeechicleId("sp_1234"), false);
});

test("rejects invalid IDs and internal filenames in public status rows", () => {
  const valid = {
    id: speechicleId(1),
    text: "Earlier",
    voice: "af_heart",
  };
  assert.equal(
    parseEngineStatus({ ...status, history_count: 1, history: [{ ...valid, id: "old-name" }] }),
    null,
  );
  assert.equal(
    parseEngineStatus({
      ...status,
      history_count: 1,
      history: [{ ...valid, filename: "001-af_heart-say.txt" }],
    }),
    null,
  );
});

test("keeps the last status through a transient read failure", () => {
  assert.equal(statusAfterTransientRead(null, status), status);
  assert.equal(statusAfterTransientRead(null, null), null);
  assert.equal(statusAfterTransientRead(status, null), status);
  assert.equal(
    statusAfterTransientRead({ ...status, version: ENGINE_STATUS_VERSION - 1 }, status),
    null,
  );
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

test("rejects waiting speech without a playback-boundary item", () => {
  const waiting = {
    id: speechicleId(2),
    text: "Waiting",
    voice: "af_heart",
  };
  assert.equal(
    parseEngineStatus({ ...status, state: "playing", queue_count: 1, queue: [waiting] }),
    null,
  );
});

test("rejects playback states that contradict the boundary", () => {
  const current = {
    id: speechicleId(1),
    text: "Current",
    voice: "af_heart",
    piece: 0,
    piece_count: 1,
    piece_start: null,
    piece_end: null,
    elapsed_seconds: 0,
  };
  assert.equal(parseEngineStatus({ ...status, state: "playing" }), null);
  assert.equal(parseEngineStatus({ ...status, state: "paused" }), null);
  assert.equal(parseEngineStatus({ ...status, state: "idle", current }), null);
});

test("rejects duplicate active rows", () => {
  const current = {
    id: speechicleId(1),
    text: "Current",
    voice: "af_heart",
    piece: 0,
    piece_count: 1,
    piece_start: null,
    piece_end: null,
    elapsed_seconds: 0,
  };
  assert.equal(
    parseEngineStatus({
      ...status,
      state: "playing",
      current,
      queue_count: 1,
      queue: [current],
    }),
    null,
  );
});

test("rejects duplicate IDs across active speech and History", () => {
  const current = {
    id: speechicleId(1),
    text: "Current",
    voice: "af_heart",
    piece: 0,
    piece_count: 1,
    piece_start: null,
    piece_end: null,
    elapsed_seconds: 0,
  };
  const waiting = {
    id: speechicleId(2),
    text: "Waiting",
    voice: "af_heart",
  };
  for (const duplicate of [current, waiting]) {
    assert.equal(
      parseEngineStatus({
        ...status,
        state: "playing",
        current,
        queue_count: 1,
        queue: [waiting],
        history_count: 1,
        history: [duplicate],
      }),
      null,
    );
  }
});

test("rejects impossible piece ranges and History totals", () => {
  const current = {
    id: speechicleId(1),
    text: "Hello world",
    voice: "af_heart",
    piece: 1,
    piece_count: 1,
    piece_start: 0,
    piece_end: 5,
    elapsed_seconds: 0,
  };
  const valid = { ...status, state: "playing", current };
  assert.notEqual(parseEngineStatus(valid), null);
  assert.equal(
    parseEngineStatus({ ...valid, current: { ...current, piece_start: null } }),
    null,
  );
  assert.equal(
    parseEngineStatus({ ...valid, current: { ...current, piece_end: 99 } }),
    null,
  );
  assert.equal(parseEngineStatus({ ...status, history_count: -1 }), null);
  assert.equal(parseEngineStatus({ ...status, history_count: 0.5 }), null);
  const history = [{ id: speechicleId(2), text: "Earlier", voice: "af_heart" }];
  assert.equal(parseEngineStatus({ ...status, history_count: 0, history }), null);
  assert.equal(
    parseEngineStatus({ ...status, history_count: 2, history: [history[0], history[0]] }),
    null,
  );
});

test("extracts the current Unicode piece with code-point offsets", () => {
  const item = {
    id: speechicleId(1),
    text: "😀 First. Second.",
    voice: "af_heart",
    piece: 2,
    piece_count: 2,
    piece_start: 9,
    piece_end: 16,
    elapsed_seconds: 0,
  };
  assert.deepEqual(currentPieceSegments(item), {
    before: "😀 First. ",
    current: "Second.",
    after: "",
  });
});

test("makes active playback states impossible without active speech", () => {
  const waiting = {
    id: speechicleId(2),
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
    { state: "idle", item: null },
  );
});

test("reflects pause commands immediately without creating an empty paused state", () => {
  const waiting = {
    id: speechicleId(2),
    text: "Waiting",
    voice: "af_heart",
  };
  const active = {
    ...status,
    state: "playing" as const,
    engine_running: true,
    installed: true,
    current: {
      ...waiting,
      id: speechicleId(1),
      text: "Current",
      piece: 0,
      piece_count: 1,
      piece_start: null,
      piece_end: null,
      elapsed_seconds: 0,
    },
    queue_count: 1,
    queue: [waiting],
  };

  assert.equal(statusAfterPauseCommand(active, true).state, "paused");
  assert.equal(statusAfterPauseCommand({ ...active, current: null, queue_count: 0, queue: [] }, true).state, "idle");
  assert.equal(statusAfterPauseCommand({ ...active, state: "loading" }, true).state, "loading");
  const stopped = statusAfterPauseCommand({ ...active, engine_running: false }, true);
  assert.equal(stopped.state, "stopped");
  assert.equal(stopped.current?.id, active.current.id);
  assert.equal(
    statusAfterPauseCommand({ ...active, state: "loading", engine_running: false }, true).state,
    "stopped",
  );
});

test("an explicit selection starts playing even from a stale paused snapshot", () => {
  const selected = {
    id: speechicleId(7),
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
    id: speechicleId(7),
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
    id: speechicleId(2),
    text: "Now",
    voice: "af_heart",
    piece: 1,
    piece_count: 1,
    piece_start: 0,
    piece_end: 3,
    elapsed_seconds: 0,
  };
  const next = { ...current, id: speechicleId(3), text: "Next" };
  const newest = { ...current, id: speechicleId(4), text: "Newest" };
  const earlier = { ...current, id: speechicleId(1), text: "Earlier" };

  assert.deepEqual(
    timelineItems({ current, queue: [next, newest], history: [earlier] }),
    [
      {
        id: newest.id,
        text: newest.text,
        voice: newest.voice,
        kind: "upcoming",
        position: 2,
      },
      {
        id: next.id,
        text: next.text,
        voice: next.voice,
        kind: "upcoming",
        position: 1,
      },
      {
        id: current.id,
        text: current.text,
        voice: current.voice,
        kind: "current",
        position: null,
      },
      {
        id: earlier.id,
        text: earlier.text,
        voice: earlier.voice,
        kind: "history",
        position: null,
      },
    ],
  );
});

test("keeps row order stable when current speech enters history", () => {
  const current = {
    id: speechicleId(2),
    text: "Now",
    voice: "af_heart",
    piece: 1,
    piece_count: 1,
    piece_start: 0,
    piece_end: 3,
    elapsed_seconds: 0,
  };
  const next = { ...current, id: speechicleId(3), text: "Next" };
  const newest = { ...current, id: speechicleId(4), text: "Newest" };
  const earlier = { ...current, id: speechicleId(1), text: "Earlier" };
  const before = timelineItems({ current, queue: [next, newest], history: [earlier] });
  const after = timelineItems({
    current: next,
    queue: [newest],
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

test("validates every timeline mutation variant", () => {
  const id = speechicleId(8);
  const beforeId = speechicleId(9);
  const mutations = [
    { type: "play", id },
    { type: "play", id, voice: "bm_fable" },
    { type: "move", section: "waiting", id, beforeId },
    { type: "move", section: "history", id, beforeId: null },
    { type: "archive", id },
    { type: "delete", id },
    { type: "clear" },
  ];
  for (const mutation of mutations) {
    assert.deepEqual(parseTimelineMutation(mutation), mutation);
  }

  assert.equal(parseTimelineMutation({ type: "play", id: "old-name" }), null);
  assert.equal(parseTimelineMutation({ type: "play", id, voice: "" }), null);
  assert.equal(parseTimelineMutation({ type: "play", id, voice: "Heart" }), null);
  assert.equal(parseTimelineMutation({ type: "clear", id }), null);
  assert.equal(parseTimelineMutation({ type: "archive", id, beforeId: null }), null);
  assert.equal(
    parseTimelineMutation({ type: "move", section: "current", id, beforeId: null }),
    null,
  );
  assert.equal(
    parseTimelineMutation({ type: "move", section: "waiting", id }),
    null,
  );
  assert.equal(parseTimelineMutation({ type: "unknown", id }), null);
});

test("normalizes committed and failed mutation results", () => {
  const id = speechicleId(8);
  const request1 = "1".repeat(24);
  const request2 = "2".repeat(24);
  assert.deepEqual(
    parseTimelineMutationResult({
      outcome: "committed",
      request_id: request1,
      result_id: id,
      snapshot: status,
    }),
    {
      outcome: "committed",
      requestId: request1,
      resultId: id,
      snapshot: status,
    },
  );
  for (const outcome of ["rejected", "unconfirmed"] as const) {
    assert.deepEqual(
      parseTimelineMutationResult({
        outcome,
        request_id: request2,
        error: "Could not commit",
        snapshot: status,
      }),
      {
        outcome,
        requestId: request2,
        error: "Could not commit",
        snapshot: status,
      },
    );
  }
  assert.equal(
    parseTimelineMutationResult({
      outcome: "committed",
      request_id: "3".repeat(24),
      result_id: "old-name",
      snapshot: status,
    }),
    null,
  );
  assert.equal(
    parseTimelineMutationResult({
      outcome: "committed",
      request_id: "4".repeat(24),
      snapshot: status,
      extra: true,
    }),
    null,
  );
  assert.equal(
    parseTimelineMutationResult({
      outcome: "rejected",
      request_id: "5".repeat(24),
      snapshot: status,
    }),
    null,
  );
  assert.equal(
    parseTimelineMutationResult({
      outcome: "committed",
      request_id: "6".repeat(24),
      error: "Contradictory",
      snapshot: status,
    }),
    null,
  );
  assert.equal(
    parseTimelineMutationResult({
      outcome: "unconfirmed",
      request_id: "7".repeat(24),
      result_id: id,
      error: "Contradictory",
      snapshot: status,
    }),
    null,
  );
  assert.equal(
    parseTimelineMutationResult({
      outcome: "committed",
      request_id: "8".repeat(24),
      snapshot: { ...status, version: ENGINE_STATUS_VERSION - 1 },
    }),
    null,
  );
  assert.equal(
    parseTimelineMutationResult({
      outcome: "committed",
      request_id: "not-a-request-id",
      snapshot: status,
    }),
    null,
  );
});

test("adopts timeline snapshots by revision and then publication time", () => {
  const current: RuntimeStatus = {
    ...status,
    timeline_revision: 5,
    updated_at: 20,
    state: "idle",
    engine_running: true,
    installed: true,
  };
  const olderRevision = {
    ...current,
    timeline_revision: 4,
    updated_at: 100,
  };
  const olderPublication = { ...current, updated_at: 19 };
  const newerPublication = { ...current, updated_at: 21 };
  const newerRevision = {
    ...current,
    timeline_revision: 6,
    updated_at: 1,
  };

  assert.equal(adoptTimelineSnapshot(current, olderRevision), current);
  assert.equal(adoptTimelineSnapshot(current, olderPublication), current);
  assert.equal(adoptTimelineSnapshot(current, newerPublication), newerPublication);
  assert.equal(adoptTimelineSnapshot(current, newerRevision), newerRevision);
});

test("a stale poll cannot roll back a committed snapshot", () => {
  const current: RuntimeStatus = {
    ...status,
    timeline_revision: 8,
    updated_at: 80,
    state: "idle",
    engine_running: true,
    installed: true,
  };
  const stalePoll = {
    ...current,
    timeline_revision: 7,
    updated_at: 90,
    history_count: 1,
    history: [{ id: speechicleId(4), text: "Old", voice: "af_heart" }],
  };
  assert.equal(adoptTimelineSnapshot(current, stalePoll), current);

  const stoppedPoll = {
    ...stalePoll,
    state: "stopped" as const,
    engine_pid: null,
    engine_running: false,
  };
  assert.deepEqual(adoptTimelineSnapshot(current, stoppedPoll), {
    ...current,
    state: "stopped",
    engine_pid: null,
    engine_running: false,
  });
});

test("a new engine process starts a fresh timeline revision sequence", () => {
  const oldProcess: RuntimeStatus = {
    ...status,
    timeline_revision: 80,
    updated_at: 80,
    engine_pid: 100,
    engine_running: true,
    installed: true,
  };
  const newProcess: RuntimeStatus = {
    ...oldProcess,
    timeline_revision: 0,
    updated_at: 1,
    engine_pid: 200,
  };

  assert.equal(adoptTimelineSnapshot(oldProcess, newProcess), newProcess);
});

test("a live process is adopted after the stopped state cleared the old PID", () => {
  const oldProcess: RuntimeStatus = {
    ...status,
    timeline_revision: 80,
    updated_at: 80,
    engine_pid: 100,
    engine_running: true,
    installed: true,
  };
  const stopped = adoptTimelineSnapshot(oldProcess, {
    ...oldProcess,
    timeline_revision: 79,
    updated_at: 81,
    state: "stopped",
    engine_pid: null,
    engine_running: false,
  });
  const newProcess: RuntimeStatus = {
    ...status,
    timeline_revision: 0,
    updated_at: 1,
    engine_pid: 200,
    engine_running: true,
    installed: true,
  };

  assert.equal(stopped.engine_pid, null);
  assert.equal(adoptTimelineSnapshot(stopped, newProcess), newProcess);
});

test("mutation snapshots must belong to the live engine process", () => {
  const runtime: RuntimeStatus = {
    ...status,
    timeline_revision: 2,
    updated_at: 20,
    engine_pid: 200,
    engine_running: true,
    installed: true,
  };
  const newerSnapshot = {
    ...status,
    timeline_revision: 3,
    updated_at: 21,
    engine_pid: 200,
  };
  const lateSnapshot = {
    ...newerSnapshot,
    timeline_revision: 80,
    updated_at: 80,
    engine_pid: 100,
  };

  assert.deepEqual(runtimeStatusForMutationSnapshot(newerSnapshot, runtime), {
    ...newerSnapshot,
    engine_running: true,
    installed: true,
  });
  assert.equal(runtimeStatusForMutationSnapshot(lateSnapshot, runtime), runtime);
  const stopped = { ...runtime, state: "stopped" as const, engine_pid: null, engine_running: false };
  assert.equal(runtimeStatusForMutationSnapshot(newerSnapshot, stopped), stopped);
});

test("mutation snapshots cannot regress the live process timeline", () => {
  const runtime: RuntimeStatus = {
    ...status,
    timeline_revision: 5,
    updated_at: 20,
    engine_pid: 200,
    engine_running: true,
    installed: true,
  };
  const olderRevision = {
    ...status,
    timeline_revision: 4,
    updated_at: 100,
    engine_pid: 200,
  };
  const olderPublication = {
    ...status,
    timeline_revision: 5,
    updated_at: 19,
    engine_pid: 200,
  };
  const newerSnapshot = {
    ...status,
    timeline_revision: 6,
    updated_at: 1,
    engine_pid: 200,
  };

  assert.equal(runtimeStatusForMutationSnapshot(olderRevision, runtime), runtime);
  assert.equal(runtimeStatusForMutationSnapshot(olderPublication, runtime), runtime);
  assert.deepEqual(runtimeStatusForMutationSnapshot(newerSnapshot, runtime), {
    ...newerSnapshot,
    engine_running: true,
    installed: true,
  });
});
