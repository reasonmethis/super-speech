import "./styles.css";
import {
  ENGINE_STATUS_VERSION,
  INITIAL_STATUS,
  moveQueueItemBefore,
  timelineItems,
  type RuntimeStatus,
  type TimelineItem,
} from "./runtime";
import {
  isHistoryDropArea,
  queueDropBeforeId,
  startQueueDrag,
  transitionQueueDrag,
  type QueueDragState,
} from "./queue-drag-model";

const demoStatus: RuntimeStatus = {
  version: ENGINE_STATUS_VERSION,
  state: "playing",
  updated_at: Date.now() / 1000,
  engine_pid: 4821,
  engine_running: true,
  installed: true,
  current: {
    id: "014-af_heart-say",
    filename: "014-af_heart-say.txt",
    text: "The first desktop version is taking shape. You can pause it without losing your place.",
    voice: "af_heart",
    piece: 1,
    piece_count: 2,
    elapsed_seconds: 4.2,
  },
  queue_count: 2,
  queue: [
    {
      id: "015-bm_fable-say",
      filename: "015-bm_fable-say.txt",
      text: "Click this speech item to play it now. Use its arrow to expand or collapse the complete text.",
      voice: "bm_fable",
    },
    {
      id: "016-af_bella-say",
      filename: "016-af_bella-say.txt",
      text: "Every voice and source app will be easy to spot at a glance.",
      voice: "af_bella",
    },
  ],
  history_count: 2,
  history: [
    {
      id: "013-bm_george-say",
      filename: "013-bm_george-say.txt",
      text: "Earlier speech stays available here whenever you want to hear it again.",
      voice: "bm_george",
    },
    {
      id: "012-af_aoede-say",
      filename: "012-af_aoede-say.txt",
      text: "The app keeps upcoming speech intact when you choose something else.",
      voice: "af_aoede",
    },
  ],
};

const playbackButton = requiredElement<HTMLButtonElement>("playback-button");
const playbackIcon = requiredElement<HTMLSpanElement>("playback-icon");
const statusDot = requiredElement<HTMLSpanElement>("status-dot");
const statusLabel = requiredElement<HTMLSpanElement>("status-label");
const playbackCopy = requiredElement<HTMLDivElement>("playback-copy");
const playbackTitle = requiredElement<HTMLHeadingElement>("playback-title");
const currentText = requiredElement<HTMLParagraphElement>("current-text");
const voicePill = requiredElement<HTMLSpanElement>("voice-pill");
const voiceLabel = requiredElement<HTMLSpanElement>("voice-label");
const metadataRow = requiredElement<HTMLDivElement>("metadata-row");
const queueCount = requiredElement<HTMLSpanElement>("queue-count");
const clearQueueButton = requiredElement<HTMLButtonElement>("clear-queue-button");
const queueList = requiredElement<HTMLDivElement>("queue-list");
const commandStatus = requiredElement<HTMLDivElement>("command-status");
const desktopApi = window.superSpeech;

interface PendingSelection {
  sourceId: string;
  resultId: string | null;
  acceptedAt: number | null;
  timeoutId: number;
}

interface PendingQueueMutation {
  action: "move" | "archive";
  id: string;
}

interface QueuePointerDrag {
  state: QueueDragState;
  startX: number;
  startY: number;
  pointerOffsetX: number;
  pointerOffsetY: number;
  width: number;
}

let currentStatus = desktopApi ? INITIAL_STATUS : demoStatus;
let commandPending = false;
let pendingSelection: PendingSelection | null = null;
let failedChunkId: string | null = null;
let clearPending = false;
let clearFailed = false;
let clearBaselineIds = new Set<string>();
let clearTimeoutId: number | null = null;
let expandedItemId: string | null = null;
let renderedTimelineKey: string | null = null;
let pendingQueueMutation: PendingQueueMutation | null = null;
let failedQueueMutationId: string | null = null;
let queueMutationGeneration = 0;
let queuePointerDrag: QueuePointerDrag | null = null;

const QUEUE_DRAG_THRESHOLD = 5;

function requiredElement<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing required element #${id}`);
  }
  return element as T;
}

function formatVoice(voice: string): string {
  const names: Record<string, string> = {
    af_aoede: "Aoede",
    af_bella: "Bella",
    af_heart: "Heart",
    am_echo: "Echo",
    bm_fable: "Fable",
    bm_george: "George",
  };
  return names[voice] ?? voice.replace(/^[a-z]{2}_/, "").replaceAll("_", " ");
}

function timelineAction(item: TimelineItem, pending: boolean, failed: boolean): string {
  if (failedQueueMutationId === item.id) {
    return "Could not move / Try again";
  }
  if (pending) {
    return pendingSelection?.acceptedAt ? "Selected / Up next" : "Starting...";
  }
  if (failed) {
    return "Could not start / Try again";
  }
  if (item.kind === "history") {
    return "Replay";
  }
  if (item.kind === "upcoming") {
    return "Play now";
  }
  return currentStatus.state === "paused" ? "Paused" : "Speaking";
}

