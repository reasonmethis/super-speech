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

async function beginDrag(page, handle, deltaY = -65) {
  await waitFor(
    () => handle.isEnabled(),
    "Drag handle did not become enabled",
  );
  const bounds = await handle.boundingBox();
  assert(bounds, "Drag handle must be visible");
  const from = {
    x: bounds.x + bounds.width / 2,
    y: bounds.y + bounds.height / 2,
  };
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(from.x, from.y + deltaY, { steps: 8 });
  await waitFor(
    async () => await page.locator(".queue-drag-ghost").count() === 1,
    "The drag preview did not activate",
  );
}

async function assertDragClean(page) {
  assert.equal(await page.locator(".queue-drag-ghost").count(), 0);
  assert.equal(await page.locator(".is-drag-source").count(), 0);
  assert.equal(await page.locator(".queue-drag-placeholder").count(), 0);
  const ids = await page.locator(".queue-item.is-upcoming").evaluateAll((items) =>
    items.map((item) => item.getAttribute("data-item-id"))
  );
  assert.equal(new Set(ids).size, ids.length, "Waiting cards must have unique IDs");
}

async function visibleHistoryDropPoint(page) {
  await page.locator("#queue-list").evaluate((list) => {
    list.scrollTop = list.scrollHeight;
  });
  const dividerBounds = await page.locator(
    ".timeline-divider.history-drop-target",
  ).boundingBox();
  const listBounds = await page.locator("#queue-list").boundingBox();
  assert(dividerBounds && listBounds, "History must be visible during a drag");
  const point = {
    x: dividerBounds.x + dividerBounds.width / 2,
    y: dividerBounds.y + dividerBounds.height / 2,
  };
  assert(point.y >= listBounds.y && point.y <= listBounds.y + listBounds.height);
  return point;
}

