import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { _electron as electron } from "playwright-core";

const appDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const executableName = process.platform === "win32"
  ? "super-speech-engine.exe"
  : "super-speech-engine";
const electronName = process.platform === "win32" ? "electron.exe" : "electron";
const engine = path.join(appDirectory, "build-resources", "engine", executableName);
const electronExecutable = path.join(
  appDirectory,
  "node_modules",
  "electron",
  "dist",
  electronName,
);
const root = mkdtempSync(path.join(tmpdir(), "super-speech-drag-"));
const runtime = path.join(root, "runtime");
const profile = path.join(root, "profile");
const environment = {
  ...process.env,
  SUPER_SPEECH_ENGINE_PATH: engine,
  SUPER_SPEECH_HOME: runtime,
  SUPER_SPEECH_MODEL_DIR: path.join(appDirectory, "build-resources", "models", "kokoro"),
  SUPER_SPEECH_SILENT: "1",
  SUPER_SPEECH_SKIP_SKILL_INSTALL: "1",
};

let electronApp;
let enginePid;

function runEngine(...args) {
  return execFileSync(engine, args, {
    encoding: "utf8",
    env: environment,
    windowsHide: true,
  }).trim();
}

function status() {
  return JSON.parse(readFileSync(path.join(runtime, "status.json"), "utf8"));
}

function processExists(processId) {
  if (!processId) {
    return false;
  }
  try {
    process.kill(processId, 0);
    return true;
  } catch {
    return false;
  }
}

async function waitFor(predicate, message, timeout = 15_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(message);
}

async function drag(page, source, destination) {
  const sourceBounds = await source.boundingBox();
  const destinationBounds = await destination.boundingBox();
  assert(sourceBounds, "Drag source must be visible");
  assert(destinationBounds, "Drop target must be visible");
  const from = {
    x: sourceBounds.x + sourceBounds.width / 2,
    y: sourceBounds.y + sourceBounds.height / 2,
  };
  const to = {
    x: destinationBounds.x + destinationBounds.width / 2,
    y: destinationBounds.y + 2,
  };
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(to.x, to.y, { steps: 12 });
  await page.mouse.up();
}

try {
  runEngine("pause");
  runEngine("speak", "Drag test one");
  runEngine("speak", "Drag test two");
  runEngine("speak", "Drag test three");
  await waitFor(
    () => status().current && status().queue_count === 2,
    "The silent drag fixture did not reach the paused queue state",
    30_000,
  );
  enginePid = status().engine_pid;

  electronApp = await electron.launch({
    executablePath: electronExecutable,
    args: [appDirectory, "--hidden", `--user-data-dir=${profile}`],
    cwd: appDirectory,
    env: environment,
  });
  const page = await electronApp.firstWindow();
  await page.locator(".queue-item.is-upcoming").first().waitFor();
  await waitFor(
    async () => await page.locator(".queue-item.is-upcoming").count() === 2,
    "The Electron window did not render two waiting items",
  );

  const rows = page.locator(".queue-item.is-upcoming");
  const firstId = await rows.nth(0).getAttribute("data-item-id");
  const secondId = await rows.nth(1).getAttribute("data-item-id");
  assert(firstId && secondId, "Waiting items must expose stable IDs");

  await drag(page, rows.nth(1).locator(".queue-drag-handle"), rows.nth(0));
  await waitFor(
    async () => await rows.nth(0).getAttribute("data-item-id") === secondId,
    "A real mouse drag did not reorder the waiting items",
  );
  await waitFor(
    () => status().queue[0]?.id === secondId,
    "The engine did not persist the mouse-driven reorder",
  );

  await drag(
    page,
    page.locator(`[data-item-id="${secondId}"] .queue-drag-handle`),
    page.locator(".timeline-divider.history-drop-target"),
  );
  await waitFor(
    async () => await page.locator(`[data-item-id="${secondId}"].is-history`).count() === 1,
    "A real mouse drag to History did not archive the waiting item",
  );
  await waitFor(
    () => !status().queue.some(({ id }) => id === secondId),
    "The engine did not persist the mouse-driven archive",
  );

  console.log("Super Speech real mouse drag smoke test passed");
} finally {
  await electronApp?.close().catch(() => undefined);
  try {
    runEngine("interrupt");
  } catch {
    // The fixture engine may already be gone
  }
  await waitFor(
    () => !processExists(enginePid),
    "The silent drag fixture engine did not stop",
  ).catch((error) => console.warn(error.message));
  await new Promise((resolve) => setTimeout(resolve, 500));
  try {
    rmSync(root, { recursive: true, force: true, maxRetries: 50, retryDelay: 100 });
  } catch (error) {
    console.warn(`Could not remove silent drag fixture ${root}: ${error.message}`);
  }
}