function itemReference(item: TimelineItem): string {
  const summary = item.text.trim().slice(0, 40);
  if (item.kind === "current") {
    return "current speech";
  }
  if (item.kind === "upcoming") {
    return `waiting speech ${item.position}, ${summary}`;
  }
  return `history speech, ${summary}`;
}

function selectionWasApplied(status: RuntimeStatus, selection: PendingSelection): boolean {
  return Boolean(
    selection.resultId &&
    (status.current?.id === selection.resultId ||
      status.history.some(({ id }) => id === selection.resultId)),
  );
}

function selectionFailed(status: RuntimeStatus, selection: PendingSelection): boolean {
  if (
    !selection.resultId ||
    selection.acceptedAt === null ||
    status.updated_at < selection.acceptedAt
  ) {
    return false;
  }
  return ![
    ...(status.current ? [status.current] : []),
    ...status.queue,
    ...status.history,
  ].some(({ id }) => id === selection.resultId);
}

function reconcileCommands(status: RuntimeStatus): void {
  if (pendingSelection && selectionWasApplied(status, pendingSelection)) {
    window.clearTimeout(pendingSelection.timeoutId);
    pendingSelection = null;
    commandStatus.textContent = "";
  }
  if (pendingSelection && selectionFailed(status, pendingSelection)) {
    failedChunkId = pendingSelection.sourceId;
    pendingSelection = null;
    commandStatus.textContent = "Could not start selected speech. Try again.";
  }
  if (pendingSelection && status.state === "stopped" && !status.engine_running) {
    window.clearTimeout(pendingSelection.timeoutId);
    failedChunkId = pendingSelection.sourceId;
    pendingSelection = null;
    commandStatus.textContent = "Could not start selected speech. Try again.";
  }
  const clearWasApplied =
    (clearBaselineIds.size > 0 &&
      [...clearBaselineIds].every((id) => !status.queue.some((item) => item.id === id))) ||
    (clearPending && status.queue_count === 0);
  if (clearWasApplied) {
    const restoreFocus = document.activeElement === clearQueueButton;
    if (clearTimeoutId !== null) {
      window.clearTimeout(clearTimeoutId);
    }
    clearTimeoutId = null;
    clearPending = false;
    clearFailed = false;
    clearBaselineIds = new Set();
    commandStatus.textContent = "";
    if (restoreFocus) {
      queueList.focus({ preventScroll: true });
    }
  }
}

function playActionLabel(item: TimelineItem): string {
  if (item.kind === "current") {
    return currentStatus.state === "paused" ? "Currently paused" : "Currently speaking";
  }
  const reference = itemReference(item);
  return item.kind === "history" ? `Replay: ${reference}` : `Play now: ${reference}`;
}

function statusCopy(status: RuntimeStatus): {
  label: string;
  title?: string;
  body?: string;
} {
  if (status.state === "setup_required") {
    return {
      label: "Install incomplete",
      title: "Speech files are missing",
      body: "Reinstall Super Speech to restore its bundled engine and voices.",
    };
  }
  if (status.state === "stopped") {
    return {
      label: "Engine stopped",
      title: "Super Speech could not start",
      body: "Open the runtime folder from the tray and inspect engine.log for details.",
    };
  }
  if (status.state === "loading") {
    return {
      label: "Loading",
      title: "Preparing your voice",
      body: "Kokoro is loading locally. This normally takes a few seconds.",
    };
  }
  if (status.state === "paused") {
    return { label: "Paused" };
  }
  if (status.state === "playing" && status.current) {
    return {
      label: "Speaking",
      title: formatVoice(status.current.voice),
      body: status.current.text,
    };
  }
  return {
    label: "Ready",
    title: "Ready when you are",
    body: "Your next voice reply will appear here as soon as it starts.",
  };
}

type PlaybackAction = "pause" | "resume" | "setup" | "ready";

function playbackAction(status: RuntimeStatus): PlaybackAction {
  if (status.state === "setup_required") {
    return "setup";
  }
  if (status.state === "paused") {
    return "resume";
  }
  return status.state === "playing" ? "pause" : "ready";
}

function playbackIconMarkup(status: RuntimeStatus): string {
  if (status.state === "setup_required") {
    return '<svg viewBox="0 0 32 32"><path d="M16 7v14m-6-5 6 6 6-6M8 25h16"/></svg>';
  }
  if (status.state === "paused") {
    return '<svg viewBox="0 0 32 32"><path class="solid" d="m11 8 13 8-13 8Z"/></svg>';
  }
  if (status.state === "playing") {
    return '<svg viewBox="0 0 32 32"><rect class="solid" x="9" y="8" width="5" height="16" rx="2"/><rect class="solid" x="18" y="8" width="5" height="16" rx="2"/></svg>';
  }
  if (status.state === "stopped") {
    return '<svg viewBox="0 0 32 32"><path d="M16 8v9m0 6v1"/></svg>';
  }
  return '<img class="idle-icon" src="./icon.svg" alt="">';
}