async function layoutBounds(locator) {
  return locator.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const transform = getComputedStyle(element).transform;
    const animatedOffsetY = transform === "none"
      ? 0
      : new DOMMatrixReadOnly(transform).m42;
    return {
      x: bounds.x,
      y: bounds.y - animatedOffsetY,
      width: bounds.width,
      height: bounds.height,
    };
  });
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
  await page.evaluate(() => {
    const animate = Element.prototype.animate;
    globalThis.__queueReorderAnimationRequests = 0;
    Element.prototype.animate = function (keyframes, options) {
      if (
        this instanceof HTMLElement &&
        this.classList.contains("queue-item") &&
        JSON.stringify(keyframes).includes("translateY")
      ) {
        globalThis.__queueReorderAnimationRequests += 1;
      }
      return animate.call(this, keyframes, options);
    };
  });

  const rows = page.locator(".queue-item.is-upcoming");
  const firstId = await rows.nth(0).getAttribute("data-item-id");
  const secondId = await rows.nth(1).getAttribute("data-item-id");
  assert(firstId && secondId, "Waiting items must expose stable IDs");
  const firstStart = await rows.nth(0).boundingBox();
  const secondStart = await rows.nth(1).boundingBox();
  assert(firstStart && secondStart, "Waiting items must be visible before dragging");
  await rows.nth(1).evaluate((row) => {
    row.dataset.smokeNode = "midpoint-source";
  });
  const sourceHandle = rows.nth(1).locator(".queue-drag-handle");
  const sourceHandleBounds = await sourceHandle.boundingBox();
  assert(sourceHandleBounds, "Drag handle must be visible");
  const grabPoint = {
    x: sourceHandleBounds.x + sourceHandleBounds.width / 2,
    y: sourceHandleBounds.y + 5,
  };
  const grabOffsetY = grabPoint.y - secondStart.y;
  const midpointPointerY = firstStart.y + firstStart.height / 2 +
    grabOffsetY - secondStart.height / 2;
  await page.mouse.move(grabPoint.x, grabPoint.y);
  await page.mouse.down();
  await page.mouse.move(grabPoint.x, midpointPointerY + 2);
  assert.equal(
    await rows.nth(0).getAttribute("data-item-id"),
    firstId,
    "The row switched before its center reached the destination midpoint",
  );
  await page.mouse.move(grabPoint.x, midpointPointerY);
  assert.equal(
    await rows.nth(0).getAttribute("data-item-id"),
    secondId,
    "The row did not switch when its center reached the destination midpoint",
  );
  const dragged = await page.locator(".queue-drag-ghost").boundingBox();
  assert(dragged, "The floating drag preview must stay visible");
  assert(
    Math.abs(dragged.y + dragged.height / 2 - (firstStart.y + firstStart.height / 2)) < 1,
  );
  assert(
    await page.evaluate(() => globalThis.__queueReorderAnimationRequests) > 0,
    "Queue neighbors must animate into their preview positions",
  );
  await page.mouse.up();
  assert.equal(
    await page.locator(`[data-item-id="${secondId}"]`).getAttribute("data-smoke-node"),
    "midpoint-source",
    "Releasing a reorder must preserve the animating row nodes",
  );
  const reorderedReferences = await page.locator(`[data-item-id="${secondId}"]`).evaluate(
    (row) => [
      row.querySelector(".queue-drag-handle")?.getAttribute("aria-label"),
      row.querySelector(".queue-remove")?.getAttribute("aria-label"),
      row.querySelector(".queue-disclosure")?.getAttribute("aria-label"),
      row.querySelector(".queue-full-text")?.getAttribute("aria-label"),
    ],
  );
  assert(
    reorderedReferences.every((label) => label?.includes("waiting speech 2")),
    "Preserved row nodes must refresh their order-dependent accessible names",
  );
  await assertDragClean(page);
  await waitFor(
    async () => await rows.nth(0).getAttribute("data-item-id") === secondId,
    "A real mouse drag did not move the item into the first visual position",
  );
  await waitFor(
    () => status().queue.at(-1)?.id === secondId,
    "The engine did not persist the mouse-driven reorder",
  );
  await waitFor(
    async () => {
      const visualOrder = await rows.evaluateAll((items) =>
        items.map((item) => item.getAttribute("data-item-id"))
      );
      return visualOrder.join(",") === status().queue.map(({ id }) => id).reverse().join(",");
    },
    "The renderer did not reconcile to the persisted queue order",
  );
  assert.equal(
    await page.locator(`[data-item-id="${secondId}"]`).getAttribute("data-smoke-node"),
    "midpoint-source",
    "Engine reconciliation must preserve the settled row nodes",
  );

  await beginDrag(
    page,
    page.locator(`[data-item-id="${secondId}"] .queue-drag-handle`),
    70,
  );
  runEngine("speak", "Drag refresh cancellation");
  await waitFor(
    async () =>
      await page.locator(".queue-item.is-upcoming").count() === 3 &&
      await page.locator(".queue-drag-ghost").count() === 0,
    "A queue refresh did not cancel and clean the active drag",
  );
  await page.mouse.up();
  await assertDragClean(page);
  assert.equal(
    await page.locator(`[data-item-id="${secondId}"].is-upcoming`).count(),
    1,
    "A queue refresh lost the dragged card",
  );

  const archivedRow = page.locator(`[data-item-id="${secondId}"].is-upcoming`);
  const archivedBounds = await archivedRow.boundingBox();
  const archivedHandleBounds = await archivedRow.locator(".queue-drag-handle").boundingBox();
  const waitingCards = page.locator(".queue-item.is-upcoming:not(.queue-drag-ghost)");
  const lastWaitingBounds = await waitingCards.last().boundingBox();
  assert(
    archivedBounds && archivedHandleBounds && lastWaitingBounds,
    "Archive drag source and destinations must be visible",
  );
  const archiveGrab = {
    x: archivedHandleBounds.x + archivedHandleBounds.width / 2,
    y: archivedHandleBounds.y + archivedHandleBounds.height / 2,
  };
  const archiveOffsetY = archiveGrab.y - archivedBounds.y;
  const pointerYForDraggedCenter = (targetBounds, offset = 0) =>
    targetBounds.y + targetBounds.height / 2 + offset + archiveOffsetY -
    archivedBounds.height / 2;
  await page.mouse.move(archiveGrab.x, archiveGrab.y);
  await page.mouse.down();
  await page.mouse.move(archiveGrab.x, pointerYForDraggedCenter(lastWaitingBounds, 2));
  assert.equal(
    await waitingCards.last().getAttribute("data-item-id"),
    secondId,
    "The archive gesture must first preview a move to the last position",
  );
  const firstOtherBounds = await layoutBounds(page.locator(
    `.queue-item.is-upcoming:not(.queue-drag-ghost):not([data-item-id="${secondId}"])`,
  ).first());
  assert(firstOtherBounds, "The first neighboring card must be visible");
  await page.mouse.move(archiveGrab.x, pointerYForDraggedCenter(firstOtherBounds));
  assert.equal(
    await waitingCards.first().getAttribute("data-item-id"),
    secondId,
    "Crossing the same cards upward must retarget the preview",
  );
  const lastOtherBounds = await layoutBounds(page.locator(
    `.queue-item.is-upcoming:not(.queue-drag-ghost):not([data-item-id="${secondId}"])`,
  ).last());
  assert(lastOtherBounds, "The last neighboring card must be visible");
  await page.mouse.move(archiveGrab.x, pointerYForDraggedCenter(lastOtherBounds, 2));
  const previewOrder = await waitingCards.evaluateAll((items) =>
    items.map((item) => item.getAttribute("data-item-id"))
  );
  assert.equal(
    previewOrder.at(-1),
    secondId,
    "Crossing the same cards downward must retarget the preview again",
  );
  const animationCountBeforeHistory = await page.evaluate(
    () => globalThis.__queueReorderAnimationRequests,
  );
  const historyDropPoint = await visibleHistoryDropPoint(page);
  await page.mouse.move(historyDropPoint.x, historyDropPoint.y);
  assert.equal(
    await page.locator(".is-history-drop").count(),
    1,
    "The visible History target must acknowledge the drag",
  );
  assert.deepEqual(
    await waitingCards.evaluateAll((items) =>
      items.map((item) => item.getAttribute("data-item-id"))
    ),
    previewOrder,
    "Entering History must preserve the current queue preview",
  );
  assert.equal(
    await page.evaluate(() => globalThis.__queueReorderAnimationRequests),
    animationCountBeforeHistory,
    "Entering History must not start a backtracking queue animation",
  );
  await page.locator("#queue-list").evaluate((list) => {
    list.scrollTop = 0;
  });
  const exitTargetBounds = await layoutBounds(page.locator(
    `.queue-item.is-upcoming:not(.queue-drag-ghost):not([data-item-id="${secondId}"])`,
  ).first());
  assert(exitTargetBounds, "A queue target must remain visible after entering History");
  await page.mouse.move(archiveGrab.x, pointerYForDraggedCenter(exitTargetBounds));
  assert.equal(
    await page.locator(".is-history-drop").count(),
    0,
    "Leaving History must clear its drop highlight",
  );
  assert.equal(
    await waitingCards.first().getAttribute("data-item-id"),
    secondId,
    "Leaving History must resume queue previewing",
  );
  const exitPreviewOrder = await waitingCards.evaluateAll((items) =>
    items.map((item) => item.getAttribute("data-item-id"))
  );
  const animationCountBeforeFinalHistory = await page.evaluate(
    () => globalThis.__queueReorderAnimationRequests,
  );
  const finalHistoryPoint = await visibleHistoryDropPoint(page);
  await page.mouse.move(finalHistoryPoint.x, finalHistoryPoint.y);
  assert.equal(
    await page.locator(".is-history-drop").count(),
    1,
    "History must acknowledge a re-entered drag",
  );
  assert.deepEqual(
    await waitingCards.evaluateAll((items) =>
      items.map((item) => item.getAttribute("data-item-id"))
    ),
    exitPreviewOrder,
    "Re-entering History must preserve the latest queue preview",
  );
  assert.equal(
    await page.evaluate(() => globalThis.__queueReorderAnimationRequests),
    animationCountBeforeFinalHistory,
    "Re-entering History must not start a backtracking queue animation",
  );
  await page.mouse.up();
  await assertDragClean(page);
  await waitFor(
    async () => await page.locator(`[data-item-id="${secondId}"].is-history`).count() === 1,
    "A real mouse drag to History did not archive the waiting item",
  );
  await waitFor(
    () => !status().queue.some(({ id }) => id === secondId),
    "The engine did not persist the mouse-driven archive",
  );

  const remainingRows = page.locator(".queue-item.is-upcoming");
  await waitFor(
    async () => {
      const visualOrder = await remainingRows.evaluateAll((items) =>
        items.map((item) => item.getAttribute("data-item-id"))
      );
      return visualOrder.join(",") === status().queue.map(({ id }) => id).reverse().join(",");
    },
    "The renderer did not reconcile to the persisted archive",
  );

  const copyLefts = await page.locator(
    ".queue-item.is-current .queue-copy, .queue-item.is-upcoming .queue-copy, .queue-item.is-history .queue-copy",
  ).evaluateAll((copies) => copies.map((copy) => copy.getBoundingClientRect().left));
  assert(copyLefts.length >= 3, "Current, waiting, and History rows must all be visible");
  assert(
    Math.max(...copyLefts) - Math.min(...copyLefts) < 1,
    "Timeline copy must share one left edge across every row type",
  );
  assert.equal(
    await page.locator(".queue-order").count(),
    0,
    "Timeline labels must not shift row copy",
  );
  assert.equal(
    await page.locator(".timeline-divider.history-drop-target span").nth(1).textContent(),
    `${status().history_count.toLocaleString()} total`,
    "History must show its total count without implying every archived item is rendered",
  );
  await waitFor(
    async () => /App \d+\.\d+\.\d+ \| Engine \d+\.\d+\.\d+/.test(
      await page.locator("#version-label").textContent() ?? ""
    ),
    "The footer did not show app and engine versions",
  );
  if (process.env.SUPER_SPEECH_SCREENSHOT) {
    await page.screenshot({ path: process.env.SUPER_SPEECH_SCREENSHOT });
  }

  const playbackBeforeHistoryGesture = status();
  const historyPlay = page.locator(
    `[data-item-id="${secondId}"].is-history .queue-play`,
  );
  const historyBounds = await historyPlay.boundingBox();
  assert(historyBounds, "Archived card must be visible for the gesture test");
  const historyPoint = {
    x: historyBounds.x + historyBounds.width / 2,
    y: historyBounds.y + historyBounds.height / 2,
  };
  await page.mouse.move(historyPoint.x, historyPoint.y);
  await page.mouse.down();
  await page.mouse.move(historyPoint.x + 10, historyPoint.y, { steps: 4 });
  await page.mouse.up();
  await new Promise((resolve) => setTimeout(resolve, 1_000));
  const playbackAfterHistoryGesture = status();
  assert.equal(await page.locator(".queue-item.is-pending").count(), 0);
  assert.equal(
    playbackAfterHistoryGesture.current?.id,
    playbackBeforeHistoryGesture.current?.id,
    "Dragging an archived card started playback",
  );
  assert.deepEqual(
    playbackAfterHistoryGesture.queue.map(({ id }) => id),
    playbackBeforeHistoryGesture.queue.map(({ id }) => id),
    "Dragging an archived card changed the waiting queue",
  );
  assert.equal(
    playbackAfterHistoryGesture.history_count,
    playbackBeforeHistoryGesture.history_count,
    "Dragging an archived card changed History",
  );

  const orderBeforeBlur = await remainingRows.evaluateAll((items) =>
    items.map((item) => item.getAttribute("data-item-id"))
  );
  await beginDrag(page, remainingRows.first().locator(".queue-drag-handle"), 70);
  await page.evaluate(() => {
    window.dispatchEvent(new PointerEvent("pointermove", {
      pointerId: 1,
      isPrimary: true,
      buttons: 0,
    }));
  });
  await waitFor(
    async () => await page.locator(".queue-drag-ghost").count() === 0,
    "A pointer move without the primary button left drag artifacts behind",
  );
  await page.mouse.up();
  await assertDragClean(page);
  assert.deepEqual(
    await remainingRows.evaluateAll((items) =>
      items.map((item) => item.getAttribute("data-item-id"))
    ),
    orderBeforeBlur,
    "Losing the primary button changed the queue order",
  );

  await beginDrag(page, remainingRows.first().locator(".queue-drag-handle"), 70);
  await page.evaluate(() => window.dispatchEvent(new Event("blur")));
  await waitFor(
    async () => await page.locator(".queue-drag-ghost").count() === 0,
    "Window blur left drag artifacts behind",
  );
  await page.mouse.up();
  await assertDragClean(page);
  assert.deepEqual(
    await remainingRows.evaluateAll((items) =>
      items.map((item) => item.getAttribute("data-item-id"))
    ),
    orderBeforeBlur,
    "Window blur changed the queue order",
  );

  console.log("Super Speech pointer drag and cancellation smoke test passed");
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
