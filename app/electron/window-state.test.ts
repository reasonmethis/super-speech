import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import {
  parseSavedWindowState,
  readSavedWindowState,
  restoredWindowBounds,
  writeSavedWindowState,
  type SavedWindowState,
} from "./window-state.ts";

const primary = { x: 0, y: 0, width: 1920, height: 1040 };
const secondary = { x: -1280, y: 0, width: 1280, height: 984 };

test("parses only complete window state", () => {
  const state = {
    bounds: { x: -100, y: 20, width: 600, height: 700 },
    maximized: true,
  };
  assert.deepEqual(parseSavedWindowState(state), state);
  assert.equal(parseSavedWindowState({ ...state, maximized: "yes" }), null);
  assert.equal(parseSavedWindowState({ ...state, bounds: { ...state.bounds, width: 0 } }), null);
  assert.equal(parseSavedWindowState({ ...state, bounds: { ...state.bounds, x: 1.5 } }), null);
});

test("preserves visible bounds and fits partially off-screen bounds", () => {
  const visible: SavedWindowState = {
    bounds: { x: -1100, y: 80, width: 700, height: 760 },
    maximized: false,
  };
  assert.deepEqual(restoredWindowBounds(visible, [primary, secondary]), visible.bounds);

  const partlyOffScreen: SavedWindowState = {
    bounds: { x: 1800, y: 900, width: 500, height: 700 },
    maximized: false,
  };
  assert.deepEqual(
    restoredWindowBounds(partlyOffScreen, [primary, secondary]),
    { x: 1420, y: 340, width: 500, height: 700 },
  );
});

test("recenters saved bounds when their monitor is gone", () => {
  const disconnected: SavedWindowState = {
    bounds: { x: 2300, y: 100, width: 600, height: 720 },
    maximized: true,
  };
  assert.deepEqual(
    restoredWindowBounds(disconnected, [primary]),
    { x: 660, y: 160, width: 600, height: 720 },
  );
});

test("centers defaults and fits them to a small work area", () => {
  assert.deepEqual(
    restoredWindowBounds(null, [{ x: 10, y: 20, width: 360, height: 580 }]),
    { x: 10, y: 20, width: 360, height: 580 },
  );
});

test("writes and reads state from one atomic JSON file", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "super-speech-window-state-"));
  const filePath = path.join(directory, "window-state.json");
  const state: SavedWindowState = {
    bounds: { x: 120, y: 90, width: 540, height: 760 },
    maximized: false,
  };
  try {
    await writeSavedWindowState(filePath, state);
    assert.deepEqual(readSavedWindowState(filePath), state);
    assert.deepEqual(JSON.parse(await readFile(filePath, "utf8")), state);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
