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
  await handle.scrollIntoViewIfNeeded();
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

async function actionMenuIsFullyVisible(page) {
  return page.locator("#queue-action-menu:not([hidden])").evaluate((menu) => {
    const bounds = menu.getBoundingClientRect();
    const listBounds = document.querySelector("#queue-list").getBoundingClientRect();
    const center = document.elementFromPoint(
      bounds.left + bounds.width / 2,
      bounds.top + bounds.height / 2,
    );
    return bounds.left >= 0 &&
      bounds.top >= listBounds.top &&
      bounds.right <= window.innerWidth &&
      bounds.bottom <= listBounds.bottom &&
      center !== null &&
      menu.contains(center);
  });
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

async function playbackSnapshot(page) {
  return page.evaluate(() => {
    const bounds = (selector) => {
      const rect = document.querySelector(selector)?.getBoundingClientRect();
      if (!rect) {
        throw new Error(`Missing ${selector}`);
      }
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    return {
      state: document.body.dataset.state,
      accent: getComputedStyle(document.body).getPropertyValue("--accent").trim(),
      button: bounds("#playback-button"),
      ring: bounds(".ambient-ring"),
      icon: bounds("#playback-icon svg"),
      title: document.querySelector("#playback-title")?.textContent,
      text: document.querySelector("#current-text")?.textContent,
      voice: document.querySelector("#voice-label")?.textContent,
    };
  });
}

try {
  runEngine("pause");
  runEngine("speak", "Drag test one has enough words to keep silent playback active while the interface checks that playing and paused layouts stay perfectly aligned.");
  runEngine("speak", "Drag test two also has enough words to keep the silent replay observable during the double click interaction test.");
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
  await waitFor(
    async () => await page.locator("body").getAttribute("data-state") === "paused",
    "The Electron window did not render the paused fixture",
  );
  assert(
    await page.locator(".queue-item.is-current").evaluate((row) => {
      const rowBounds = row.getBoundingClientRect();
      const listBounds = document.querySelector("#queue-list").getBoundingClientRect();
      return rowBounds.top >= listBounds.top && rowBounds.bottom <= listBounds.bottom;
    }),
    "The app must reveal the current row when playback first appears",
  );
  assert.deepEqual(
    await page.locator(".timeline-divider").evaluateAll((dividers) =>
      dividers.map((divider) => ({
        section: divider.getAttribute("data-section"),
        title: divider.querySelector(".timeline-divider-title")?.textContent,
      }))
    ),
    [
      { section: "upcoming", title: "Waiting" },
      { section: "current", title: "Current" },
      { section: "history", title: "History" },
    ],
    "Current, Waiting, and History must have explicit timeline boundaries",
  );
  await waitFor(
    async () => await page.locator("#visible-section-label").textContent() === "Current",
    "The fixed timeline context did not identify the visible Current section",
  );
  const pausedPlayback = await playbackSnapshot(page);
  assert(pausedPlayback.title && pausedPlayback.text && pausedPlayback.voice);
  assert.equal(pausedPlayback.accent, "#4153be");
  assert(pausedPlayback.icon.width >= 47, "The paused symbol must fill more of the main button");
  await page.evaluate(() => window.superSpeech.setPaused(false));
  await waitFor(
    () => status().state === "playing",
    "The silent fixture did not resume",
  );
  await waitFor(
    async () => await page.locator("body").getAttribute("data-state") === "playing",
    "The Electron window did not render resumed playback",
  );
  const playingPlayback = await playbackSnapshot(page);
  assert.equal(playingPlayback.accent, "#009a91");
  assert(playingPlayback.icon.width >= 47, "The playing symbol must fill more of the main button");
  assert.equal(playingPlayback.title, pausedPlayback.title);
  assert.equal(playingPlayback.text, pausedPlayback.text);
  assert.equal(playingPlayback.voice, pausedPlayback.voice);
  for (const key of ["x", "y", "width", "height"]) {
    assert(
      Math.abs(playingPlayback.button[key] - pausedPlayback.button[key]) < 0.5,
      `The main button changed ${key} between playing and paused`,
    );
  }
  const center = (bounds) => ({
    x: bounds.x + bounds.width / 2,
    y: bounds.y + bounds.height / 2,
  });
  assert.deepEqual(center(playingPlayback.button), center(playingPlayback.ring));
  assert.deepEqual(center(pausedPlayback.button), center(pausedPlayback.ring));
  await page.evaluate(() => window.superSpeech.setPaused(true));
  await waitFor(
    () => status().state === "paused",
    "The silent fixture did not pause again",
  );
  await waitFor(
    async () => await page.locator("body").getAttribute("data-state") === "paused",
    "The Electron window did not return to the paused layout",
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
  await rows.first().scrollIntoViewIfNeeded();
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
      row.querySelector(".queue-chunk")?.getAttribute("aria-label"),
      row.querySelector(".queue-menu-button")?.getAttribute("aria-label"),
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
  const renderedHistoryCount = await page.locator(".queue-item.is-history").count();
  const historyCountLabel = renderedHistoryCount < status().history_count
    ? `${renderedHistoryCount.toLocaleString()} recent of ${status().history_count.toLocaleString()}`
    : renderedHistoryCount.toLocaleString();
  assert.equal(
    await page.locator(".timeline-divider.history-drop-target .timeline-divider-count").textContent(),
    historyCountLabel,
    "History must distinguish rendered recent items from the full archive",
  );
  await waitFor(
    async () => /App \d+\.\d+\.\d+ \| Engine \d+\.\d+\.\d+/.test(
      await page.locator("#version-label").textContent() ?? ""
    ),
    "The footer did not show app and engine versions",
  );
  assert.equal(
    await page.locator(".queue-disclosure").count(),
    0,
    "Timeline rows must not render separate disclosure buttons",
  );
  const menuRow = remainingRows.first();
  const menuRowId = await menuRow.getAttribute("data-item-id");
  const menuRowBounds = await menuRow.boundingBox();
  const dragHandleBounds = await menuRow.locator(".queue-drag-handle").boundingBox();
  const actionAreaBounds = await menuRow.locator(".chunk-actions").boundingBox();
  const menuButton = menuRow.locator(".queue-menu-button");
  const menuButtonBounds = await menuButton.boundingBox();
  assert(
    menuRowBounds && dragHandleBounds && actionAreaBounds && menuButtonBounds,
    "The row controls must be visible",
  );
  assert(
    Math.abs(dragHandleBounds.width - actionAreaBounds.width) < 0.5,
    "The row's left and right control areas must be symmetric",
  );
  assert(
    Math.abs(
      menuRowBounds.y + menuRowBounds.height / 2 -
      (menuButtonBounds.y + menuButtonBounds.height / 2)
    ) < 0.5,
    "The row action button must be vertically centered",
  );
  await menuRow.locator(".queue-chunk").click();
  await waitFor(
    async () => await menuRow.evaluate((row) => row.classList.contains("is-expanded")),
    "The row did not expand for its alignment check",
  );
  const expandedRowBounds = await menuRow.boundingBox();
  const expandedDragBounds = await menuRow.locator(".queue-drag-handle").boundingBox();
  const expandedMenuBounds = await menuButton.boundingBox();
  assert(
    expandedRowBounds && expandedDragBounds && expandedMenuBounds,
    "The expanded row controls must be visible",
  );
  const expandedRowCenter = expandedRowBounds.y + expandedRowBounds.height / 2;
  assert(
    Math.abs(expandedRowCenter - (expandedDragBounds.y + expandedDragBounds.height / 2)) < 0.5,
    "The expanded row drag handle must be vertically centered",
  );
  assert(
    Math.abs(expandedRowCenter - (expandedMenuBounds.y + expandedMenuBounds.height / 2)) < 0.5,
    "The expanded row action button must be vertically centered",
  );
  if (process.env.SUPER_SPEECH_SCREENSHOT) {
    const screenshot = path.parse(process.env.SUPER_SPEECH_SCREENSHOT);
    await page.screenshot({
      path: path.join(screenshot.dir, `${screenshot.name}-expanded${screenshot.ext}`),
    });
  }
  await menuRow.locator(".queue-chunk").click();
  await waitFor(
    async () => await menuRow.evaluate((row) => !row.classList.contains("is-expanded")),
    "The row did not collapse after its alignment check",
  );
  await menuButton.click();
  const visibleActions = page.locator("#queue-action-menu:not([hidden])");
  await new Promise((resolve) => setTimeout(resolve, 800));
  assert.equal(await visibleActions.count(), 1, "The row action menu did not open");
  assert(
    await actionMenuIsFullyVisible(page),
    "The row action menu was clipped or covered",
  );
  assert.deepEqual(
    await visibleActions.locator(".queue-menu-action").allTextContents(),
    ["Play", "Copy text", "Delete"],
  );
  assert.equal(
    await visibleActions.locator(".queue-menu-action").first().evaluate(
      (action) => getComputedStyle(action).fontSize,
    ),
    "10.5px",
  );
  assert.equal(
    await menuButton.locator("span").evaluate((dot) => getComputedStyle(dot).width),
    "2.5px",
  );
  assert.equal(
    await menuRow.locator(".queue-meta").textContent(),
    "Heart",
    "Waiting rows must not repeat the double-click instruction",
  );
  if (process.env.SUPER_SPEECH_SCREENSHOT) {
    const screenshot = path.parse(process.env.SUPER_SPEECH_SCREENSHOT);
    await page.screenshot({
      path: path.join(screenshot.dir, `${screenshot.name}-menu${screenshot.ext}`),
    });
  }
  await electronApp.evaluate(({ clipboard }) => {
    globalThis.__superSpeechSmokeClipboard = null;
    clipboard.writeText = (text) => {
      globalThis.__superSpeechSmokeClipboard = text;
    };
  });
  await visibleActions.getByRole("menuitem", { name: "Copy text" }).click();
  assert.equal(
    await electronApp.evaluate(() => globalThis.__superSpeechSmokeClipboard),
    status().queue.find((item) => item.id === menuRowId)?.text,
    "Copy text must write the full waiting chunk",
  );
  await page.locator("#speech-heading").click();
  assert.equal(await visibleActions.count(), 0, "Clicking outside did not close the action menu");
  const historyMenuButton = page.locator(".queue-item.is-history .queue-menu-button").first();
  await historyMenuButton.scrollIntoViewIfNeeded();
  await historyMenuButton.click();
  assert(
    await actionMenuIsFullyVisible(page),
    "A History menu near the viewport edge was clipped or covered",
  );
  await page.locator("#speech-heading").click();
  if (process.env.SUPER_SPEECH_SCREENSHOT) {
    await page.screenshot({ path: process.env.SUPER_SPEECH_SCREENSHOT });
  }

  const playbackBeforeHistoryGesture = status();
  const historyPlay = page.locator(
    `[data-item-id="${secondId}"].is-history .queue-chunk`,
  );
  const historyBounds = await historyPlay.boundingBox();
  assert(historyBounds, "Archived card must be visible for the gesture test");
  await historyPlay.click();
  await waitFor(
    async () => await page.locator(`[data-item-id="${secondId}"].is-expanded`).count() === 1,
    "A single chunk click did not expand its text",
  );
  await new Promise((resolve) => setTimeout(resolve, 800));
  assert.equal(
    status().current?.id,
    playbackBeforeHistoryGesture.current?.id,
    "A single chunk click started playback",
  );
  await historyPlay.click();
  await waitFor(
    async () => await page.locator(`[data-item-id="${secondId}"].is-expanded`).count() === 0,
    "A second single click did not collapse the chunk",
  );
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

  const deleteRow = remainingRows.first();
  const deletedId = await deleteRow.getAttribute("data-item-id");
  assert(deletedId, "A waiting item must be available for the Delete action");
  await deleteRow.locator(".queue-menu-button").click();
  await page.locator("#queue-action-menu").getByRole("menuitem", { name: "Delete" }).click();
  await waitFor(
    async () => await page.locator(`[data-item-id="${deletedId}"].is-history`).count() === 1,
    "The Delete menu action did not remove the item from the waiting queue",
  );
  await waitFor(
    () => !status().queue.some(({ id }) => id === deletedId),
    "The engine did not persist the Delete menu action",
  );

  const historyCountBeforeReplay = status().history_count;
  await historyPlay.dblclick();
  assert.equal(
    await page.locator("body").getAttribute("data-state"),
    "playing",
    "A selected chunk did not enter the playing presentation immediately",
  );
  assert.equal(await page.locator("#playback-title").textContent(), "Heart");
  assert.equal(
    await page.locator(`[data-item-id="${secondId}"].is-expanded`).count(),
    0,
    "Double-clicking a chunk also expanded it",
  );
  await waitFor(
    () => status().current?.id === secondId,
    "Double-clicking a History chunk did not start it",
    30_000,
  );
  assert.equal(
    status().history_count,
    historyCountBeforeReplay,
    "Replaying a History chunk added a duplicate archive row",
  );
  runEngine("pause");

  runEngine(
    "speak",
    "Jump test current speech keeps the paused fixture alive while the next two waiting rows are added.",
  );
  runEngine(
    "speak",
    "Jump test older waiting speech should move into History when the newer target starts.",
  );
  runEngine(
    "speak",
    "Jump test target has enough words to remain current while the renderer verifies stable timeline order after selection.",
  );
  await waitFor(
    () => status().current && status().state === "paused" && status().queue_count >= 2,
    "The jump-to-here fixture did not reach the waiting queue",
  );
  const interruptedId = status().current.id;
  const [olderWaiting, jumpTarget] = status().queue.slice(-2);
  await waitFor(
    async () => await page.locator(`[data-item-id="${jumpTarget.id}"].is-upcoming`).count() === 1,
    "The jump target did not appear in the renderer",
  );
  const stableIds = [jumpTarget.id, olderWaiting.id, interruptedId];
  const orderBeforeJump = await page.locator(".queue-item").evaluateAll(
    (rows, ids) => rows
      .map((row) => row.getAttribute("data-item-id"))
      .filter((id) => ids.includes(id)),
    stableIds,
  );
  await page.locator(`[data-item-id="${jumpTarget.id}"] .queue-chunk`).dblclick();
  assert.equal(
    await page.locator("body").getAttribute("data-state"),
    "playing",
    "Jump-to-here did not enter the playing presentation immediately",
  );
  await waitFor(
    () => status().current?.id === jumpTarget.id,
    "Jump-to-here did not start the selected waiting item",
    30_000,
  );
  await waitFor(
    async () =>
      await page.locator(`[data-item-id="${olderWaiting.id}"].is-history`).count() === 1 &&
      await page.locator(`[data-item-id="${interruptedId}"].is-history`).count() === 1,
    "Jump-to-here did not move the interrupted and older rows into History",
  );
  assert.deepEqual(
    await page.locator(".queue-item").evaluateAll(
      (rows, ids) => rows
        .map((row) => row.getAttribute("data-item-id"))
        .filter((id) => ids.includes(id)),
      stableIds,
    ),
    orderBeforeJump,
    "Jump-to-here changed row order instead of changing section membership",
  );
  assert(!status().queue.some(({ id }) => id === olderWaiting.id));
  runEngine("pause");

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
