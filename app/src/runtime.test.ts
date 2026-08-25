import assert from "node:assert/strict";
import test from "node:test";
import {
  parseEngineStatus,
  statusForEngineProcess,
  timelineItems,
  type EngineStatus,
} from "./runtime.ts";

const status: EngineStatus = {
  version: 2,
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

test("accepts a complete version 2 status", () => {
  assert.equal(parseEngineStatus(status), status);
});

test("upgrades version 1 status during an app-engine rolling upgrade", () => {
  const { history: _history, history_count: _historyCount, ...versionTwo } = status;
  const versionOne = { ...versionTwo, version: 1 };

  assert.deepEqual(parseEngineStatus(versionOne), {
    ...versionOne,
    version: 2,
    history_count: 0,
    history: [],
  });
});

test("rejects a partial version 2 status instead of inventing missing fields", () => {
  const { history: _history, ...partial } = status;
  assert.equal(parseEngineStatus(partial), null);
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