function render(status: RuntimeStatus): void {
  reconcileCommands(status);
  currentStatus = status;
  const paused = status.state === "paused";
  const copy = statusCopy(status);
  const action = playbackAction(status);
  const showPlaybackCopy = copy.title !== undefined;
  document.body.dataset.state = status.state;

  statusDot.className = `status-dot state-${status.state}`;
  statusLabel.textContent = copy.label;
  playbackCopy.classList.toggle("is-hidden", !showPlaybackCopy);
  playbackTitle.textContent = copy.title ?? "";
  currentText.textContent = copy.body ?? "";

  const actionLabels: Record<PlaybackAction, string> = {
    pause: "Pause speech",
    resume: "Resume speech",
    setup: "Open setup guide",
    ready: status.state === "loading" ? "Preparing speech" : "Ready for speech",
  };
  playbackButton.dataset.action = action;
  playbackButton.setAttribute("aria-label", actionLabels[action]);
  playbackButton.disabled = commandPending || pendingQueueMutation !== null || action === "ready";
  playbackButton.setAttribute("aria-busy", String(commandPending || status.state === "loading"));
  playbackIcon.innerHTML = playbackIconMarkup(status);

  const current = paused ? null : status.current;
  metadataRow.classList.toggle("is-hidden", !current);
  if (current) {
    voiceLabel.textContent = formatVoice(current.voice);
    voicePill.classList.remove("is-hidden");
  } else {
    voicePill.classList.add("is-hidden");
  }

  queueCount.textContent = `${status.queue_count} waiting`;
  clearQueueButton.classList.toggle("is-hidden", status.queue_count === 0);
  clearQueueButton.disabled = commandPending || clearPending || pendingQueueMutation !== null;
  clearQueueButton.setAttribute("aria-disabled", String(clearQueueButton.disabled));
  clearQueueButton.setAttribute("aria-busy", String(clearPending));
  clearQueueButton.setAttribute(
    "aria-label",
    clearPending
      ? "Clearing waiting speech"
      : clearFailed
        ? "Retry clearing waiting speech"
        : `Clear ${status.queue_count} waiting speech ${status.queue_count === 1 ? "item" : "items"}; current speech and playback state will not change`,
  );
  clearQueueButton.textContent = clearPending
    ? "Clearing..."
    : clearFailed
      ? "Retry clear all"
      : "Clear all";
  renderTimeline(timelineItems(status), status.history_count);
}

function clearHistoryDropIndicator(): void {
  for (const element of queueList.querySelectorAll(".is-history-drop")) {
    element.classList.remove("is-history-drop");
  }
}

function beginQueuePointerDrag(
  event: PointerEvent,
  id: string,
  row: HTMLElement,
  handle: HTMLButtonElement,
): void {
  if (
    event.button !== 0 ||
    !event.isPrimary ||
    commandPending ||
    pendingQueueMutation ||
    clearPending
  ) {
    return;
  }

  cancelQueuePointerDrag();
  event.preventDefault();
  const bounds = row.getBoundingClientRect();
  const visualOrder = [...currentStatus.queue].reverse().map(({ id }) => id);
  const state = startQueueDrag(event.pointerId, id, visualOrder);
  if (!state) {
    return;
  }
  handle.focus({ preventScroll: true });
  queueList.setPointerCapture(event.pointerId);
  queuePointerDrag = {
    state,
    startX: event.clientX,
    startY: event.clientY,
    pointerOffsetX: event.clientX - bounds.left,
    pointerOffsetY: event.clientY - bounds.top,
    width: bounds.width,
  };
}

function upcomingRow(id: string): HTMLElement | null {
  return [...queueList.querySelectorAll<HTMLElement>(".queue-item.is-upcoming")]
    .find((row) => row.dataset.itemId === id) ?? null;
}

function activeDragGhost(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".queue-drag-ghost");
}

function activateQueuePointerDrag(drag: QueuePointerDrag): boolean {
  const row = upcomingRow(drag.state.sourceId);
  if (!row) {
    return false;
  }
  const bounds = row.getBoundingClientRect();
  const ghost = row.cloneNode(true) as HTMLElement;
  ghost.classList.add("queue-drag-ghost");
  ghost.setAttribute("aria-hidden", "true");
  for (const element of ghost.querySelectorAll<HTMLElement>(
    "[id], [aria-controls], [aria-labelledby], [aria-describedby]",
  )) {
    element.removeAttribute("id");
    element.removeAttribute("aria-controls");
    element.removeAttribute("aria-labelledby");
    element.removeAttribute("aria-describedby");
  }
  for (const button of ghost.querySelectorAll<HTMLButtonElement>("button")) {
    button.disabled = true;
    button.tabIndex = -1;
  }
  ghost.style.left = `${bounds.left}px`;
  ghost.style.top = `${bounds.top}px`;
  ghost.style.width = `${bounds.width}px`;
  ghost.style.height = `${bounds.height}px`;
  document.body.append(ghost);
  queueList.classList.add("is-queue-dragging");
  row.classList.add("is-drag-source");
  return true;
}

