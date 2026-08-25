import assert from "node:assert/strict";
import test from "node:test";
import {
  ENGINE_STATUS_VERSION,
  parseEngineStatus,
  parseEngineProcessStatus,
  parsePlayAcceptance,
  statusForEngineProcess,
  timelineItems,
  type EngineStatus,
} from "./runtime.ts";

const status: EngineStatus = {
  version: ENGINE_STATUS_VERSION,
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

test("normalizes an engine play acknowledgement", () => {
  assert.deepEqual(parsePlayAcceptance({ id: "008-bm_fable-say", accepted_at: 12.5 }), {
    id: "008-bm_fable-say",
    acceptedAt: 12.5,
  });
  assert.equal(parsePlayAcceptance({ id: "008-bm_fable-say" }), null);
});

test("orders the timeline as current, upcoming, then history", () => {
  const current = {
    id: "002-af_heart-say",
    filename: "002-af_heart-say.txt",
    text: "Now",
    voice: "af_heart",
    piece: 1,
    piece_count: 1,
    elapsed_seconds: 0,
  };
  const upcoming = { ...current, id: "003-af_heart-say", text: "Next" };
  const earlier = { ...current, id: "001-af_heart-say", text: "Earlier" };

  assert.deepEqual(
    timelineItems({ current, queue: [upcoming], history: [earlier] }),
    [
      {
        id: current.id,
        filename: current.filename,
        text: current.text,
        voice: current.voice,
        kind: "current",
        position: null,
      },
      {
        id: upcoming.id,
        filename: upcoming.filename,
        text: upcoming.text,
        voice: upcoming.voice,
        kind: "upcoming",
        position: 1,
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
