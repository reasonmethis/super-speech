import "./styles.css";
import {
  ENGINE_STATUS_VERSION,
  INITIAL_STATUS,
  VOICE_OPTIONS,
  clearRequestWasApplied,
  currentPieceSegments,
  moveQueueItemBefore,
  pendingPlaybackState,
  playAcceptanceState,
  playbackPresentation,
  timelineItems,
  timelineItemsAtBoundary,
  type PlayAcceptance,
  type PlaybackPresentation,
  type RuntimeStatus,
  type TimelineItem,
} from "./runtime";
import {
  isHistoryDropArea,
  pointerMovedBeyondThreshold,
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
    piece_start: 0,
    piece_end: 42,
    elapsed_seconds: 4.2,
  },
  recent_starts: [{ id: "014-af_heart-say", started_at: Date.now() / 1000 }],
  queue_count: 2,
  queue: [
    {
      id: "015-bm_fable-say",
      filename: "015-bm_fable-say.txt",
      text: "Click this speech item to expand it, or double-click to play it now.",
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
const playbackCard = requiredElement<HTMLElement>("playback-card");
const playbackCopy = requiredElement<HTMLDivElement>("playback-copy");
const playbackTitle = requiredElement<HTMLHeadingElement>("playback-title");
const currentText = requiredElement<HTMLParagraphElement>("current-text");
const voicePill = requiredElement<HTMLSpanElement>("voice-pill");
const voiceLabel = requiredElement<HTMLSpanElement>("voice-label");
const metadataRow = requiredElement<HTMLDivElement>("metadata-row");
const clearQueueButton = requiredElement<HTMLButtonElement>("clear-queue-button");
const queueList = requiredElement<HTMLDivElement>("queue-list");
const queueActionMenu = requiredElement<HTMLDivElement>("queue-action-menu");
const commandStatus = requiredElement<HTMLDivElement>("command-status");
const versionLabel = requiredElement<HTMLSpanElement>("version-label");
const ambientRings = [...document.querySelectorAll<HTMLElement>(".ring")];
const playbackBackground = [
  ...document.querySelectorAll<HTMLElement>(".queue-section, footer"),
];
const desktopApi = window.superSpeech;
const themeButtons = [...document.querySelectorAll<HTMLButtonElement>("[data-theme-choice]")];

type Theme = "dark" | "light";

function applyTheme(theme: Theme): void {
  document.body.dataset.theme = theme;
  document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')?.setAttribute(
    "content",
    theme === "light" ? "#f5f6f9" : "#0b0d14",
  );
  for (const button of themeButtons) {
    button.setAttribute("aria-pressed", String(button.dataset.themeChoice === theme));
  }
}

const savedTheme = localStorage.getItem("super-speech-theme");
applyTheme(savedTheme === "light" ? "light" : "dark");
for (const button of themeButtons) {
  button.addEventListener("click", () => {
    const theme = button.dataset.themeChoice === "light" ? "light" : "dark";
    localStorage.setItem("super-speech-theme", theme);
    applyTheme(theme);
  });
}

interface PendingSelection {
  selectedItem: TimelineItem;
  sourceItemId: string;
  state: "playing" | "paused";
  acceptance: PlayAcceptance | null;
  timeoutId: number;
}

type DraggableKind = "upcoming" | "history";

interface QueuePointerDrag {
  state: QueueDragState;
  startX: number;
  startY: number;
  pointerOffsetX: number;
  pointerOffsetY: number;
  width: number;
  height: number;
}

interface ChunkPointerGesture {
  pointerId: number;
  button: HTMLButtonElement;
  startX: number;
  startY: number;
  moved: boolean;
}

interface PendingChunkExpansion {
  itemId: string;
  timeoutId: number;
}

let currentStatus = desktopApi ? INITIAL_STATUS : demoStatus;
let commandPending = false;
let pendingSelection: PendingSelection | null = null;
let failedChunkId: string | null = null;
let clearPending = false;
let clearFailed = false;
let clearBaselineIds = new Set<string>();
let clearRequestedAfter: number | null = null;
let clearTimeoutId: number | null = null;
let expandedItemId: string | null = null;
let renderedTimelineKey: string | null = null;
let pendingQueueMutation: { id: string } | null = null;
let failedQueueMutationId: string | null = null;
let queueMutationGeneration = 0;
let queuePointerDrag: QueuePointerDrag | null = null;
let chunkPointerGesture: ChunkPointerGesture | null = null;
const suppressedChunkClicks = new WeakSet<HTMLButtonElement>();
let pendingChunkExpansion: PendingChunkExpansion | null = null;
let openMenuItemId: string | null = null;
let revealedCurrentItemId: string | null = null;
let ringSettlingAnimations: Animation[] = [];
let playbackExpanded = false;
let lastFollowedPieceKey: string | null = null;

const POINTER_GESTURE_THRESHOLD = 5;
const QUEUE_REORDER_ANIMATION_MS = 140;
const QUEUE_STATUS_CONFIRMATION_MS = 1_500;
const CHUNK_DOUBLE_CLICK_MS = 400;

function selectionBlocksTimelineMutation(): boolean {
  return pendingSelection !== null;
}

function timelineMutationBlocked(): boolean {
  return commandPending || clearPending || pendingQueueMutation !== null ||
    selectionBlocksTimelineMutation();
}

function requiredElement<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing required element #${id}`);
  }
  return element as T;
}

const voiceLabels = new Map<string, string>(
  VOICE_OPTIONS.map(([id, label]) => [id, label]),
);

function formatVoice(voice: string): string {
  return voiceLabels.get(voice) ?? voice.replace(/^[a-z]{2}_/, "").replaceAll("_", " ");
}

function timelineAction(item: TimelineItem, pending: boolean, failed: boolean): string {
  if (failedQueueMutationId === item.id) {
    return "Could not move / Try again";
  }
  if (pending) {
    return pendingSelection?.acceptance ? "Selected / Up next" : "Starting...";
  }
  if (failed) {
    return "Could not start / Try again";
  }
  if (item.kind !== "current") {
    return "";
  }
  const labels: Partial<Record<RuntimeStatus["state"], string>> = {
    loading: "Preparing",
    playing: "Speaking",
    paused: "Paused",
    setup_required: "Setup needed",
    stopped: "Stopped",
  };
  return labels[currentStatus.state] ?? "";
}