function clearQueueDragVisuals(): void {
  for (const ghost of document.querySelectorAll(".queue-drag-ghost")) {
    ghost.remove();
  }
  for (const row of queueList.querySelectorAll(".is-drag-source")) {
    row.classList.remove("is-drag-source");
  }
  queueList.classList.remove("is-queue-dragging");
  clearHistoryDropIndicator();
}

function releaseQueuePointer(pointerId: number): void {
  try {
    if (queueList.hasPointerCapture(pointerId)) {
      queueList.releasePointerCapture(pointerId);
    }
  } finally {
    clearQueueDragVisuals();
  }
}

function applyQueueVisualOrder(
  visualOrder: readonly string[],
  animate = true,
): void {
  const rows = [...queueList.querySelectorAll<HTMLElement>(".queue-item.is-upcoming")];
  for (const row of rows) {
    for (const animation of row.getAnimations()) {
      animation.cancel();
    }
  }
  const currentOrder = rows.map((row) => row.dataset.itemId ?? "");
  if (visualOrder.every((id, index) => id === currentOrder[index])) {
    return;
  }
  const rowsById = new Map(rows.map((row) => [row.dataset.itemId ?? "", row]));
  const previousTops = new Map(rows.map((row) => [row, row.getBoundingClientRect().top]));
  const anchor = queueList.querySelector<HTMLElement>(
    ".queue-item.is-current, .timeline-divider",
  );
  for (const id of visualOrder) {
    const row = rowsById.get(id);
    if (row) {
      queueList.insertBefore(row, anchor);
    }
  }
  if (!animate || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return;
  }
  for (const row of rows) {
    const distance = (previousTops.get(row) ?? 0) - row.getBoundingClientRect().top;
    if (distance === 0) {
      continue;
    }
    row.animate(
      [{ transform: `translateY(${distance}px)` }, { transform: "translateY(0)" }],
      { duration: 130, easing: "ease-out" },
    );
  }
}

function updateQueuePointerDrag(event: PointerEvent): void {
  const drag = queuePointerDrag;
  if (!drag || event.pointerId !== drag.state.pointerId) {
    return;
  }
  if ((event.buttons & 1) === 0) {
    cancelQueuePointerDrag();
    return;
  }
  event.preventDefault();
  if (drag.state.phase === "armed") {
    const distance = Math.hypot(
      event.clientX - drag.startX,
      event.clientY - drag.startY,
    );
    if (distance < QUEUE_DRAG_THRESHOLD) {
      return;
    }
    if (!activateQueuePointerDrag(drag)) {
      cancelQueuePointerDrag();
      return;
    }
  }

  const listBounds = queueList.getBoundingClientRect();
  const left = Math.min(
    Math.max(event.clientX - drag.pointerOffsetX, listBounds.left),
    listBounds.right - drag.width,
  );
  const ghost = activeDragGhost();
  if (!ghost) {
    cancelQueuePointerDrag();
    return;
  }
  ghost.style.left = `${left}px`;
  ghost.style.top = `${event.clientY - drag.pointerOffsetY}px`;

  clearHistoryDropIndicator();
  const pointed = document.elementFromPoint(event.clientX, event.clientY);
  const historyTarget = pointed instanceof Element
    ? pointed.closest<HTMLElement>(".history-drop-target, .queue-item.is-history")
    : null;
  const historyDivider = queueList.querySelector<HTMLElement>(
    ".timeline-divider.history-drop-target",
  );
  const overHistory = historyTarget && queueList.contains(historyTarget)
    ? historyTarget
    : historyDivider && isHistoryDropArea(
        listBounds,
        historyDivider.getBoundingClientRect().top,
        event.clientX,
        event.clientY,
      )
      ? historyDivider
      : null;
  if (overHistory) {
    overHistory.classList.add("is-history-drop");
    const transition = transitionQueueDrag(drag.state, {
      type: "preview-history",
      pointerId: event.pointerId,
    });
    if (transition.state) {
      drag.state = transition.state;
    }
    if (transition.visualOrder) {
      applyQueueVisualOrder(transition.visualOrder);
    }
    return;
  }

  const rows = [...queueList.querySelectorAll<HTMLElement>(".queue-item.is-upcoming")];
  const beforeId = queueDropBeforeId(
    drag.state.sourceId,
    rows.map((row) => {
      const bounds = row.getBoundingClientRect();
      return {
        id: row.dataset.itemId ?? "",
        top: bounds.top,
        height: bounds.height,
      };
    }),
    event.clientY,
  );
  const transition = transitionQueueDrag(drag.state, {
    type: "preview-queue",
    pointerId: event.pointerId,
    beforeId,
  });
  if (transition.state) {
    drag.state = transition.state;
  }
  if (transition.visualOrder) {
    applyQueueVisualOrder(transition.visualOrder);
  }
}

