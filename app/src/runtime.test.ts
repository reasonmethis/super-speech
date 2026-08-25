import assert from "node:assert/strict";
import test from "node:test";
import {
  statusForEngineProcess,
  timelineItems,
  type EngineStatus,
} from "./runtime.ts";

const status: EngineStatus = {
  version: 1,
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
    timelineItems({ current, queue: [upcoming], history: [earlier] }).map(
      ({ id, kind, position }) => ({ id, kind, position }),
    ),
    [
      { id: current.id, kind: "current", position: null },
      { id: upcoming.id, kind: "upcoming", position: 1 },
      { id: earlier.id, kind: "history", position: null },
    ],
  );
});