function visibleTimelineItems(status = currentStatus): TimelineItem[] {
  return pendingSelection
    ? timelineItemsAtBoundary(
      status,
      pendingSelection.selectedItem,
      pendingSelection.sourceItemId,
    )
    : timelineItems(status);
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

function reconcileCommands(status: RuntimeStatus): void {
  if (pendingSelection) {
    pendingSelection.state = pendingPlaybackState(
      status,
      pendingSelection.acceptance,
      pendingSelection.state,
    );
  }
  const selectionState = pendingSelection?.acceptance
    ? playAcceptanceState(status, pendingSelection.acceptance)
    : "pending";
  if (pendingSelection && selectionState === "applied") {
    window.clearTimeout(pendingSelection.timeoutId);
    pendingSelection = null;
    if (commandStatus.textContent === "Selected speech is up next.") {
      commandStatus.textContent = "";
    }
  }
  if (pendingSelection && selectionState === "failed") {
    window.clearTimeout(pendingSelection.timeoutId);
    failedChunkId = pendingSelection.selectedItem.id;
    pendingSelection = null;
    commandStatus.textContent = "Could not start selected speech. Try again.";
  }
  const clearWasApplied = clearRequestedAfter !== null &&
    clearRequestWasApplied(status, clearBaselineIds, clearRequestedAfter);
  if (clearWasApplied) {
    const restoreFocus = document.activeElement === clearQueueButton;
    if (clearTimeoutId !== null) {
      window.clearTimeout(clearTimeoutId);
    }
    clearTimeoutId = null;
    clearPending = false;
    clearFailed = false;
    clearBaselineIds = new Set();
    clearRequestedAfter = null;
    commandStatus.textContent = "";
    if (restoreFocus) {
      queueList.focus({ preventScroll: true });
    }
  }
}

function chunkActionLabel(item: TimelineItem, expanded: boolean): string {
  return `${expanded ? "Collapse" : "Expand"} full text for ${itemReference(item)}`;
}

function statusCopy(presentation: PlaybackPresentation): {
  label: string;
  title?: string;
  body?: string;
} {
  if (presentation.state === "setup_required") {
    return {
      label: "Install incomplete",
      title: "Speech files are missing",
      body: "Reinstall Super Speech to restore its bundled engine and voices.",
    };
  }
  if (presentation.state === "stopped") {
    return {
      label: "Engine stopped",
      title: "Super Speech could not start",
      body: "Open the runtime folder from the tray and inspect engine.log for details.",
    };
  }
  if (presentation.state === "loading") {
    return {
      label: "Loading",
      title: "Preparing your voice",
      body: "Kokoro is loading locally. This normally takes a few seconds.",
    };
  }
  if (presentation.state === "paused") {
    return {
      label: "Paused",
      title: formatVoice(presentation.item.voice),
      body: presentation.item.text,
    };
  }
  if (presentation.state === "playing") {
    return {
      label: "Speaking",
      title: formatVoice(presentation.item.voice),
      body: presentation.item.text,
    };
  }
  return {
    label: "Ready",
    title: "Ready when you are",
    body: "Your next voice reply will appear here as soon as it starts.",
  };
}

function setPlaybackExpanded(expanded: boolean): void {
  playbackExpanded = expanded && playbackCopy.dataset.expandable === "true";
  if (!playbackExpanded) {
    lastFollowedPieceKey = null;
  }
  playbackCard.classList.toggle("is-expanded", playbackExpanded);
  document.body.classList.toggle("playback-expanded", playbackExpanded);
  for (const element of playbackBackground) {
    element.inert = playbackExpanded;
  }
  if (playbackCopy.dataset.expandable === "true") {
    playbackCopy.setAttribute("aria-expanded", String(playbackExpanded));
  } else {
    playbackCopy.removeAttribute("aria-expanded");
  }
}

function renderCurrentSpeechText(
  fallback: string,
  current: RuntimeStatus["current"],
): void {
  const segments = current ? currentPieceSegments(current) : null;
  currentText.replaceChildren();
  if (!playbackExpanded) {
    currentText.textContent = segments?.current ?? fallback;
    return;
  }
  if (!current || !segments) {
    currentText.textContent = fallback;
    return;
  }
  currentText.append(document.createTextNode(segments.before));
  const active = document.createElement("mark");
  active.className = "current-piece";
  active.textContent = segments.current;
  currentText.append(active, document.createTextNode(segments.after));
  const pieceKey = `${current.id}:${current.piece}`;
  if (pieceKey !== lastFollowedPieceKey) {
    lastFollowedPieceKey = pieceKey;
    requestAnimationFrame(() => {
      active.scrollIntoView({
        block: "center",
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto"
          : "smooth",
      });
    });
  }
}

type PlaybackAction = "pause" | "resume" | "setup" | "inactive";

function playbackAction(state: PlaybackPresentation["state"]): PlaybackAction {
  if (state === "setup_required") {
    return "setup";
  }
  if (state === "paused") {
    return "resume";
  }
  return state === "playing" ? "pause" : "inactive";
}

function playbackIconMarkup(state: PlaybackPresentation["state"]): string {
  if (state === "setup_required") {
    return '<svg viewBox="0 0 32 32"><path d="M16 7v14m-6-5 6 6 6-6M8 25h16"/></svg>';
  }
  if (state === "paused") {
    return '<svg viewBox="0 0 32 32"><path class="solid" d="m8 5 19 11L8 27Z"/></svg>';
  }
  if (state === "playing") {
    return '<svg viewBox="0 0 32 32"><rect class="solid" x="7" y="5" width="7" height="22" rx="2.5"/><rect class="solid" x="18" y="5" width="7" height="22" rx="2.5"/></svg>';
  }
  if (state === "stopped") {
    return '<svg viewBox="0 0 32 32"><path d="M16 8v9m0 6v1"/></svg>';
  }
  return '<img class="idle-icon" src="./icon.svg" alt="">';
}

function setPlaybackState(state: PlaybackPresentation["state"]): void {
  const previous = document.body.dataset.state;
  if (state === "playing") {
    for (const animation of ringSettlingAnimations) {
      animation.cancel();
    }
    ringSettlingAnimations = [];
  }
  if (
    previous !== "playing" ||
    state === "playing" ||
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  ) {
    document.body.dataset.state = state;
    return;
  }

  const snapshots = ambientRings.map((ring) => {
    const style = getComputedStyle(ring);
    return { opacity: style.opacity, transform: style.transform };
  });
  document.body.dataset.state = state;
  ringSettlingAnimations = ambientRings.map((ring, index) => ring.animate(
    [snapshots[index], { opacity: "1", transform: "scale(1)" }],
    { duration: 520, easing: "ease-out" },
  ));
}

function render(status: RuntimeStatus): void {
  reconcileCommands(status);
  currentStatus = status;
  const presentation = playbackPresentation(
    status,
    pendingSelection
      ? { item: pendingSelection.selectedItem, state: pendingSelection.state }
      : null,
  );
  const copy = statusCopy(presentation);
  const action = playbackAction(presentation.state);
  const showPlaybackCopy = copy.title !== undefined;
  setPlaybackState(presentation.state);

  statusDot.className = `status-dot state-${presentation.state}`;
  statusLabel.textContent = copy.label;
  playbackCopy.classList.toggle("is-hidden", !showPlaybackCopy);
  playbackTitle.textContent = copy.title ?? "";
  const followedCurrent = status.current?.id === presentation.item?.id
    ? status.current
    : null;
  const canExpand = followedCurrent !== null;
  playbackCopy.dataset.expandable = String(canExpand);
  playbackCopy.classList.toggle("is-expandable", canExpand);
  if (!canExpand && playbackExpanded) {
    const restoreFocus = playbackCopy.contains(document.activeElement);
    setPlaybackExpanded(false);
    if (restoreFocus) {
      queueList.focus({ preventScroll: true });
    }
  }
  if (canExpand) {
    playbackCopy.tabIndex = 0;
    playbackCopy.setAttribute("role", "button");
    playbackCopy.setAttribute("aria-expanded", String(playbackExpanded));
    playbackCopy.setAttribute("aria-describedby", "playback-title current-text");
    playbackCopy.setAttribute(
      "aria-label",
      playbackExpanded ? "Collapse current speech text" : "Expand current speech text",
    );
  } else {
    playbackCopy.removeAttribute("tabindex");
    playbackCopy.removeAttribute("role");
    playbackCopy.removeAttribute("aria-expanded");
    playbackCopy.removeAttribute("aria-describedby");
    playbackCopy.removeAttribute("aria-label");
  }
  renderCurrentSpeechText(copy.body ?? "", followedCurrent);

  const actionLabels: Record<PlaybackAction, string> = {
    pause: "Pause speech",
    resume: "Resume speech",
    setup: "Open setup guide",
    inactive: presentation.state === "loading" ? "Preparing speech" : "Ready for speech",
  };
  playbackButton.dataset.action = action;
  playbackButton.setAttribute("aria-label", actionLabels[action]);
  playbackButton.disabled = commandPending || pendingQueueMutation !== null ||
    (pendingSelection !== null && pendingSelection.acceptance === null) ||
    action === "inactive";
  playbackButton.setAttribute(
    "aria-busy",
    String(commandPending || pendingSelection !== null || presentation.state === "loading"),
  );
  playbackIcon.innerHTML = playbackIconMarkup(presentation.state);

  const current = presentation.item;
  metadataRow.classList.toggle("is-hidden", !current);
  if (current) {
    voiceLabel.textContent = formatVoice(current.voice);
    voicePill.classList.remove("is-hidden");
  } else {
    voicePill.classList.add("is-hidden");
  }

  const visibleItems = visibleTimelineItems(status);
  const activeCount = visibleItems.filter(({ kind }) => kind !== "history").length;
  clearQueueButton.classList.toggle("is-hidden", activeCount === 0);
  clearQueueButton.disabled = commandPending || clearPending || pendingQueueMutation !== null ||
    (pendingSelection !== null && pendingSelection.acceptance === null);
  clearQueueButton.setAttribute("aria-disabled", String(clearQueueButton.disabled));
  clearQueueButton.setAttribute("aria-busy", String(clearPending));
  clearQueueButton.setAttribute(
    "aria-label",
    clearPending
      ? "Clearing speech"
      : clearFailed
        ? "Retry clearing speech"
        : `Move ${activeCount} active speech ${activeCount === 1 ? "item" : "items"} to History`,
  );
  clearQueueButton.textContent = clearPending
    ? "Clearing..."
    : clearFailed
      ? "Retry clear all"
      : "Clear all";
  const hiddenHistoryCount = Math.max(0, status.history_count - status.history.length);
  const projectedHistoryTotal = hiddenHistoryCount +
    visibleItems.filter(({ kind }) => kind === "history").length;
  renderTimeline(visibleItems, projectedHistoryTotal);
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
  kind: DraggableKind,
): void {
  if (
    event.button !== 0 ||
    !event.isPrimary ||
    timelineMutationBlocked()
  ) {
    return;
  }

  cancelQueuePointerDrag();
  event.preventDefault();
  const bounds = row.getBoundingClientRect();
  const visualOrder = kind === "upcoming"
    ? [...currentStatus.queue].reverse().map(({ id }) => id)
    : currentStatus.history.map(({ id }) => id);
  const state = startQueueDrag(
    event.pointerId,
    id,
    visualOrder,
    kind,
  );
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
    height: bounds.height,
  };
}