function finishQueuePointerDrag(event: PointerEvent, commit: boolean): void {
  const drag = queuePointerDrag;
  if (!drag || event.pointerId !== drag.state.pointerId) {
    return;
  }
  const transition = transitionQueueDrag(drag.state, {
    type: "finish",
    pointerId: event.pointerId,
    commit,
  });
  queuePointerDrag = null;
  let command = transition.command;
  let projectionFailed = false;
  try {
    if (transition.visualOrder) {
      applyQueueVisualOrder(transition.visualOrder, false);
    }
  } catch (error) {
    console.error("Could not settle queue drag", error);
    command = null;
    projectionFailed = true;
  } finally {
    releaseQueuePointer(event.pointerId);
  }
  if (projectionFailed) {
    renderedTimelineKey = null;
    render(currentStatus);
    return;
  }
  if (command?.type === "archive") {
    void archiveWaitingItem(command.id);
  } else if (command?.type === "move") {
    void moveWaitingItem(command.id, command.beforeId);
  }
}

function cancelQueuePointerDrag(): void {
  const drag = queuePointerDrag;
  if (!drag) {
    clearQueueDragVisuals();
    return;
  }
  const transition = transitionQueueDrag(drag.state, { type: "cancel" });
  queuePointerDrag = null;
  let projectionFailed = false;
  try {
    if (transition.visualOrder) {
      applyQueueVisualOrder(transition.visualOrder, false);
    }
  } catch (error) {
    console.error("Could not cancel queue drag", error);
    projectionFailed = true;
  } finally {
    releaseQueuePointer(drag.state.pointerId);
  }
  if (projectionFailed) {
    renderedTimelineKey = null;
    render(currentStatus);
  }
}

function handleQueueReorderKey(event: KeyboardEvent, id: string): void {
  const ids = currentStatus.queue.map((item) => item.id);
  const index = ids.indexOf(id);
  if (index < 0 || commandPending || pendingQueueMutation || clearPending) {
    return;
  }
  let beforeId: string | null | undefined;
  if (event.key === "ArrowUp" && index < ids.length - 1) {
    beforeId = ids[index + 2] ?? null;
  } else if (event.key === "ArrowDown" && index > 0) {
    beforeId = ids[index - 1];
  } else if (event.key === "Home" && index < ids.length - 1) {
    beforeId = null;
  } else if (event.key === "End" && index > 0) {
    beforeId = ids[0];
  }
  if (beforeId !== undefined) {
    event.preventDefault();
    void moveWaitingItem(id, beforeId);
  }
}

