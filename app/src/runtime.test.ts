import assert from "node:assert/strict";
import test from "node:test";
import {
  ENGINE_STATUS_VERSION,
  activeTimelineIds,
  clearRequestWasApplied,
  compatibleEngineIsRunning,
  currentPieceSegments,
  engineProcessIsLive,
  moveQueueItemBefore,
  pendingPlaybackState,
  playAcceptanceState,
  playbackPresentation,
  parseEngineStatus,
  parseEngineProcessStatus,
  parsePlayAcceptance,
  runtimeStateForSnapshot,
  statusAfterTransientRead,
  statusForEngineProcess,
  statusAfterPauseCommand,
  timelineItems,
  timelineItemsAtBoundary,
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
    id: "002-af_heart-say",
    filename: "002-af_heart-say.txt",
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
    id: "001-af_heart-say",
    filename: "001-af_heart-say.txt",
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
    id: "001-af_heart-say",
    filename: "001-af_heart-say.txt",
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
    id: "001-af_heart-say",
    filename: "001-af_heart-say.txt",
    text: "Current",
    voice: "af_heart",
    piece: 0,
    piece_count: 1,
    piece_start: null,
    piece_end: null,
    elapsed_seconds: 0,
  };
  const waiting = {
    id: "002-af_heart-say",
    filename: "002-af_heart-say.txt",
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
    id: "001-af_heart-say",
    filename: "001-af_heart-say.txt",
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
  const history = [{ id: "h", filename: "h.txt", text: "Earlier", voice: "af_heart" }];
  assert.equal(parseEngineStatus({ ...status, history_count: 0, history }), null);
  assert.equal(
    parseEngineStatus({ ...status, history_count: 2, history: [history[0], history[0]] }),
    null,
  );
});

test("extracts the current Unicode piece with code-point offsets", () => {
  const item = {
    id: "001-af_heart-say",
    filename: "001-af_heart-say.txt",
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

test("active timeline IDs include Current and Waiting", () => {
  const waiting = { id: "w", filename: "w.txt", text: "Wait", voice: "af_heart" };
  const current = {
    id: "c",
    filename: "c.txt",
    text: "Current",
    voice: "af_heart",
    piece: 0,
    piece_count: 1,
    piece_start: null,
    piece_end: null,
    elapsed_seconds: 0,
  };
  assert.deepEqual([...activeTimelineIds({ current, queue: [waiting] })], ["c", "w"]);
  assert.deepEqual([...activeTimelineIds({ current: null, queue: [] })], []);
});

test("clear confirmation requires a newer status without the baseline IDs", () => {
  const baseline = new Set(["selected"]);
  const selected = {
    id: "selected",
    filename: "selected-af_heart-say.txt",
    text: "Selected",
    voice: "af_heart",
    piece: 0,
    piece_count: 1,
    piece_start: null,
    piece_end: null,
    elapsed_seconds: 0,
  };

  assert.equal(
    clearRequestWasApplied(
      { ...status, updated_at: 10, current: null, queue: [] },
      baseline,
      10,
    ),
    false,
  );
  assert.equal(
    clearRequestWasApplied(
      { ...status, updated_at: 10.5, current: null, queue: [] },
      baseline,
      11,
    ),
    false,
  );
  assert.equal(
    clearRequestWasApplied(
      { ...status, updated_at: 11, current: selected, queue: [] },
      baseline,
      10,
    ),
    false,
  );
  assert.equal(
    clearRequestWasApplied(
      { ...status, updated_at: 12, current: null, queue: [] },
      baseline,
      10,
    ),
    true,
  );
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
  const preparing = {
    ...archived,
    piece: 0,
    piece_count: 1,
    piece_start: null,
    piece_end: null,
    elapsed_seconds: 0,
  };
  assert.equal(
    playAcceptanceState(
      { ...status, state: "stopped", updated_at: 13, current: preparing },
      acceptance,
    ),
    "failed",
  );
  assert.equal(
    playAcceptanceState(
      { ...status, state: "playing", updated_at: 13, current: { ...archived, piece: 1, piece_count: 1, piece_start: 0, piece_end: archived.text.length, elapsed_seconds: 0 }, history_count: 1, history: [archived] },
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
    { state: "idle", item: null },
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
    current: {
      ...waiting,
      id: "001-af_heart-say",
      filename: "001-af_heart-say.txt",
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
    piece_start: 0,
    piece_end: 3,
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

test("keeps row order stable when current speech enters history", () => {
  const current = {
    id: "002-af_heart-say",
    filename: "002-af_heart-say.txt",
    text: "Now",
    voice: "af_heart",
    piece: 1,
    piece_count: 1,
    piece_start: 0,
    piece_end: 3,
    elapsed_seconds: 0,
  };
  const next = { ...current, id: "003-af_heart-say", text: "Next" };
  const newest = { ...current, id: "004-af_heart-say", text: "Newest" };
  const earlier = { ...current, id: "001-af_heart-say", text: "Earlier" };
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

test("projects a selected History row as the boundary without moving any card", () => {
  const history = ["005", "004", "003", "002", "001"].map((id) => ({
    id: `${id}-af_heart-say`,
    filename: `${id}-af_heart-say.txt`,
    text: id,
    voice: "af_heart",
  }));

  for (const selectedIndex of history.keys()) {
    const projected = timelineItemsAtBoundary(
      { current: null, queue: [], history },
      history[selectedIndex],
    );
    assert.deepEqual(
      projected.map(({ id }) => id),
      history.map(({ id }) => id),
    );
    assert.deepEqual(
      projected.map(({ kind }) => kind),
      history.map((_, index) =>
        index < selectedIndex
          ? "upcoming"
          : index === selectedIndex
            ? "current"
            : "history"
      ),
    );
  }
});

test("keeps a voice-changed selection in the source card's position", () => {
  const original = {
    id: "003-af_heart-say",
    filename: "003-af_heart-say.txt",
    text: "Same words",
    voice: "af_heart",
  };
  const changed = {
    ...original,
    id: "003-bm_fable-say",
    filename: "003-bm_fable-say.txt",
    voice: "bm_fable",
  };
  const projected = timelineItemsAtBoundary(
    {
      current: null,
      queue: [],
      history: [
        { ...original, id: "004-af_heart-say", filename: "004-af_heart-say.txt" },
        original,
        { ...original, id: "002-af_heart-say", filename: "002-af_heart-say.txt" },
      ],
    },
    changed,
    original.id,
  );

  assert.deepEqual(projected.map(({ id }) => id), [
    "004-af_heart-say",
    changed.id,
    "002-af_heart-say",
  ]);
  assert.equal(projected[1].kind, "current");
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