function draggableRow(kind: DraggableKind, id: string): HTMLElement | null {
  return [...queueList.querySelectorAll<HTMLElement>(`.queue-item.is-${kind}`)]
    .find((row) => row.dataset.itemId === id) ?? null;
}

function activeDragGhost(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".queue-drag-ghost");
}

function activateQueuePointerDrag(drag: QueuePointerDrag): boolean {
  const row = draggableRow(drag.state.kind, drag.state.sourceId);
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

function beginChunkPointerGesture(
  event: PointerEvent,
  button: HTMLButtonElement,
): void {
  if (
    event.button !== 0 ||
    !event.isPrimary ||
    button.disabled ||
    chunkPointerGesture
  ) {
    return;
  }
  chunkPointerGesture = {
    pointerId: event.pointerId,
    button,
    startX: event.clientX,
    startY: event.clientY,
    moved: false,
  };
}

function suppressNextChunkClick(button: HTMLButtonElement): void {
  suppressedChunkClicks.add(button);
  window.setTimeout(() => suppressedChunkClicks.delete(button), 0);
}

function recordChunkPointerMovement(
  gesture: ChunkPointerGesture,
  event: PointerEvent,
): void {
  gesture.moved ||= pointerMovedBeyondThreshold(
    gesture.startX,
    gesture.startY,
    event.clientX,
    event.clientY,
    POINTER_GESTURE_THRESHOLD,
  );
}

function updateChunkPointerGesture(event: PointerEvent): void {
  const gesture = chunkPointerGesture;
  if (!gesture || event.pointerId !== gesture.pointerId) {
    return;
  }
  recordChunkPointerMovement(gesture, event);
  if ((event.buttons & 1) === 0) {
    if (gesture.moved) {
      suppressNextChunkClick(gesture.button);
    }
    chunkPointerGesture = null;
  }
}

function finishChunkPointerGesture(event: PointerEvent): void {
  const gesture = chunkPointerGesture;
  if (!gesture || event.pointerId !== gesture.pointerId) {
    return;
  }
  recordChunkPointerMovement(gesture, event);
  if (gesture.moved) {
    suppressNextChunkClick(gesture.button);
  }
  chunkPointerGesture = null;
}

function cancelChunkPointerGesture(pointerId?: number): void {
  if (pointerId !== undefined && chunkPointerGesture?.pointerId !== pointerId) {
    return;
  }
  chunkPointerGesture = null;
}

function cancelPendingChunkExpansion(): void {
  if (pendingChunkExpansion) {
    window.clearTimeout(pendingChunkExpansion.timeoutId);
    pendingChunkExpansion = null;
  }
}

function scheduleChunkExpansion(itemId: string): void {
  cancelPendingChunkExpansion();
  const expansion: PendingChunkExpansion = {
    itemId,
    timeoutId: window.setTimeout(() => {
      if (pendingChunkExpansion !== expansion) {
        return;
      }
      pendingChunkExpansion = null;
      const expanding = expandedItemId !== itemId;
      setExpandedItem(expanding ? itemId : null);
      if (expanding) {
        queueList.querySelector<HTMLElement>(
          `[data-item-id="${itemId}"]`,
        )?.scrollIntoView({ block: "nearest" });
      }
    }, CHUNK_DOUBLE_CLICK_MS),
  };
  pendingChunkExpansion = expansion;
}

function applyDragVisualOrder(kind: DraggableKind, visualOrder: readonly string[]): void {
  const rows = [...queueList.querySelectorAll<HTMLElement>(`.queue-item.is-${kind}`)];
  const currentOrder = rows.map((row) => row.dataset.itemId ?? "");
  if (
    visualOrder.length === currentOrder.length &&
    visualOrder.every((id, index) => id === currentOrder[index])
  ) {
    return;
  }
  const visualTops = new Map(rows.map((row) => [row, row.getBoundingClientRect().top]));
  for (const row of rows) {
    for (const animation of row.getAnimations()) {
      if (animation.id === "queue-reorder") {
        animation.cancel();
      }
    }
  }
  const rowsById = new Map(rows.map((row) => [row.dataset.itemId ?? "", row]));
  const anchor = kind === "upcoming"
    ? queueList.querySelector<HTMLElement>(
        '.timeline-divider[data-section="current"], .timeline-divider[data-section="history"]',
      )
    : null;
  for (const id of visualOrder) {
    const row = rowsById.get(id);
    if (row) {
      kind === "upcoming" ? queueList.insertBefore(row, anchor) : queueList.append(row);
    }
  }
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return;
  }
  for (const row of rows) {
    const previousTop = visualTops.get(row);
    if (previousTop === undefined) {
      continue;
    }
    const distance = previousTop - row.getBoundingClientRect().top;
    if (Math.abs(distance) < 0.5) {
      continue;
    }
    const animation = row.animate(
      [
        { transform: `translateY(${distance}px)` },
        { transform: "translateY(0)" },
      ],
      { duration: QUEUE_REORDER_ANIMATION_MS, easing: "ease-out" },
    );
    animation.id = "queue-reorder";
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
    if (!pointerMovedBeyondThreshold(
      drag.startX,
      drag.startY,
      event.clientX,
      event.clientY,
      POINTER_GESTURE_THRESHOLD,
    )) {
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
  const ghostTop = event.clientY - drag.pointerOffsetY;
  ghost.style.left = `${left}px`;
  ghost.style.top = `${ghostTop}px`;

  clearHistoryDropIndicator();
  if (drag.state.kind === "upcoming") {
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
        applyDragVisualOrder(drag.state.kind, transition.visualOrder);
      }
      return;
    }
  }

  const rows = [
    ...queueList.querySelectorAll<HTMLElement>(`.queue-item.is-${drag.state.kind}`),
  ];
  const beforeId = queueDropBeforeId(
    drag.state.sourceId,
    rows.map((row) => {
      const bounds = row.getBoundingClientRect();
      const transform = window.getComputedStyle(row).transform;
      const animatedOffsetY = transform === "none"
        ? 0
        : new DOMMatrixReadOnly(transform).m42;
      return {
        id: row.dataset.itemId ?? "",
        top: bounds.top - animatedOffsetY,
        height: bounds.height,
      };
    }),
    ghostTop + drag.height / 2,
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
    applyDragVisualOrder(drag.state.kind, transition.visualOrder);
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
      applyDragVisualOrder(drag.state.kind, transition.visualOrder);
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
    if (command.kind === "upcoming") {
      void moveWaitingItem(command.id, command.beforeId);
    } else {
      void moveHistoryItem(command.id, command.beforeId);
    }
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
      applyDragVisualOrder(drag.state.kind, transition.visualOrder);
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
  if (index < 0 || timelineMutationBlocked()) {
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

function handleHistoryReorderKey(event: KeyboardEvent, id: string): void {
  const ids = currentStatus.history.map((item) => item.id);
  const index = ids.indexOf(id);
  if (index < 0 || timelineMutationBlocked()) {
    return;
  }
  let beforeId: string | null | undefined;
  if (event.key === "ArrowUp" && index > 0) {
    beforeId = ids[index - 1];
  } else if (event.key === "ArrowDown" && index < ids.length - 1) {
    beforeId = ids[index + 2] ?? null;
  } else if (event.key === "Home" && index > 0) {
    beforeId = ids[0];
  } else if (event.key === "End" && index < ids.length - 1) {
    beforeId = null;
  }
  if (beforeId !== undefined) {
    event.preventDefault();
    void moveHistoryItem(id, beforeId);
  }
}

function timelineRenderKey(items: TimelineItem[], historyTotal: number): string {
  return JSON.stringify([
    historyTotal,
    items.map(({ id, text, voice, kind, position }) => [id, text, voice, kind, position]),
  ]);
}

type TimelineSection = TimelineItem["kind"];

function requiredTimelineSections(items: TimelineItem[]): TimelineSection[] {
  const present = new Set(items.map(({ kind }) => kind));
  if (present.has("upcoming")) {
    present.add("history");
  }
  return (["upcoming", "current", "history"] as const)
    .filter((section) => present.has(section));
}

function reconcileTimelineNodes(items: TimelineItem[]): boolean {
  const rows = [...queueList.querySelectorAll<HTMLElement>(".queue-item")];
  const rowsById = new Map(rows.map((row) => [row.dataset.itemId, row]));
  const dividers = [...queueList.querySelectorAll<HTMLElement>(".timeline-divider")];
  const dividersBySection = new Map(
    dividers.map((divider) => [divider.dataset.section as TimelineSection, divider]),
  );
  const sections = requiredTimelineSections(items);
  if (
    rows.length !== items.length ||
    rowsById.size !== rows.length ||
    dividersBySection.size !== sections.length ||
    sections.some((section) => !dividersBySection.has(section)) ||
    items.some((item) => {
      const row = rowsById.get(item.id);
      return !row ||
        !row.classList.contains(`is-${item.kind}`) ||
        row.dataset.voice !== item.voice ||
        row.querySelector(".queue-copy p")?.textContent !== item.text;
    })
  ) {
    return false;
  }

  const insertedSections = new Set<TimelineSection>();
  let activeSection: TimelineSection | null = null;
  for (const item of items) {
    const section = item.kind;
    if (section !== activeSection) {
      queueList.append(dividersBySection.get(section)!);
      insertedSections.add(section);
      activeSection = section;
    }
    queueList.append(rowsById.get(item.id)!);
  }
  for (const section of sections) {
    if (!insertedSections.has(section)) {
      queueList.append(dividersBySection.get(section)!);
    }
  }
  return true;
}

function setOpenActionMenu(itemId: string | null): void {
  const shouldFocus = itemId !== null && itemId !== openMenuItemId;
  const item = itemId
    ? visibleTimelineItems().find(({ id }) => id === itemId)
    : null;
  const target = item
    ? queueList.querySelector<HTMLElement>(`[data-item-id="${item.id}"]`)
    : null;
  const button = target?.querySelector<HTMLButtonElement>(".queue-menu-button");
  if (itemId) {
    const listBounds = queueList.getBoundingClientRect();
    const targetBounds = target?.getBoundingClientRect();
    if (
      !item ||
      !button ||
      !targetBounds ||
      targetBounds.bottom <= listBounds.top ||
      targetBounds.top >= listBounds.bottom
    ) {
      itemId = null;
    }
  }
  openMenuItemId = itemId;
  for (const row of queueList.querySelectorAll<HTMLElement>(".queue-item")) {
    const open = row.dataset.itemId === itemId;
    row.querySelector<HTMLButtonElement>(".queue-menu-button")
      ?.setAttribute("aria-expanded", String(open));
  }
  queueActionMenu.hidden = !itemId;
  if (!itemId || !item || !button) {
    queueActionMenu.replaceChildren();
    return;
  }

  const actions: HTMLElement[] = [];
  if (item.kind === "current" && ["playing", "paused"].includes(currentStatus.state)) {
    actions.push(createMenuAction(
      currentStatus.state === "paused" ? "Resume" : "Pause",
      () => void runPlaybackAction(),
      "is-playback",
    ));
  } else if (item.kind !== "current" || currentStatus.state === "stopped") {
    actions.push(createMenuAction(
      "Play",
      () => void playTimelineItem(item),
    ));
  }
  actions.push(createVoiceSelect(item));
  actions.push(createMenuAction(
    "Copy text",
    () => void desktopApi?.copyText(item.text),
  ));
  if (item.kind === "upcoming") {
    actions.push(createMenuAction(
      "Delete",
      () => void archiveWaitingItem(item.id),
      "is-delete",
    ));
  } else if (item.kind === "history") {
    actions.push(createMenuAction(
      "Delete",
      () => void deleteHistoryItem(item.id),
      "is-delete",
    ));
  }
  queueActionMenu.replaceChildren(...actions);

  const edgeGap = 8;
  const itemGap = 4;
  const listBounds = queueList.getBoundingClientRect();
  const buttonBounds = button.getBoundingClientRect();
  const availableTop = Math.max(edgeGap, listBounds.top + edgeGap);
  const availableBottom = Math.min(window.innerHeight - edgeGap, listBounds.bottom - edgeGap);
  queueActionMenu.style.maxHeight = `${Math.max(0, availableBottom - availableTop)}px`;
  const menuBounds = queueActionMenu.getBoundingClientRect();
  const availableLeft = Math.max(edgeGap, listBounds.left);
  const availableRight = Math.min(window.innerWidth - edgeGap, listBounds.right);
  const left = Math.min(
    Math.max(availableLeft, buttonBounds.right - menuBounds.width),
    availableRight - menuBounds.width,
  );
  const below = buttonBounds.bottom + itemGap;
  const above = buttonBounds.top - menuBounds.height - itemGap;
  const top = below + menuBounds.height <= availableBottom
    ? below
    : above >= availableTop
      ? above
      : Math.min(Math.max(availableTop, buttonBounds.top), availableBottom - menuBounds.height);
  queueActionMenu.style.left = `${left}px`;
  queueActionMenu.style.top = `${top}px`;
  if (shouldFocus) {
    queueActionMenu.querySelector<HTMLElement>("button, select")?.focus();
  }
}

function closeActionMenu(restoreFocus: boolean): void {
  const button = restoreFocus && openMenuItemId
    ? queueList.querySelector<HTMLButtonElement>(
        `[data-item-id="${openMenuItemId}"] .queue-menu-button`,
      )
    : null;
  setOpenActionMenu(null);
  if (button) {
    button.focus({ preventScroll: true });
  } else if (restoreFocus === false) {
    queueList.focus({ preventScroll: true });
  }
}

function createMenuAction(
  label: string,
  action: () => void,
  className = "",
): HTMLButtonElement {
  const button = document.createElement("button");
  button.className = `queue-menu-action ${className}`.trim();
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", () => {
    closeActionMenu(className !== "is-delete");
    action();
  });
  return button;
}

function createVoiceSelect(item: TimelineItem): HTMLSelectElement {
  const select = document.createElement("select");
  select.className = "queue-menu-voice";
  select.setAttribute("aria-label", `Change voice for ${itemReference(item)}`);
  const prompt = document.createElement("option");
  prompt.value = "";
  prompt.textContent = "Change voice";
  select.append(prompt);
  const groups = new Map<string, HTMLOptGroupElement>();
  for (const [id, label, group] of VOICE_OPTIONS) {
    let options = groups.get(group);
    if (!options) {
      options = document.createElement("optgroup");
      options.label = group;
      groups.set(group, options);
      select.append(options);
    }
    const option = document.createElement("option");
    option.value = id;
    option.textContent = label;
    option.disabled = id === item.voice;
    options.append(option);
  }
  select.addEventListener("change", () => {
    if (!select.value) {
      return;
    }
    const voice = select.value;
    closeActionMenu(true);
    void playTimelineItem(item, voice);
  });
  return select;
}

function updateTimelineDividers(items: TimelineItem[], historyTotal: number): void {
  const waitingCount = items.filter(({ kind }) => kind === "upcoming").length;
  const historyCount = items.filter(({ kind }) => kind === "history").length;
  const currentLabel = pendingSelection
    ? pendingSelection.acceptance
      ? pendingSelection.state === "paused" ? "Paused" : "Playing"
      : "Starting"
    : ({
      loading: "Preparing",
      playing: "Playing",
      paused: "Paused",
      setup_required: "Setup needed",
      stopped: "Stopped",
      idle: "Idle",
    } satisfies Record<RuntimeStatus["state"], string>)[currentStatus.state];
  const labels: Record<TimelineSection, [string, string]> = {
    upcoming: ["Waiting", waitingCount.toLocaleString()],
    current: ["Current", currentLabel],
    history: [
      "History",
      historyCount < historyTotal
        ? `${historyCount.toLocaleString()} recent of ${historyTotal.toLocaleString()}`
        : historyCount.toLocaleString(),
    ],
  };
  for (const divider of queueList.querySelectorAll<HTMLElement>(".timeline-divider")) {
    const section = divider.dataset.section as TimelineSection;
    const [title, count] = labels[section];
    divider.querySelector<HTMLElement>(".timeline-divider-title")!.textContent = title;
    divider.querySelector<HTMLElement>(".timeline-divider-count")!.textContent = count;
  }
}

function createTimelineDivider(section: TimelineSection): HTMLDivElement {
  const divider = document.createElement("div");
  divider.className = `timeline-divider${section === "history" ? " history-drop-target" : ""}`;
  divider.dataset.section = section;
  divider.innerHTML = '<span class="timeline-divider-title"></span><span class="timeline-divider-count"></span>';
  return divider;
}

function revealCurrentItem(): void {
  const currentId = currentStatus.current?.id ?? null;
  if (!currentId) {
    if (currentStatus.state === "idle" && currentStatus.queue_count === 0) {
      revealedCurrentItemId = null;
    }
    return;
  }
  if (currentId === revealedCurrentItemId) {
    return;
  }
  const row = queueList.querySelector<HTMLElement>(`[data-item-id="${currentId}"]`);
  if (row) {
    row.scrollIntoView({ block: "center" });
    revealedCurrentItemId = currentId;
  }
}

function renderTimeline(items: TimelineItem[], historyTotal: number): void {
  const pendingExpansionId = pendingChunkExpansion?.itemId;
  if (
    pendingExpansionId &&
    !items.some((item) => item.id === pendingExpansionId)
  ) {
    cancelPendingChunkExpansion();
  }
  if (!items.some((item) => item.id === expandedItemId)) {
    expandedItemId = null;
  }
  if (!items.some((item) => item.id === failedChunkId)) {
    failedChunkId = null;
  }
  if (
    !items.some((item) => item.id === failedQueueMutationId)
  ) {
    failedQueueMutationId = null;
  }
  // Preserve row nodes across polling so hover, focus, expansion, and in-progress clicks survive
  const timelineKey = timelineRenderKey(items, historyTotal);
  if (timelineKey === renderedTimelineKey || reconcileTimelineNodes(items)) {
    renderedTimelineKey = timelineKey;
    updateTimelineDividers(items, historyTotal);
    updateTimelineRows(items);
    revealCurrentItem();
    return;
  }
  cancelQueuePointerDrag();
  setOpenActionMenu(null);
  renderedTimelineKey = timelineKey;

  const previousScrollTop = queueList.scrollTop;
  const focusedControl = document.activeElement instanceof HTMLElement
    ? document.activeElement.closest<HTMLElement>(".queue-item")
    : null;
  const focusedItemId = focusedControl?.dataset.itemId;
  const focusedControlClass = [
    "queue-drag-handle",
    "queue-chunk",
    "queue-menu-button",
  ].find((className) => document.activeElement?.classList.contains(className));
  queueList.replaceChildren();
  if (items.length === 0) {
    const empty = document.createElement("div");
    empty.className = "queue-empty";
    empty.innerHTML = '<span class="empty-check">&#10003;</span><span>No speech yet</span>';
    queueList.append(empty);
    return;
  }

  const sections = requiredTimelineSections(items);
  const insertedSections = new Set<TimelineSection>();
  for (const item of items) {
    const section = item.kind;
    if (!insertedSections.has(section)) {
      queueList.append(createTimelineDivider(section));
      insertedSections.add(section);
    }

    const row = document.createElement("div");
    row.className = "queue-item";
    row.dataset.itemId = item.id;
    row.dataset.voice = item.voice;
    const isCurrent = item.kind === "current";
    const isUpcoming = item.kind === "upcoming";
    const isExpanded = item.id === expandedItemId;
    const reference = itemReference(item);
    row.classList.add(`is-${item.kind}`);
    if (isCurrent) {
      row.setAttribute("aria-current", "true");
    }

    const rowControls: HTMLElement[] = [];
    if (isUpcoming || item.kind === "history") {
      const dragHandle = document.createElement("button");
      dragHandle.className = "queue-drag-handle";
      dragHandle.type = "button";
      dragHandle.title = item.kind === "history"
        ? "Drag to reorder History"
        : "Drag to reorder waiting speech";
      dragHandle.setAttribute(
        "aria-label",
        `Reorder ${reference}. Drag, or use the arrow keys`,
      );
      dragHandle.innerHTML = '<span aria-hidden="true"></span>';
      dragHandle.addEventListener("keydown", (event) => {
        if (item.kind === "history") {
          handleHistoryReorderKey(event, item.id);
        } else {
          handleQueueReorderKey(event, item.id);
        }
      });
      rowControls.push(dragHandle);
    }

    const chunk = document.createElement("button");
    chunk.className = "queue-chunk";
    chunk.type = "button";

    const copy = document.createElement("div");
    copy.className = "queue-copy";
    const text = document.createElement("p");
    text.textContent = item.text;
    const meta = document.createElement("span");
    meta.className = "queue-meta";
    copy.append(text, meta);
    chunk.append(copy);

    const accessibleText = document.createElement("div");
    accessibleText.id = `speech-full-${item.id}`;
    accessibleText.className = "sr-only queue-full-text";
    accessibleText.hidden = !isExpanded;
    accessibleText.setAttribute("role", "region");
    accessibleText.setAttribute("aria-label", `Full text for ${reference}`);
    accessibleText.textContent = item.text;
    chunk.setAttribute("aria-controls", accessibleText.id);
    chunk.setAttribute("aria-expanded", String(isExpanded));
    chunk.setAttribute("aria-label", chunkActionLabel(item, isExpanded));
    chunk.addEventListener("click", (event) => {
      if (suppressedChunkClicks.delete(chunk)) {
        event.preventDefault();
        return;
      }
      if (event.detail > 1) {
        cancelPendingChunkExpansion();
        return;
      }
      scheduleChunkExpansion(item.id);
    });
    chunk.addEventListener("dblclick", (event) => {
      event.preventDefault();
      cancelPendingChunkExpansion();
      void playTimelineItem(item);
    });

    const actions = document.createElement("div");
    actions.className = "chunk-actions";
    const menuButton = document.createElement("button");
    menuButton.className = "queue-menu-button";
    menuButton.type = "button";
    menuButton.setAttribute("aria-haspopup", "dialog");
    menuButton.setAttribute("aria-expanded", "false");
    menuButton.setAttribute("aria-label", `Actions for ${reference}`);
    menuButton.innerHTML = '<span aria-hidden="true"></span>';
    menuButton.setAttribute("aria-controls", queueActionMenu.id);
    menuButton.addEventListener("click", () => {
      setOpenActionMenu(openMenuItemId === item.id ? null : item.id);
    });
    actions.append(menuButton);
    rowControls.push(chunk, actions, accessibleText);
    row.classList.toggle("is-expanded", isExpanded);
    row.append(...rowControls);
    queueList.append(row);
  }
  for (const section of sections) {
    if (!insertedSections.has(section)) {
      queueList.append(createTimelineDivider(section));
    }
  }
  queueList.scrollTop = previousScrollTop;
  updateTimelineDividers(items, historyTotal);
  updateTimelineRows(items);
  revealCurrentItem();
  if (focusedItemId) {
    const row = queueList.querySelector<HTMLElement>(`[data-item-id="${focusedItemId}"]`);
    const preferred = focusedControlClass
      ? row?.querySelector<HTMLButtonElement>(`.${focusedControlClass}`)
      : row?.querySelector<HTMLButtonElement>(".queue-chunk");
    const fallback = row?.querySelector<HTMLButtonElement>(".queue-menu-button");
    (preferred?.disabled ? fallback : preferred)?.focus({ preventScroll: true });
  }
}

function updateTimelineRows(items: TimelineItem[]): void {
  const itemById = new Map(items.map((item) => [item.id, item]));
  const pendingId = pendingSelection?.selectedItem.id ?? null;
  const commandInFlight = pendingSelection !== null && pendingSelection.acceptance === null;
  const queueCommandInFlight = commandPending || pendingQueueMutation !== null ||
    (pendingSelection !== null && pendingSelection.acceptance === null);
  for (const row of queueList.querySelectorAll<HTMLElement>(".queue-item")) {
    const item = itemById.get(row.dataset.itemId ?? "");
    if (!item) {
      continue;
    }
    const reference = itemReference(item);
    const pending = item.id === pendingId;
    const selected = pending && !commandInFlight;
    const failed = item.id === failedChunkId;
    row.classList.toggle("is-pending", pending);
    row.classList.toggle(
      "is-error",
      failed || failedQueueMutationId === item.id,
    );
    const chunk = row.querySelector<HTMLButtonElement>(".queue-chunk");
    if (chunk) {
      chunk.setAttribute("aria-busy", String(pending && commandInFlight));
      chunk.setAttribute(
        "aria-label",
        selected
          ? `Selected and up next: ${reference}. ${chunkActionLabel(item, row.classList.contains("is-expanded"))}`
          : chunkActionLabel(item, row.classList.contains("is-expanded")),
      );
    }
    const dragHandle = row.querySelector<HTMLButtonElement>(".queue-drag-handle");
    if (dragHandle) {
      dragHandle.disabled = queueCommandInFlight || clearPending ||
        selectionBlocksTimelineMutation();
      dragHandle.setAttribute(
        "aria-label",
        `Reorder ${reference}. Drag, or use the arrow keys`,
      );
    }
    const menuButton = row.querySelector<HTMLButtonElement>(".queue-menu-button");
    if (menuButton) {
      menuButton.disabled = queueCommandInFlight || clearPending;
      menuButton.setAttribute("aria-label", `Actions for ${reference}`);
      menuButton.setAttribute(
        "aria-busy",
        String(pendingQueueMutation?.id === item.id),
      );
    }
    row.querySelector<HTMLElement>(".queue-full-text")?.setAttribute(
      "aria-label",
      `Full text for ${reference}`,
    );
    const meta = row.querySelector<HTMLElement>(".queue-meta");
    if (meta) {
      const action = timelineAction(item, pending, failed);
      meta.textContent = action
        ? `${formatVoice(item.voice)}  /  ${action}`
        : formatVoice(item.voice);
    }
  }
  const playbackMenuAction = queueActionMenu.querySelector<HTMLButtonElement>(
    ".queue-menu-action.is-playback",
  );
  if (playbackMenuAction) {
    playbackMenuAction.textContent = currentStatus.state === "paused" ? "Resume" : "Pause";
  }
  for (const action of queueActionMenu.querySelectorAll<HTMLButtonElement>(
    ".queue-menu-action",
  )) {
    action.disabled = queueCommandInFlight || clearPending ||
      (action.classList.contains("is-delete") &&
        selectionBlocksTimelineMutation());
  }
  const voiceSelect = queueActionMenu.querySelector<HTMLSelectElement>(".queue-menu-voice");
  if (voiceSelect) {
    voiceSelect.disabled = queueCommandInFlight || clearPending;
  }
}

function setExpandedItem(id: string | null): void {
  expandedItemId = id;
  const itemById = new Map(visibleTimelineItems().map((item) => [item.id, item]));
  for (const row of queueList.querySelectorAll<HTMLElement>(".queue-item")) {
    const expanded = row.dataset.itemId === id;
    row.classList.toggle("is-expanded", expanded);
    const chunk = row.querySelector<HTMLButtonElement>(".queue-chunk");
    const item = itemById.get(row.dataset.itemId ?? "");
    chunk?.setAttribute("aria-expanded", String(expanded));
    if (chunk && item) {
      chunk.setAttribute("aria-label", chunkActionLabel(item, expanded));
    }
    const accessibleText = row.querySelector<HTMLElement>(".queue-full-text");
    if (accessibleText) {
      accessibleText.hidden = !expanded;
    }
  }
}

async function playTimelineItem(item: TimelineItem, voice?: string): Promise<void> {
  if (
    (item.kind === "current" && !voice && currentStatus.state !== "stopped") ||
    commandPending ||
    clearPending ||
    pendingQueueMutation !== null ||
    (pendingSelection !== null && pendingSelection.acceptance === null)
  ) {
    return;
  }
  failedChunkId = null;
  const selection: PendingSelection = {
    selectedItem: voice ? { ...item, voice } : item,
    sourceItemId: item.id,
    state: "playing",
    acceptance: null,
    timeoutId: 0,
  };
  selection.timeoutId = window.setTimeout(() => {
    if (pendingSelection === selection) {
      pendingSelection = null;
      failedChunkId = item.id;
      commandStatus.textContent = "Could not start selected speech. Try again.";
      render(currentStatus);
    }
  }, 75_000);
  pendingSelection = selection;
  commandStatus.textContent = "";
  render(currentStatus);
  try {
    if (desktopApi) {
      const acceptance = await desktopApi.playChunk(item.id, voice);
      if (pendingSelection === selection) {
        selection.acceptance = acceptance;
        selection.selectedItem = {
          ...selection.selectedItem,
          id: acceptance.id,
          filename: `${acceptance.id}.txt`,
        };
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
    if (commandResultWasUnconfirmed(error)) {
      failedChunkId = null;
      currentStatus = await statusAfterFailedMutation(currentStatus);
      commandStatus.textContent = "Command result was unconfirmed. Speech state was refreshed.";
      render(currentStatus);
      return;
    }
    failedChunkId = item.id;
    commandStatus.textContent = "Could not start selected speech. Try again.";
    render(currentStatus);
  }
}

async function waitForQueueStatus(
  matches: (status: RuntimeStatus) => boolean,
): Promise<RuntimeStatus | null> {
  if (!desktopApi) {
    return null;
  }
  const deadline = performance.now() + QUEUE_STATUS_CONFIRMATION_MS;
  while (performance.now() < deadline) {
    try {
      const status = await desktopApi.getStatus();
      if (matches(status)) {
        return status;
      }
    } catch {
      // The regular status poll reports persistent read failures
    }
    await new Promise((resolve) => window.setTimeout(resolve, 25));
  }
  return null;
}

async function statusAfterFailedMutation(fallback: RuntimeStatus): Promise<RuntimeStatus> {
  if (!desktopApi) {
    return fallback;
  }
  try {
    return await desktopApi.getStatus();
  } catch {
    return fallback;
  }
}

function commandResultWasUnconfirmed(error: unknown): boolean {
  return error instanceof Error && error.message.includes("result was unconfirmed");
}

async function reconcileFailedTimelineMutation(
  fallback: RuntimeStatus,
  id: string,
  error: unknown,
  failureMessage: string,
  postcondition: (status: RuntimeStatus) => boolean,
): Promise<void> {
  pendingQueueMutation = null;
  currentStatus = await statusAfterFailedMutation(fallback);
  if (postcondition(currentStatus)) {
    failedQueueMutationId = null;
    commandStatus.textContent = "";
  } else if (commandResultWasUnconfirmed(error)) {
    failedQueueMutationId = null;
    commandStatus.textContent = "Command result was unconfirmed. Speech state was refreshed.";
  } else {
    failedQueueMutationId = id;
    commandStatus.textContent = failureMessage;
  }
  render(currentStatus);
}

async function moveWaitingItem(id: string, beforeId: string | null): Promise<void> {
  if (timelineMutationBlocked()) {
    return;
  }
  const reordered = moveQueueItemBefore(currentStatus.queue, id, beforeId);
  const previousIds = currentStatus.queue.map((item) => item.id);
  const expectedIds = reordered.map((item) => item.id);
  if (reordered.every((item, index) => item.id === previousIds[index])) {
    return;
  }
  const previousStatus = currentStatus;
  queueMutationGeneration += 1;
  pendingQueueMutation = { id };
  failedQueueMutationId = null;
  commandStatus.textContent = "";
  currentStatus = { ...currentStatus, queue: reordered };
  render(currentStatus);
  try {
    if (desktopApi) {
      await desktopApi.moveQueueItem(id, beforeId);
      const confirmed = await waitForQueueStatus((status) =>
        status.queue.map((item) => item.id).every(
          (itemId, index) => itemId === expectedIds[index],
        ) && status.queue.length === expectedIds.length
      );
      if (confirmed) {
        currentStatus = confirmed;
      }
    } else {
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    pendingQueueMutation = null;
    render(currentStatus);
    void refreshStatus();
  } catch (error) {
    console.error("Could not reorder waiting speech", error);
    await reconcileFailedTimelineMutation(
      previousStatus,
      id,
      error,
      "Could not reorder waiting speech. Try again.",
      (status) =>
        status.queue.length === expectedIds.length &&
        status.queue.every((item, index) => item.id === expectedIds[index]),
    );
  }
}

async function moveHistoryItem(id: string, beforeId: string | null): Promise<void> {
  if (timelineMutationBlocked()) {
    return;
  }
  const reordered = moveQueueItemBefore(currentStatus.history, id, beforeId);
  const previousIds = currentStatus.history.map((item) => item.id);
  const expectedIds = reordered.map((item) => item.id);
  if (reordered.every((item, index) => item.id === previousIds[index])) {
    return;
  }
  const previousStatus = currentStatus;
  queueMutationGeneration += 1;
  pendingQueueMutation = { id };
  failedQueueMutationId = null;
  commandStatus.textContent = "";
  currentStatus = { ...currentStatus, history: reordered };
  render(currentStatus);
  try {
    if (desktopApi) {
      await desktopApi.moveHistoryItem(id, beforeId);
      const confirmed = await waitForQueueStatus((status) =>
        status.history.length === expectedIds.length &&
        status.history.every((item, index) => item.id === expectedIds[index])
      );
      if (confirmed) {
        currentStatus = confirmed;
      }
    } else {
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    pendingQueueMutation = null;
    render(currentStatus);
    void refreshStatus();
  } catch (error) {
    console.error("Could not reorder History speech", error);
    await reconcileFailedTimelineMutation(
      previousStatus,
      id,
      error,
      "Could not reorder History speech. Try again.",
      (status) =>
        status.history.length === expectedIds.length &&
        status.history.every((item, index) => item.id === expectedIds[index]),
    );
  }
}

async function archiveWaitingItem(id: string): Promise<void> {
  if (timelineMutationBlocked()) {
    return;
  }
  const item = currentStatus.queue.find((queued) => queued.id === id);
  if (!item) {
    return;
  }
  const previousStatus = currentStatus;
  const alreadyInHistory = currentStatus.history.some((entry) => entry.id === id);
  queueMutationGeneration += 1;
  pendingQueueMutation = { id };
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
      const confirmed = await waitForQueueStatus((status) =>
        !status.queue.some((item) => item.id === id) &&
        status.history.some((item) => item.id === id)
      );
      if (confirmed) {
        currentStatus = confirmed;
      }
    } else {
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    pendingQueueMutation = null;
    render(currentStatus);
    void refreshStatus();
  } catch (error) {
    console.error("Could not move waiting speech to History", error);
    await reconcileFailedTimelineMutation(
      previousStatus,
      id,
      error,
      "Could not move waiting speech to History. Try again.",
      (status) =>
        !status.queue.some((entry) => entry.id === id) &&
        status.history.some((entry) => entry.id === id),
    );
  }
}

async function deleteHistoryItem(id: string): Promise<void> {
  if (timelineMutationBlocked()) {
    return;
  }
  if (!currentStatus.history.some((item) => item.id === id)) {
    return;
  }
  const previousStatus = currentStatus;
  queueMutationGeneration += 1;
  pendingQueueMutation = { id };
  failedQueueMutationId = null;
  commandStatus.textContent = "";
  currentStatus = {
    ...currentStatus,
    history_count: Math.max(0, currentStatus.history_count - 1),
    history: currentStatus.history.filter((item) => item.id !== id),
  };
  render(currentStatus);
  try {
    if (desktopApi) {
      await desktopApi.deleteHistoryItem(id);
      const confirmed = await waitForQueueStatus((status) =>
        !status.history.some((item) => item.id === id) &&
        status.history_count < previousStatus.history_count
      );
      if (confirmed) {
        currentStatus = confirmed;
      }
    } else {
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    pendingQueueMutation = null;
    render(currentStatus);
    void refreshStatus();
  } catch (error) {
    console.error("Could not delete speech from History", error);
    await reconcileFailedTimelineMutation(
      previousStatus,
      id,
      error,
      "Could not delete speech from History. Try again.",
      () => false,
    );
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

async function runPlaybackAction(): Promise<void> {
  const action = playbackAction(
    playbackPresentation(
      currentStatus,
      pendingSelection
        ? { item: pendingSelection.selectedItem, state: pendingSelection.state }
        : null,
    ).state,
  );
  if (commandPending || pendingQueueMutation || queuePointerDrag || action === "inactive") {
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
  const previousSelectionState = pendingSelection?.state ?? null;
  if (pendingSelection) {
    pendingSelection.state = paused ? "paused" : "playing";
    render(currentStatus);
  }
  try {
    if (desktopApi) {
      render(await desktopApi.setPaused(paused));
    } else {
      render({ ...currentStatus, state: paused ? "paused" : "playing" });
    }
  } catch (error) {
    console.error("Could not change playback state", error);
    if (pendingSelection && previousSelectionState) {
      pendingSelection.state = previousSelectionState;
    }
  } finally {
    commandPending = false;
    render(currentStatus);
  }
}

playbackButton.addEventListener("click", () => void runPlaybackAction());

playbackCopy.addEventListener("click", () => {
  if (playbackCopy.dataset.expandable === "true") {
    setPlaybackExpanded(!playbackExpanded);
    render(currentStatus);
  }
});
playbackCopy.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    playbackCopy.click();
  }
});

clearQueueButton.addEventListener("click", async () => {
  const projectedActiveItems = visibleTimelineItems().filter(
    ({ kind }) => kind !== "history",
  );
  if (
    commandPending ||
    clearPending ||
    pendingQueueMutation ||
    (pendingSelection !== null && pendingSelection.acceptance === null) ||
    projectedActiveItems.length === 0
  ) {
    return;
  }
  const clearRequestedAt = Date.now() / 1000;
  const projectedActiveIds = new Set(projectedActiveItems.map(({ id }) => id));
  const selectionAcceptedAt = pendingSelection?.acceptance?.acceptedAt ?? 0;
  if (pendingSelection) {
    window.clearTimeout(pendingSelection.timeoutId);
    pendingSelection = null;
  }
  clearPending = true;
  clearFailed = false;
  clearBaselineIds = projectedActiveIds;
  clearRequestedAfter = Math.max(
    clearRequestedAt,
    currentStatus.updated_at,
    selectionAcceptedAt,
  );
  commandStatus.textContent = "";
  clearTimeoutId = window.setTimeout(() => {
    clearTimeoutId = null;
    clearPending = false;
    clearFailed = true;
    commandStatus.textContent = "Could not clear speech. Try again.";
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
      clearBaselineIds = new Set();
      clearRequestedAfter = null;
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
    clearBaselineIds = new Set();
    clearRequestedAfter = null;
    commandStatus.textContent = "Could not clear speech. Try again.";
    render(currentStatus);
  }
});

requiredElement<HTMLButtonElement>("minimize-button").addEventListener("click", () => {
  void desktopApi?.minimize();
});

const maximizeButton = requiredElement<HTMLButtonElement>("maximize-button");
function renderMaximizedState(maximized: boolean): void {
  document.body.classList.toggle("is-maximized", maximized);
  maximizeButton.classList.toggle("is-maximized", maximized);
  maximizeButton.setAttribute("aria-label", maximized ? "Restore" : "Maximize");
}

maximizeButton.addEventListener("click", () => {
  void desktopApi?.toggleMaximize();
});
desktopApi?.onMaximizedChange(renderMaximizedState);

requiredElement<HTMLButtonElement>("hide-button").addEventListener("click", () => {
  void desktopApi?.hide();
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    cancelQueuePointerDrag();
    cancelChunkPointerGesture();
    cancelPendingChunkExpansion();
    setOpenActionMenu(null);
  } else {
    void refreshStatus();
  }
});
window.addEventListener("focus", () => void refreshStatus());

queueList.addEventListener("pointerdown", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) {
    return;
  }
  const handle = target.closest<HTMLButtonElement>(".queue-drag-handle");
  const row = handle?.closest<HTMLElement>(
    ".queue-item.is-upcoming, .queue-item.is-history",
  );
  const id = row?.dataset.itemId;
  if (handle && row && id) {
    beginQueuePointerDrag(
      event,
      id,
      row,
      handle,
      row.classList.contains("is-history") ? "history" : "upcoming",
    );
    return;
  }
  const chunk = target.closest<HTMLButtonElement>(".queue-chunk");
  if (chunk && queueList.contains(chunk)) {
    beginChunkPointerGesture(event, chunk);
  }
});
queueList.addEventListener("scroll", () => {
  if (openMenuItemId) {
    setOpenActionMenu(openMenuItemId);
  }
}, { passive: true });
window.addEventListener("pointermove", updateQueuePointerDrag, true);
window.addEventListener("pointermove", updateChunkPointerGesture, true);
window.addEventListener("pointerup", (event) => {
  finishQueuePointerDrag(event, true);
  finishChunkPointerGesture(event);
}, true);
window.addEventListener("pointercancel", (event) => {
  finishQueuePointerDrag(event, false);
  cancelChunkPointerGesture(event.pointerId);
}, true);
queueList.addEventListener("lostpointercapture", (event) => {
  if (queuePointerDrag?.state.pointerId === event.pointerId) {
    cancelQueuePointerDrag();
  }
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    cancelQueuePointerDrag();
    closeActionMenu(true);
    if (playbackExpanded) {
      setPlaybackExpanded(false);
      playbackCopy.focus({ preventScroll: true });
      render(currentStatus);
    }
  }
});
document.addEventListener("pointerdown", (event) => {
  if (
    openMenuItemId &&
    event.target instanceof Element &&
    !event.target.closest(".chunk-actions, #queue-action-menu")
  ) {
    setOpenActionMenu(null);
  }
}, true);
window.addEventListener("blur", () => {
  cancelQueuePointerDrag();
  cancelChunkPointerGesture();
  cancelPendingChunkExpansion();
  setOpenActionMenu(null);
});

render(currentStatus);
if (desktopApi) {
  void desktopApi.getVersions().then(({ app, engine }) => {
    versionLabel.textContent = `App ${app} | Engine ${engine}`;
  }).catch(() => {
    versionLabel.textContent = "App unavailable | Engine unavailable";
  });
} else {
  versionLabel.textContent = "App demo | Engine demo";
}
void refreshStatus();
window.setInterval(() => void refreshStatus(), 700);