function renderTimeline(items: TimelineItem[], historyTotal: number): void {
  if (!items.some((item) => item.id === expandedItemId)) {
    expandedItemId = null;
  }
  if (!items.some((item) => item.id === failedChunkId)) {
    failedChunkId = null;
  }
  if (
    !items.some(
      (item) => item.kind === "upcoming" && item.id === failedQueueMutationId,
    )
  ) {
    failedQueueMutationId = null;
  }
  // Preserve row nodes across polling so hover, focus, expansion, and in-progress clicks survive
  const timelineKey = JSON.stringify([
    historyTotal,
    items.map(({ id, text, voice, kind, position }) => [id, text, voice, kind, position]),
  ]);
  if (timelineKey === renderedTimelineKey) {
    updateTimelineRows(items);
    updateTimelineFade();
    return;
  }
  cancelQueuePointerDrag();
  renderedTimelineKey = timelineKey;

  const previousScrollTop = queueList.scrollTop;
  const focusedControl = document.activeElement instanceof HTMLElement
    ? document.activeElement.closest<HTMLElement>(".queue-item")
    : null;
  const focusedItemId = focusedControl?.dataset.itemId;
  const focusedControlClass = [
    "queue-drag-handle",
    "queue-play",
    "queue-remove",
    "queue-disclosure",
  ].find((className) => document.activeElement?.classList.contains(className));
  queueList.replaceChildren();
  if (items.length === 0) {
    const empty = document.createElement("div");
    empty.className = "queue-empty";
    empty.innerHTML = '<span class="empty-check">&#10003;</span><span>No speech yet</span>';
    queueList.append(empty);
    updateTimelineFade();
    return;
  }

  const visibleHistory = items.filter(({ kind }) => kind === "history").length;
  const hasUpcoming = items.some(({ kind }) => kind === "upcoming");
  let historyDividerAdded = false;
  const appendHistoryDivider = (): void => {
    historyDividerAdded = true;
    const divider = document.createElement("div");
    divider.className = "timeline-divider history-drop-target";
    const historyCount = visibleHistory < historyTotal
      ? `${visibleHistory} of ${historyTotal}`
      : String(historyTotal);
    divider.innerHTML = `<span>History</span><span>${historyCount}</span>`;
    queueList.append(divider);
  };
  for (const item of items) {
    if (item.kind === "history" && !historyDividerAdded) {
      appendHistoryDivider();
    }

    const row = document.createElement("div");
    row.className = "queue-item";
    row.dataset.itemId = item.id;
    const isCurrent = item.kind === "current";
    const isUpcoming = item.kind === "upcoming";
    const isExpanded = item.id === expandedItemId;
    row.classList.add(`is-${item.kind}`);
    if (isCurrent) {
      row.setAttribute("aria-current", "true");
    }

    const rowControls: HTMLElement[] = [];
    if (isUpcoming) {
      const dragHandle = document.createElement("button");
      dragHandle.className = "queue-drag-handle";
      dragHandle.type = "button";
      dragHandle.title = "Drag to reorder";
      dragHandle.setAttribute(
        "aria-label",
        `Reorder ${itemReference(item)}. Drag, or use the arrow keys`,
      );
      dragHandle.innerHTML = '<span aria-hidden="true"></span>';
      dragHandle.addEventListener("keydown", (event) => {
        handleQueueReorderKey(event, item.id);
      });
      rowControls.push(dragHandle);
    }

    const play = document.createElement("button");
    play.className = "queue-play";
    play.type = "button";

    const order = document.createElement("span");
    order.className = "queue-order";
    order.textContent = isCurrent
      ? "NOW"
      : item.kind === "history"
        ? "HIST"
        : String(item.position).padStart(2, "0");

    const copy = document.createElement("div");
    copy.className = "queue-copy";
    const text = document.createElement("p");
    text.textContent = item.text;
    const meta = document.createElement("span");
    meta.className = "queue-meta";
    copy.append(text, meta);
    play.append(order, copy);

    const disclosure = document.createElement("button");
    disclosure.className = "queue-disclosure";
    disclosure.type = "button";
    disclosure.setAttribute("aria-expanded", String(isExpanded));
    const accessibleText = document.createElement("div");
    accessibleText.id = `speech-full-${item.id}`;
    accessibleText.className = "sr-only queue-full-text";
    accessibleText.hidden = !isExpanded;
    accessibleText.setAttribute("role", "region");
    accessibleText.setAttribute("aria-label", `Full text for ${itemReference(item)}`);
    accessibleText.textContent = item.text;
    disclosure.setAttribute("aria-controls", accessibleText.id);
    disclosure.dataset.itemReference = itemReference(item);
    disclosure.setAttribute(
      "aria-label",
      `${isExpanded ? "Collapse" : "Expand"} full text for ${itemReference(item)}`,
    );
    disclosure.innerHTML = '<span aria-hidden="true"></span>';
    disclosure.addEventListener("click", () => {
      const expanding = expandedItemId !== item.id;
      setExpandedItem(expanding ? item.id : null);
      if (expanding) {
        row.scrollIntoView({ block: "nearest" });
      }
    });

    play.addEventListener("click", () => void playTimelineItem(item));
    rowControls.push(play);
    if (isUpcoming) {
      const remove = document.createElement("button");
      remove.className = "queue-remove";
      remove.type = "button";
      remove.title = "Move to History";
      remove.setAttribute("aria-label", `Move ${itemReference(item)} to History`);
      remove.innerHTML = '<span aria-hidden="true"></span>';
      remove.addEventListener("click", () => void archiveWaitingItem(item.id));
      rowControls.push(remove);
    }
    rowControls.push(disclosure, accessibleText);
    row.classList.toggle("is-expanded", isExpanded);
    row.append(...rowControls);
    queueList.append(row);
  }
  if (hasUpcoming && !historyDividerAdded) {
    appendHistoryDivider();
  }
  queueList.scrollTop = previousScrollTop;
  updateTimelineRows(items);
  updateTimelineFade();
  if (focusedItemId) {
    const row = queueList.querySelector<HTMLElement>(`[data-item-id="${focusedItemId}"]`);
    const preferred = focusedControlClass
      ? row?.querySelector<HTMLButtonElement>(`.${focusedControlClass}`)
      : row?.querySelector<HTMLButtonElement>(".queue-play");
    const fallback = row?.querySelector<HTMLButtonElement>(".queue-disclosure");
    (preferred?.disabled ? fallback : preferred)?.focus({ preventScroll: true });
  }
}

function updateTimelineRows(items: TimelineItem[]): void {
  const itemById = new Map(items.map((item) => [item.id, item]));
  const pendingId = pendingSelection?.sourceId ?? null;
  const commandInFlight = pendingSelection !== null && pendingSelection.acceptedAt === null;
  const queueCommandInFlight = commandPending || pendingQueueMutation !== null;
  for (const row of queueList.querySelectorAll<HTMLElement>(".queue-item")) {
    const item = itemById.get(row.dataset.itemId ?? "");
    if (!item) {
      continue;
    }
    const pending = item.id === pendingId;
    const selected = pending && !commandInFlight;
    const failed = item.id === failedChunkId;
    row.classList.toggle("is-pending", pending);
    row.classList.toggle(
      "is-error",
      failed || failedQueueMutationId === item.id,
    );
    const play = row.querySelector<HTMLButtonElement>(".queue-play");
    if (play) {
      play.disabled =
        item.kind === "current" || commandInFlight || queueCommandInFlight;
      play.setAttribute("aria-disabled", String(play.disabled));
      play.setAttribute("aria-busy", String(pending && commandInFlight));
      play.setAttribute(
        "aria-label",
        selected
          ? `Selected and up next: ${itemReference(item)}. Activate to select again`
          : playActionLabel(item),
      );
    }
    const dragHandle = row.querySelector<HTMLButtonElement>(".queue-drag-handle");
    if (dragHandle) {
      dragHandle.disabled = queueCommandInFlight || clearPending;
    }
    const remove = row.querySelector<HTMLButtonElement>(".queue-remove");
    if (remove) {
      remove.disabled = queueCommandInFlight || clearPending;
      remove.setAttribute(
        "aria-busy",
        String(
          pendingQueueMutation?.action === "archive" &&
            pendingQueueMutation.id === item.id,
        ),
      );
    }
    const meta = row.querySelector<HTMLElement>(".queue-meta");
    if (meta) {
      meta.textContent = `${formatVoice(item.voice)}  /  ${timelineAction(item, pending, failed)}`;
    }
  }
}

