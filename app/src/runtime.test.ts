import assert from "node:assert/strict";
import test from "node:test";
import {
  statusForEngineProcess,
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