function updateTimelineFade(): void {
  const remaining = queueList.scrollHeight - queueList.clientHeight - queueList.scrollTop;
  queueList.classList.toggle("has-more", remaining > 1);
}

function setExpandedItem(id: string | null): void {
  expandedItemId = id;
  for (const row of queueList.querySelectorAll<HTMLElement>(".queue-item")) {
    const expanded = row.dataset.itemId === id;
    row.classList.toggle("is-expanded", expanded);
    const disclosure = row.querySelector<HTMLButtonElement>(".queue-disclosure");
    disclosure?.setAttribute("aria-expanded", String(expanded));
    disclosure?.setAttribute(
      "aria-label",
      `${expanded ? "Collapse" : "Expand"} full text for ${disclosure.dataset.itemReference}`,
    );
    const accessibleText = row.querySelector<HTMLElement>(".queue-full-text");
    if (accessibleText) {
      accessibleText.hidden = !expanded;
    }
  }
  updateTimelineFade();
}

async function playTimelineItem(item: TimelineItem): Promise<void> {
  if (
    item.kind === "current" ||
    pendingQueueMutation !== null ||
    (pendingSelection !== null && pendingSelection.acceptedAt === null)
  ) {
    return;
  }
  failedChunkId = null;
  const selection: PendingSelection = {
    sourceId: item.id,
    resultId: null,
    acceptedAt: null,
    timeoutId: 0,
  };
  selection.timeoutId = window.setTimeout(() => {
    if (pendingSelection === selection) {
      pendingSelection = null;
      failedChunkId = item.id;
      commandStatus.textContent = "Could not start selected speech. Try again.";
      render(currentStatus);
    }
  }, 65_000);
  pendingSelection = selection;
  commandStatus.textContent = "";
  render(currentStatus);
  try {
    if (desktopApi) {
      const acceptance = await desktopApi.playChunk(item.id);
      if (pendingSelection === selection) {
        selection.resultId = acceptance.id;
        selection.acceptedAt = acceptance.acceptedAt;
        window.clearTimeout(selection.timeoutId);
        commandStatus.textContent = "Selected speech is up next.";
        render(currentStatus);
      }
      void refreshStatus();
    } else {
      console.info(`Demo playback requested for ${item.id}`);
      await new Promise((resolve) => window.setTimeout(resolve, 700));
      window.clearTimeout(selection.timeoutId);
      if (pendingSelection === selection) {
        pendingSelection = null;
      }
      render(currentStatus);
    }
  } catch (error) {
    console.error("Could not start the selected speech", error);
    window.clearTimeout(selection.timeoutId);
    if (pendingSelection === selection) {
      pendingSelection = null;
    }
    failedChunkId = item.id;
    commandStatus.textContent = "Could not start selected speech. Try again.";
    render(currentStatus);
  }
}

async function moveWaitingItem(id: string, beforeId: string | null): Promise<void> {
  if (commandPending || pendingQueueMutation || clearPending) {
    return;
  }
  const reordered = moveQueueItemBefore(currentStatus.queue, id, beforeId);
  const previousIds = currentStatus.queue.map((item) => item.id);
  if (reordered.every((item, index) => item.id === previousIds[index])) {
    return;
  }
  const previousStatus = currentStatus;
  queueMutationGeneration += 1;
  pendingQueueMutation = { action: "move", id };
  failedQueueMutationId = null;
  commandStatus.textContent = "";
  currentStatus = { ...currentStatus, queue: reordered };
  render(currentStatus);
  try {
    if (desktopApi) {
      await desktopApi.moveQueueItem(id, beforeId);
    } else {
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    pendingQueueMutation = null;
    render(currentStatus);
    void refreshStatus();
  } catch (error) {
    console.error("Could not reorder waiting speech", error);
    pendingQueueMutation = null;
    currentStatus = previousStatus;
    failedQueueMutationId = id;
    commandStatus.textContent = "Could not reorder waiting speech. Try again.";
    render(currentStatus);
  }
}

async function archiveWaitingItem(id: string): Promise<void> {
  if (commandPending || pendingQueueMutation || clearPending) {
    return;
  }
  const item = currentStatus.queue.find((queued) => queued.id === id);
  if (!item) {
    return;
  }
  const previousStatus = currentStatus;
  const alreadyInHistory = currentStatus.history.some((entry) => entry.id === id);
  queueMutationGeneration += 1;
  pendingQueueMutation = { action: "archive", id };
  failedQueueMutationId = null;
  commandStatus.textContent = "";
  currentStatus = {
    ...currentStatus,
    queue_count: Math.max(0, currentStatus.queue_count - 1),
    queue: currentStatus.queue.filter((queued) => queued.id !== id),
    history_count: alreadyInHistory
      ? currentStatus.history_count
      : currentStatus.history_count + 1,
    history: [item, ...currentStatus.history.filter((entry) => entry.id !== id)].slice(0, 50),
  };
  render(currentStatus);
  try {
    if (desktopApi) {
      await desktopApi.archiveQueueItem(id);
    } else {
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    pendingQueueMutation = null;
    render(currentStatus);
    void refreshStatus();
  } catch (error) {
    console.error("Could not move waiting speech to History", error);
    pendingQueueMutation = null;
    currentStatus = previousStatus;
    failedQueueMutationId = id;
    commandStatus.textContent = "Could not move waiting speech to History. Try again.";
    render(currentStatus);
  }
}

async function refreshStatus(): Promise<void> {
  if (!desktopApi) {
    render(currentStatus);
    return;
  }
  try {
    const mutationAtStart = pendingQueueMutation;
    const queueGenerationAtStart = queueMutationGeneration;
    const status = await desktopApi.getStatus();
    if (
      !mutationAtStart &&
      !pendingQueueMutation &&
      queueGenerationAtStart === queueMutationGeneration
    ) {
      render(status);
    }
  } catch (error) {
    console.error("Could not read Super Speech status", error);
    render({ ...currentStatus, state: "stopped", engine_running: false });
    statusLabel.textContent = "Disconnected";
  }
}

playbackButton.addEventListener("click", async () => {
  const action = playbackAction(currentStatus);
  if (commandPending || pendingQueueMutation || queuePointerDrag || action === "ready") {
    return;
  }
  commandPending = true;
  render(currentStatus);
  if (action === "setup") {
    try {
      await desktopApi?.openSetup();
    } catch (error) {
      console.error("Could not open the setup guide", error);
    } finally {
      commandPending = false;
      render(currentStatus);
    }
    return;
  }
  const paused = action === "pause";
  try {
    if (desktopApi) {
      render(await desktopApi.setPaused(paused));
    } else {
      render({ ...currentStatus, state: paused ? "paused" : "playing" });
    }
  } catch (error) {
    console.error("Could not change playback state", error);
  } finally {
    commandPending = false;
    render(currentStatus);
  }
});

clearQueueButton.addEventListener("click", async () => {
  if (
    commandPending ||
    clearPending ||
    pendingQueueMutation ||
    currentStatus.queue_count === 0
  ) {
    return;
  }
  clearPending = true;
  clearFailed = false;
  clearBaselineIds = new Set(currentStatus.queue.map(({ id }) => id));
  commandStatus.textContent = "";
  clearTimeoutId = window.setTimeout(() => {
    clearTimeoutId = null;
    clearPending = false;
    clearFailed = true;
    commandStatus.textContent = "Could not clear waiting speech. Try again.";
    render(currentStatus);
  }, 10_000);
  render(currentStatus);
  try {
    if (desktopApi) {
      await desktopApi.clearQueue();
      void refreshStatus();
    } else {
      console.info("Demo queue clear requested");
      await new Promise((resolve) => window.setTimeout(resolve, 700));
      window.clearTimeout(clearTimeoutId);
      clearTimeoutId = null;
      clearPending = false;
      render(currentStatus);
    }
  } catch (error) {
    console.error("Could not clear the speech queue", error);
    if (clearTimeoutId !== null) {
      window.clearTimeout(clearTimeoutId);
    }
    clearTimeoutId = null;
    clearPending = false;
    clearFailed = true;
    commandStatus.textContent = "Could not clear waiting speech. Try again.";
    render(currentStatus);
  }
});

requiredElement<HTMLButtonElement>("minimize-button").addEventListener("click", () => {
  void desktopApi?.minimize();
});

requiredElement<HTMLButtonElement>("hide-button").addEventListener("click", () => {
  void desktopApi?.hide();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    cancelQueuePointerDrag();
  } else {
    void refreshStatus();
  }
});

queueList.addEventListener("pointerdown", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) {
    return;
  }
  const handle = target.closest<HTMLButtonElement>(".queue-drag-handle");
  const row = handle?.closest<HTMLElement>(".queue-item.is-upcoming");
  const id = row?.dataset.itemId;
  if (handle && row && id) {
    beginQueuePointerDrag(event, id, row, handle);
  }
});
queueList.addEventListener("scroll", updateTimelineFade, { passive: true });
window.addEventListener("pointermove", updateQueuePointerDrag, true);
window.addEventListener("pointerup", (event) => finishQueuePointerDrag(event, true), true);
window.addEventListener("pointercancel", (event) => finishQueuePointerDrag(event, false), true);
queueList.addEventListener("lostpointercapture", (event) => {
  if (queuePointerDrag?.state.pointerId === event.pointerId) {
    cancelQueuePointerDrag();
  }
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    cancelQueuePointerDrag();
  }
});
window.addEventListener("blur", cancelQueuePointerDrag);

render(currentStatus);
void refreshStatus();
window.setInterval(() => void refreshStatus(), 700);
