import "./styles.css";
import {
  ENGINE_STATUS_VERSION,
  INITIAL_STATUS,
  VOICE_OPTIONS,
  adoptTimelineSnapshot,
  currentPieceSegments,
  moveSpeechicleItemBefore,
  playbackPresentation,
  timelineItems,
  type PlaybackPresentation,
  type RuntimeStatus,
  type TimelineItem,
  type TimelineMutation,
} from "./runtime";
import {
  isHistoryDropArea,
  pointerMovedBeyondThreshold,
  sectionDropBeforeId,
  startTimelineDrag,
  transitionTimelineDrag,
  type TimelineDragState,
} from "./timeline-drag-model";

const demoStatus: RuntimeStatus = {
  version: ENGINE_STATUS_VERSION,
  timeline_revision: 1,
  state: "playing",
  updated_at: Date.now() / 1000,
  engine_pid: 4821,
  engine_running: true,
  installed: true,
  current: {
    id: "sp_00000000000000000000000000000014",
    text: "The first desktop version is taking shape. You can pause it without losing your place.",
    voice: "af_heart",
    piece: 1,
    piece_count: 2,
    piece_start: 0,
    piece_end: 42,
    elapsed_seconds: 4.2,
  },
  queue_count: 2,
  queue: [
    {
      id: "sp_00000000000000000000000000000015",
      text: "Click this speech item to expand it, or double-click to play it now.",
      voice: "bm_fable",
    },
    {
      id: "sp_00000000000000000000000000000016",
      text: "Every voice and source app will be easy to spot at a glance.",
      voice: "af_bella",
    },
  ],
  history_count: 2,
  history: [
    {
      id: "sp_00000000000000000000000000000013",
      text: "Earlier speech stays available here whenever you want to hear it again.",
      voice: "bm_george",
    },
    {
      id: "sp_00000000000000000000000000000012",
      text: "The app keeps waiting speech intact when you choose something else.",
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
const speechicleList = requiredElement<HTMLDivElement>("speechicle-list");
const queueActionMenu = requiredElement<HTMLDivElement>("queue-action-menu");
const commandStatus = requiredElement<HTMLDivElement>("command-status");
commandStatus.removeAttribute("role");
commandStatus.removeAttribute("aria-live");
const versionLabel = requiredElement<HTMLSpanElement>("version-label");
const ambientRings = [...document.querySelectorAll<HTMLElement>(".ring")];
const playbackBackground = [
  ...document.querySelectorAll<HTMLElement>(".timeline-section, footer"),
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

type PlayMutation = Extract<TimelineMutation, { type: "play" }>;
type RowMutation = Extract<
  TimelineMutation,
  { type: "move" | "archive" | "delete" }
>;
type ClearMutation = Extract<TimelineMutation, { type: "clear" }>;

type PendingTimelineMutation =
  | {
    kind: "play";
    mutation: PlayMutation;
    item: TimelineItem;
    playbackState: "playing" | "paused";
  }
  | { kind: "row"; mutation: RowMutation; item: TimelineItem }
  | { kind: "clear"; mutation: ClearMutation };

interface TimelineMutationFailure {
  id: string | null;
  type: TimelineMutation["type"];
}

type DraggableKind = "waiting" | "history";

interface TimelinePointerDrag {
  state: TimelineDragState;
  startX: number;
  startY: number;
  pointerOffsetX: number;
  pointerOffsetY: number;
  width: number;
  height: number;
}

interface SpeechiclePointerGesture {
  pointerId: number;
  button: HTMLButtonElement;
  startX: number;
  startY: number;
  moved: boolean;
}

interface PendingSpeechicleExpansion {
  itemId: string;
  timeoutId: number;
}

let currentStatus = desktopApi ? INITIAL_STATUS : demoStatus;
let commandPending = false;
let pendingTimelineMutation: PendingTimelineMutation | null = null;
let failedTimelineMutation: TimelineMutationFailure | null = null;
let expandedItemId: string | null = null;
let renderedTimelineKey: string | null = null;
let timelinePointerDrag: TimelinePointerDrag | null = null;
let speechiclePointerGesture: SpeechiclePointerGesture | null = null;
const suppressedSpeechicleClicks = new WeakSet<HTMLButtonElement>();
let pendingSpeechicleExpansion: PendingSpeechicleExpansion | null = null;
let openMenuItemId: string | null = null;
let revealedCurrentItemId: string | null = null;
let ringSettlingAnimations: Animation[] = [];
let playbackExpanded = false;
let lastFollowedPieceKey: string | null = null;

const POINTER_GESTURE_THRESHOLD = 5;
const QUEUE_REORDER_ANIMATION_MS = 140;
const CHUNK_DOUBLE_CLICK_MS = 400;

function timelineMutationBlocked(): boolean {
  return commandPending || pendingTimelineMutation !== null;
}

function pendingPlaybackForPresentation(): {
  item: TimelineItem;
  state: "playing" | "paused";
} | null {
  const pending = pendingTimelineMutation;
  return pending?.kind === "play"
    ? { item: pending.item, state: pending.playbackState }
    : null;
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

function timelineAction(
  item: TimelineItem,
  pending: PendingTimelineMutation | null,
  failed: boolean,
): string {
  if (pending) {
    const pendingLabels: Partial<Record<TimelineMutation["type"], string>> = {
      play: "Starting...",
      move: "Moving...",
      archive: "Moving...",
      delete: "Deleting...",
    };
    return pendingLabels[pending.mutation.type] ?? "Working...";
  }
  if (failed) {
    return failedTimelineMutation?.type === "play"
      ? "Could not start / Try again"
      : "Could not update / Try again";
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
  return timelineItems(status);
}

function itemReference(
  item: TimelineItem,
  waitingPosition = item.position,
): string {
  const summary = item.text.trim().slice(0, 40);
  if (item.kind === "current") {
    return "current speech";
  }
  if (item.kind === "waiting") {
    return `waiting speech ${waitingPosition}, ${summary}`;
  }
  return `history speech, ${summary}`;
}

function speechicleActionLabel(
  item: TimelineItem,
  expanded: boolean,
  reference = itemReference(item),
): string {
  return `${expanded ? "Collapse" : "Expand"} full text for ${reference}`;
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
  if (status.state !== currentStatus.state) {
    commandStatus.textContent = "";
  }
  currentStatus = status;
  const presentation = playbackPresentation(
    status,
    pendingPlaybackForPresentation(),
  );
  const copy = statusCopy(presentation);
  const action = playbackAction(presentation.state);
  const showPlaybackCopy = copy.title !== undefined;
  setPlaybackState(presentation.state);

  statusDot.className = `status-dot state-${presentation.state}`;
  statusLabel.textContent = commandStatus.textContent || copy.label;
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
      speechicleList.focus({ preventScroll: true });
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
  const nonPlaybackMutationPending = pendingTimelineMutation !== null &&
    pendingTimelineMutation.kind !== "play";
  playbackButton.disabled = commandPending || nonPlaybackMutationPending ||
    action === "inactive";
  playbackButton.setAttribute(
    "aria-busy",
    String(commandPending || pendingTimelineMutation !== null ||
      presentation.state === "loading"),
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
  const clearPending = pendingTimelineMutation?.kind === "clear";
  const clearFailed = failedTimelineMutation?.type === "clear";
  clearQueueButton.classList.toggle("is-hidden", activeCount === 0);
  clearQueueButton.disabled = commandPending || pendingTimelineMutation !== null;
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
  for (const element of speechicleList.querySelectorAll(".is-history-drop")) {
    element.classList.remove("is-history-drop");
  }
}

function beginTimelinePointerDrag(
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

  cancelTimelinePointerDrag();
  event.preventDefault();
  const bounds = row.getBoundingClientRect();
  const visualOrder = kind === "waiting"
    ? [...currentStatus.queue].reverse().map(({ id }) => id)
    : currentStatus.history.map(({ id }) => id);
  const state = startTimelineDrag(
    event.pointerId,
    id,
    visualOrder,
    kind,
  );
  if (!state) {
    return;
  }
  handle.focus({ preventScroll: true });
  speechicleList.setPointerCapture(event.pointerId);
  timelinePointerDrag = {
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
  return [...speechicleList.querySelectorAll<HTMLElement>(`.speechicle-item.is-${kind}`)]
    .find((row) => row.dataset.itemId === id) ?? null;
}

function activeDragGhost(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".timeline-drag-ghost");
}

function activateTimelinePointerDrag(drag: TimelinePointerDrag): boolean {
  const row = draggableRow(drag.state.kind, drag.state.sourceId);
  if (!row) {
    return false;
  }
  const bounds = row.getBoundingClientRect();
  const ghost = row.cloneNode(true) as HTMLElement;
  ghost.classList.add("timeline-drag-ghost");
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
  speechicleList.classList.add("is-timeline-dragging");
  row.classList.add("is-drag-source");
  return true;
}

function clearTimelineDragVisuals(): void {
  for (const ghost of document.querySelectorAll(".timeline-drag-ghost")) {
    ghost.remove();
  }
  for (const row of speechicleList.querySelectorAll(".is-drag-source")) {
    row.classList.remove("is-drag-source");
  }
  speechicleList.classList.remove("is-timeline-dragging");
  clearHistoryDropIndicator();
}

function releaseQueuePointer(pointerId: number): void {
  try {
    if (speechicleList.hasPointerCapture(pointerId)) {
      speechicleList.releasePointerCapture(pointerId);
    }
  } finally {
    clearTimelineDragVisuals();
  }
}

function beginSpeechiclePointerGesture(
  event: PointerEvent,
  button: HTMLButtonElement,
): void {
  if (
    event.button !== 0 ||
    !event.isPrimary ||
    button.disabled ||
    speechiclePointerGesture
  ) {
    return;
  }
  speechiclePointerGesture = {
    pointerId: event.pointerId,
    button,
    startX: event.clientX,
    startY: event.clientY,
    moved: false,
  };
}

function suppressNextSpeechicleClick(button: HTMLButtonElement): void {
  suppressedSpeechicleClicks.add(button);
  window.setTimeout(() => suppressedSpeechicleClicks.delete(button), 0);
}

function recordSpeechiclePointerMovement(
  gesture: SpeechiclePointerGesture,
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

function updateSpeechiclePointerGesture(event: PointerEvent): void {
  const gesture = speechiclePointerGesture;
  if (!gesture || event.pointerId !== gesture.pointerId) {
    return;
  }
  recordSpeechiclePointerMovement(gesture, event);
  if ((event.buttons & 1) === 0) {
    if (gesture.moved) {
      suppressNextSpeechicleClick(gesture.button);
    }
    speechiclePointerGesture = null;
  }
}

function finishSpeechiclePointerGesture(event: PointerEvent): void {
  const gesture = speechiclePointerGesture;
  if (!gesture || event.pointerId !== gesture.pointerId) {
    return;
  }
  recordSpeechiclePointerMovement(gesture, event);
  if (gesture.moved) {
    suppressNextSpeechicleClick(gesture.button);
  }
  speechiclePointerGesture = null;
}

function cancelSpeechiclePointerGesture(pointerId?: number): void {
  if (pointerId !== undefined && speechiclePointerGesture?.pointerId !== pointerId) {
    return;
  }
  speechiclePointerGesture = null;
}

function cancelPendingSpeechicleExpansion(): void {
  if (pendingSpeechicleExpansion) {
    window.clearTimeout(pendingSpeechicleExpansion.timeoutId);
    pendingSpeechicleExpansion = null;
  }
}

function scheduleSpeechicleExpansion(itemId: string): void {
  cancelPendingSpeechicleExpansion();
  const expansion: PendingSpeechicleExpansion = {
    itemId,
    timeoutId: window.setTimeout(() => {
      if (pendingSpeechicleExpansion !== expansion) {
        return;
      }
      pendingSpeechicleExpansion = null;
      const expanding = expandedItemId !== itemId;
      setExpandedItem(expanding ? itemId : null);
      if (expanding) {
        speechicleList.querySelector<HTMLElement>(
          `[data-item-id="${itemId}"]`,
        )?.scrollIntoView({ block: "nearest" });
      }
    }, CHUNK_DOUBLE_CLICK_MS),
  };
  pendingSpeechicleExpansion = expansion;
}

function applyDragVisualOrder(kind: DraggableKind, visualOrder: readonly string[]): void {
  const rows = [...speechicleList.querySelectorAll<HTMLElement>(`.speechicle-item.is-${kind}`)];
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
  const anchor = kind === "waiting"
    ? speechicleList.querySelector<HTMLElement>(
        '.timeline-divider[data-section="current"], .timeline-divider[data-section="history"]',
      )
    : null;
  for (const id of visualOrder) {
    const row = rowsById.get(id);
    if (row) {
      kind === "waiting" ? speechicleList.insertBefore(row, anchor) : speechicleList.append(row);
    }
  }
  updateTimelineRows(visibleTimelineItems());
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

function updateTimelinePointerDrag(event: PointerEvent): void {
  const drag = timelinePointerDrag;
  if (!drag || event.pointerId !== drag.state.pointerId) {
    return;
  }
  if ((event.buttons & 1) === 0) {
    cancelTimelinePointerDrag();
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
    if (!activateTimelinePointerDrag(drag)) {
      cancelTimelinePointerDrag();
      return;
    }
  }

  const listBounds = speechicleList.getBoundingClientRect();
  const left = Math.min(
    Math.max(event.clientX - drag.pointerOffsetX, listBounds.left),
    listBounds.right - drag.width,
  );
  const ghost = activeDragGhost();
  if (!ghost) {
    cancelTimelinePointerDrag();
    return;
  }
  const ghostTop = event.clientY - drag.pointerOffsetY;
  ghost.style.left = `${left}px`;
  ghost.style.top = `${ghostTop}px`;

  clearHistoryDropIndicator();
  if (drag.state.kind === "waiting") {
    const pointed = document.elementFromPoint(event.clientX, event.clientY);
    const historyTarget = pointed instanceof Element
      ? pointed.closest<HTMLElement>(".history-drop-target, .speechicle-item.is-history")
      : null;
    const historyDivider = speechicleList.querySelector<HTMLElement>(
      ".timeline-divider.history-drop-target",
    );
    const overHistory = historyTarget && speechicleList.contains(historyTarget)
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
      const transition = transitionTimelineDrag(drag.state, {
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
    ...speechicleList.querySelectorAll<HTMLElement>(`.speechicle-item.is-${drag.state.kind}`),
  ];
  const beforeId = sectionDropBeforeId(
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
  const transition = transitionTimelineDrag(drag.state, {
    type: "preview-section",
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

function finishTimelinePointerDrag(event: PointerEvent, commit: boolean): void {
  const drag = timelinePointerDrag;
  if (!drag || event.pointerId !== drag.state.pointerId) {
    return;
  }
  const transition = transitionTimelineDrag(drag.state, {
    type: "finish",
    pointerId: event.pointerId,
    commit,
  });
  timelinePointerDrag = null;
  let command = transition.command;
  let projectionFailed = false;
  try {
    if (transition.visualOrder) {
      applyDragVisualOrder(drag.state.kind, transition.visualOrder);
    }
  } catch (error) {
    console.error("Could not settle timeline drag", error);
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
    if (command.kind === "waiting") {
      void moveWaitingItem(command.id, command.beforeId);
    } else {
      void moveHistoryItem(command.id, command.beforeId);
    }
  }
}

function cancelTimelinePointerDrag(): void {
  const drag = timelinePointerDrag;
  if (!drag) {
    clearTimelineDragVisuals();
    return;
  }
  const transition = transitionTimelineDrag(drag.state, { type: "cancel" });
  timelinePointerDrag = null;
  let projectionFailed = false;
  try {
    if (transition.visualOrder) {
      applyDragVisualOrder(drag.state.kind, transition.visualOrder);
    }
  } catch (error) {
    console.error("Could not cancel timeline drag", error);
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
  if (present.has("waiting")) {
    present.add("history");
  }
  return (["waiting", "current", "history"] as const)
    .filter((section) => present.has(section));
}

function reconcileTimelineNodes(items: TimelineItem[]): boolean {
  const rows = [...speechicleList.querySelectorAll<HTMLElement>(".speechicle-item")];
  const rowsById = new Map(rows.map((row) => [row.dataset.itemId, row]));
  const dividers = [...speechicleList.querySelectorAll<HTMLElement>(".timeline-divider")];
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
      speechicleList.append(dividersBySection.get(section)!);
      insertedSections.add(section);
      activeSection = section;
    }
    speechicleList.append(rowsById.get(item.id)!);
  }
  for (const section of sections) {
    if (!insertedSections.has(section)) {
      speechicleList.append(dividersBySection.get(section)!);
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
    ? speechicleList.querySelector<HTMLElement>(`[data-item-id="${item.id}"]`)
    : null;
  const button = target?.querySelector<HTMLButtonElement>(".queue-menu-button");
  if (itemId) {
    const listBounds = speechicleList.getBoundingClientRect();
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
  for (const row of speechicleList.querySelectorAll<HTMLElement>(".speechicle-item")) {
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
  if (item.kind === "waiting") {
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
  const listBounds = speechicleList.getBoundingClientRect();
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
    ? speechicleList.querySelector<HTMLButtonElement>(
        `[data-item-id="${openMenuItemId}"] .queue-menu-button`,
      )
    : null;
  setOpenActionMenu(null);
  if (button) {
    button.focus({ preventScroll: true });
  } else if (restoreFocus === false) {
    speechicleList.focus({ preventScroll: true });
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
  const waitingCount = items.filter(({ kind }) => kind === "waiting").length;
  const historyCount = items.filter(({ kind }) => kind === "history").length;
  const currentLabel = ({
      loading: "Preparing",
      playing: "Playing",
      paused: "Paused",
      setup_required: "Setup needed",
      stopped: "Stopped",
      idle: "Idle",
    } satisfies Record<RuntimeStatus["state"], string>)[currentStatus.state];
  const labels: Record<TimelineSection, [string, string]> = {
    waiting: ["Waiting", waitingCount.toLocaleString()],
    current: ["Current", currentLabel],
    history: [
      "History",
      historyCount < historyTotal
        ? `${historyCount.toLocaleString()} recent of ${historyTotal.toLocaleString()}`
        : historyCount.toLocaleString(),
    ],
  };
  for (const divider of speechicleList.querySelectorAll<HTMLElement>(".timeline-divider")) {
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
  const row = speechicleList.querySelector<HTMLElement>(`[data-item-id="${currentId}"]`);
  if (row) {
    row.scrollIntoView({ block: "center" });
    revealedCurrentItemId = currentId;
  }
}

function renderTimeline(items: TimelineItem[], historyTotal: number): void {
  const pendingExpansionId = pendingSpeechicleExpansion?.itemId;
  if (
    pendingExpansionId &&
    !items.some((item) => item.id === pendingExpansionId)
  ) {
    cancelPendingSpeechicleExpansion();
  }
  if (!items.some((item) => item.id === expandedItemId)) {
    expandedItemId = null;
  }
  if (
    failedTimelineMutation?.id &&
    !items.some((item) => item.id === failedTimelineMutation?.id)
  ) {
    failedTimelineMutation = null;
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
  cancelTimelinePointerDrag();
  setOpenActionMenu(null);
  renderedTimelineKey = timelineKey;

  const previousScrollTop = speechicleList.scrollTop;
  const focusedControl = document.activeElement instanceof HTMLElement
    ? document.activeElement.closest<HTMLElement>(".speechicle-item")
    : null;
  const focusedItemId = focusedControl?.dataset.itemId;
  const focusedControlClass = [
    "timeline-drag-handle",
    "speechicle-content",
    "queue-menu-button",
  ].find((className) => document.activeElement?.classList.contains(className));
  speechicleList.replaceChildren();
  if (items.length === 0) {
    const empty = document.createElement("div");
    empty.className = "queue-empty";
    empty.innerHTML = '<span class="empty-check">&#10003;</span><span>No speech yet</span>';
    speechicleList.append(empty);
    return;
  }

  const sections = requiredTimelineSections(items);
  const insertedSections = new Set<TimelineSection>();
  for (const item of items) {
    const section = item.kind;
    if (!insertedSections.has(section)) {
      speechicleList.append(createTimelineDivider(section));
      insertedSections.add(section);
    }

    const row = document.createElement("div");
    row.className = "speechicle-item";
    row.dataset.itemId = item.id;
    row.dataset.voice = item.voice;
    const isCurrent = item.kind === "current";
    const isWaiting = item.kind === "waiting";
    const isExpanded = item.id === expandedItemId;
    const reference = itemReference(item);
    row.classList.add(`is-${item.kind}`);
    if (isCurrent) {
      row.setAttribute("aria-current", "true");
    }

    const rowControls: HTMLElement[] = [];
    if (isWaiting || item.kind === "history") {
      const dragHandle = document.createElement("button");
      dragHandle.className = "timeline-drag-handle";
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

    const speechicle = document.createElement("button");
    speechicle.className = "speechicle-content";
    speechicle.type = "button";

    const copy = document.createElement("div");
    copy.className = "queue-copy";
    const text = document.createElement("p");
    text.textContent = item.text;
    const meta = document.createElement("span");
    meta.className = "queue-meta";
    copy.append(text, meta);
    speechicle.append(copy);

    const accessibleText = document.createElement("div");
    accessibleText.id = `speech-full-${item.id}`;
    accessibleText.className = "sr-only queue-full-text";
    accessibleText.hidden = !isExpanded;
    accessibleText.setAttribute("role", "region");
    accessibleText.setAttribute("aria-label", `Full text for ${reference}`);
    accessibleText.textContent = item.text;
    speechicle.setAttribute("aria-controls", accessibleText.id);
    speechicle.setAttribute("aria-expanded", String(isExpanded));
    speechicle.setAttribute("aria-label", speechicleActionLabel(item, isExpanded));
    speechicle.addEventListener("click", (event) => {
      if (suppressedSpeechicleClicks.delete(speechicle)) {
        event.preventDefault();
        return;
      }
      if (event.detail > 1) {
        cancelPendingSpeechicleExpansion();
        return;
      }
      scheduleSpeechicleExpansion(item.id);
    });
    speechicle.addEventListener("dblclick", (event) => {
      event.preventDefault();
      cancelPendingSpeechicleExpansion();
      void playTimelineItem(item);
    });

    const actions = document.createElement("div");
    actions.className = "speechicle-actions";
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
    rowControls.push(speechicle, actions, accessibleText);
    row.classList.toggle("is-expanded", isExpanded);
    row.append(...rowControls);
    speechicleList.append(row);
  }
  for (const section of sections) {
    if (!insertedSections.has(section)) {
      speechicleList.append(createTimelineDivider(section));
    }
  }
  speechicleList.scrollTop = previousScrollTop;
  updateTimelineDividers(items, historyTotal);
  updateTimelineRows(items);
  revealCurrentItem();
  if (focusedItemId) {
    const row = speechicleList.querySelector<HTMLElement>(`[data-item-id="${focusedItemId}"]`);
    const preferred = focusedControlClass
      ? row?.querySelector<HTMLButtonElement>(`.${focusedControlClass}`)
      : row?.querySelector<HTMLButtonElement>(".speechicle-content");
    const fallback = row?.querySelector<HTMLButtonElement>(".queue-menu-button");
    (preferred?.disabled ? fallback : preferred)?.focus({ preventScroll: true });
  }
}

function updateTimelineRows(items: TimelineItem[]): void {
  const itemById = new Map(items.map((item) => [item.id, item]));
  const displayedWaitingRows = [
    ...speechicleList.querySelectorAll<HTMLElement>(".speechicle-item.is-waiting"),
  ];
  const displayedWaitingPositions = new Map(
    displayedWaitingRows.map((row, index) => [
      row.dataset.itemId,
      displayedWaitingRows.length - index,
    ]),
  );
  const pendingId = pendingTimelineMutation && pendingTimelineMutation.kind !== "clear"
    ? pendingTimelineMutation.item.id
    : null;
  const timelineCommandInFlight = commandPending || pendingTimelineMutation !== null;
  for (const row of speechicleList.querySelectorAll<HTMLElement>(".speechicle-item")) {
    const item = itemById.get(row.dataset.itemId ?? "");
    if (!item) {
      continue;
    }
    const reference = itemReference(
      item,
      displayedWaitingPositions.get(item.id) ?? item.position,
    );
    const pending = item.id === pendingId ? pendingTimelineMutation : null;
    const failed = item.id === failedTimelineMutation?.id;
    row.classList.toggle("is-pending", pending !== null);
    row.classList.toggle("is-error", failed);
    const speechicle = row.querySelector<HTMLButtonElement>(".speechicle-content");
    if (speechicle) {
      speechicle.setAttribute("aria-busy", String(pending !== null));
      speechicle.setAttribute(
        "aria-label",
        speechicleActionLabel(item, row.classList.contains("is-expanded"), reference),
      );
    }
    const dragHandle = row.querySelector<HTMLButtonElement>(".timeline-drag-handle");
    if (dragHandle) {
      dragHandle.disabled = timelineCommandInFlight;
      dragHandle.setAttribute(
        "aria-label",
        `Reorder ${reference}. Drag, or use the arrow keys`,
      );
    }
    const menuButton = row.querySelector<HTMLButtonElement>(".queue-menu-button");
    if (menuButton) {
      menuButton.disabled = timelineCommandInFlight;
      menuButton.setAttribute("aria-label", `Actions for ${reference}`);
      menuButton.setAttribute(
        "aria-busy",
        String(pending !== null),
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
    action.disabled = timelineCommandInFlight;
  }
  const voiceSelect = queueActionMenu.querySelector<HTMLSelectElement>(".queue-menu-voice");
  if (voiceSelect) {
    voiceSelect.disabled = timelineCommandInFlight;
  }
}

function setExpandedItem(id: string | null): void {
  expandedItemId = id;
  const itemById = new Map(visibleTimelineItems().map((item) => [item.id, item]));
  for (const row of speechicleList.querySelectorAll<HTMLElement>(".speechicle-item")) {
    const expanded = row.dataset.itemId === id;
    row.classList.toggle("is-expanded", expanded);
    const speechicle = row.querySelector<HTMLButtonElement>(".speechicle-content");
    const item = itemById.get(row.dataset.itemId ?? "");
    speechicle?.setAttribute("aria-expanded", String(expanded));
    if (speechicle && item) {
      speechicle.setAttribute("aria-label", speechicleActionLabel(item, expanded));
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
    timelineMutationBlocked()
  ) {
    return;
  }
  await runTimelineMutation(
    {
      kind: "play",
      mutation: voice
        ? { type: "play", id: item.id, voice }
        : { type: "play", id: item.id },
      item: voice ? { ...item, voice } : item,
      playbackState: "playing",
    },
    "Could not start selected speech. Try again.",
  );
}

async function moveWaitingItem(id: string, beforeId: string | null): Promise<void> {
  if (timelineMutationBlocked()) {
    return;
  }
  const item = visibleTimelineItems().find((entry) => entry.id === id);
  if (!item || item.kind !== "waiting") {
    return;
  }
  const reordered = moveSpeechicleItemBefore(currentStatus.queue, id, beforeId);
  const previousIds = currentStatus.queue.map((item) => item.id);
  if (reordered.every((item, index) => item.id === previousIds[index])) {
    return;
  }
  await runTimelineMutation(
    {
      kind: "row",
      mutation: { type: "move", section: "waiting", id, beforeId },
      item,
    },
    "Could not reorder waiting speech. Try again.",
  );
}

async function moveHistoryItem(id: string, beforeId: string | null): Promise<void> {
  if (timelineMutationBlocked()) {
    return;
  }
  const item = visibleTimelineItems().find((entry) => entry.id === id);
  if (!item || item.kind !== "history") {
    return;
  }
  const reordered = moveSpeechicleItemBefore(currentStatus.history, id, beforeId);
  const previousIds = currentStatus.history.map((item) => item.id);
  if (reordered.every((item, index) => item.id === previousIds[index])) {
    return;
  }
  await runTimelineMutation(
    {
      kind: "row",
      mutation: { type: "move", section: "history", id, beforeId },
      item,
    },
    "Could not reorder History speech. Try again.",
  );
}

async function archiveWaitingItem(id: string): Promise<void> {
  if (timelineMutationBlocked()) {
    return;
  }
  const item = currentStatus.queue.find((queued) => queued.id === id);
  if (!item) {
    return;
  }
  await runTimelineMutation(
    {
      kind: "row",
      mutation: { type: "archive", id },
      item: { ...item, kind: "waiting", position: null },
    },
    "Could not move waiting speech to History. Try again.",
  );
}

async function deleteHistoryItem(id: string): Promise<void> {
  if (timelineMutationBlocked()) {
    return;
  }
  const item = currentStatus.history.find((entry) => entry.id === id);
  if (!item) {
    return;
  }
  await runTimelineMutation(
    {
      kind: "row",
      mutation: { type: "delete", id },
      item: { ...item, kind: "history", position: null },
    },
    "Could not delete speech from History. Try again.",
  );
}

async function runTimelineMutation(
  pending: PendingTimelineMutation,
  failureMessage: string,
): Promise<void> {
  if (timelineMutationBlocked()) {
    return;
  }
  const { mutation } = pending;
  const failureId = pending.kind === "clear" ? null : pending.item.id;
  const restoreTimelineFocus = pending.kind === "clear" &&
    document.activeElement === clearQueueButton;
  pendingTimelineMutation = pending;
  failedTimelineMutation = null;
  commandStatus.textContent = "";
  render(currentStatus);
  try {
    if (!desktopApi) {
      console.info("Demo timeline mutation requested", mutation);
      await new Promise((resolve) => window.setTimeout(resolve, 250));
      return;
    }
    const result = await desktopApi.mutateTimeline(mutation);
    currentStatus = adoptTimelineSnapshot(currentStatus, result.snapshot);
    if (result.outcome === "rejected") {
      failedTimelineMutation = { id: failureId, type: mutation.type };
      commandStatus.textContent = failureMessage;
      console.error("Timeline mutation was rejected", result.error);
    } else if (result.outcome === "unconfirmed") {
      commandStatus.textContent = "Command result was unconfirmed. Speech state was refreshed.";
      console.error("Timeline mutation result was unconfirmed", result.error);
    }
  } catch (error) {
    console.error("Could not update the speech timeline", error);
    failedTimelineMutation = { id: failureId, type: mutation.type };
    commandStatus.textContent = error instanceof Error &&
        error.message.includes("Engine protocol error")
      ? "The app and speech engine returned incompatible data. Restart or reinstall Super Speech."
      : failureMessage;
  } finally {
    if (pendingTimelineMutation === pending) {
      pendingTimelineMutation = null;
    }
    renderedTimelineKey = null;
    render(currentStatus);
    if (restoreTimelineFocus) {
      speechicleList.focus({ preventScroll: true });
    }
  }
}

async function refreshStatus(): Promise<void> {
  if (!desktopApi) {
    render(currentStatus);
    return;
  }
  try {
    const status = await desktopApi.getStatus();
    render(adoptTimelineSnapshot(currentStatus, status));
  } catch (error) {
    console.error("Could not read Super Speech status", error);
    render(adoptTimelineSnapshot(currentStatus, {
      ...currentStatus,
      state: "stopped",
      engine_running: false,
    }));
    statusLabel.textContent = "Disconnected";
  }
}

async function runPlaybackAction(): Promise<void> {
  const action = playbackAction(
    playbackPresentation(currentStatus, pendingPlaybackForPresentation()).state,
  );
  if (
    commandPending ||
    (pendingTimelineMutation && pendingTimelineMutation.kind !== "play") ||
    timelinePointerDrag ||
    action === "inactive"
  ) {
    return;
  }
  commandPending = true;
  commandStatus.textContent = "";
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
  const pendingPlay = pendingTimelineMutation?.kind === "play"
    ? pendingTimelineMutation
    : null;
  const previousPendingState = pendingPlay?.playbackState;
  if (pendingPlay) {
    pendingPlay.playbackState = paused ? "paused" : "playing";
    render(currentStatus);
  }
  try {
    if (desktopApi) {
      const status = await desktopApi.setPaused(paused);
      render(adoptTimelineSnapshot(currentStatus, status));
    } else {
      render({ ...currentStatus, state: paused ? "paused" : "playing" });
    }
  } catch (error) {
    console.error("Could not change playback state", error);
    commandStatus.textContent = paused
      ? "Could not pause speech. Try again."
      : "Could not resume speech. Try again.";
    if (pendingTimelineMutation === pendingPlay && previousPendingState) {
      pendingPlay.playbackState = previousPendingState;
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
  const activeItems = visibleTimelineItems().filter(({ kind }) => kind !== "history");
  if (timelineMutationBlocked() || activeItems.length === 0) {
    return;
  }
  await runTimelineMutation(
    { kind: "clear", mutation: { type: "clear" } },
    "Could not clear speech. Try again.",
  );
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
    cancelTimelinePointerDrag();
    cancelSpeechiclePointerGesture();
    cancelPendingSpeechicleExpansion();
    setOpenActionMenu(null);
  } else {
    void refreshStatus();
  }
});
window.addEventListener("focus", () => void refreshStatus());

speechicleList.addEventListener("pointerdown", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) {
    return;
  }
  const handle = target.closest<HTMLButtonElement>(".timeline-drag-handle");
  const row = handle?.closest<HTMLElement>(
    ".speechicle-item.is-waiting, .speechicle-item.is-history",
  );
  const id = row?.dataset.itemId;
  if (handle && row && id) {
    beginTimelinePointerDrag(
      event,
      id,
      row,
      handle,
      row.classList.contains("is-history") ? "history" : "waiting",
    );
    return;
  }
  const speechicle = target.closest<HTMLButtonElement>(".speechicle-content");
  if (speechicle && speechicleList.contains(speechicle)) {
    beginSpeechiclePointerGesture(event, speechicle);
  }
});
speechicleList.addEventListener("scroll", () => {
  if (openMenuItemId) {
    setOpenActionMenu(openMenuItemId);
  }
}, { passive: true });
window.addEventListener("pointermove", updateTimelinePointerDrag, true);
window.addEventListener("pointermove", updateSpeechiclePointerGesture, true);
window.addEventListener("pointerup", (event) => {
  finishTimelinePointerDrag(event, true);
  finishSpeechiclePointerGesture(event);
}, true);
window.addEventListener("pointercancel", (event) => {
  finishTimelinePointerDrag(event, false);
  cancelSpeechiclePointerGesture(event.pointerId);
}, true);
speechicleList.addEventListener("lostpointercapture", (event) => {
  if (timelinePointerDrag?.state.pointerId === event.pointerId) {
    cancelTimelinePointerDrag();
  }
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    cancelTimelinePointerDrag();
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
    !event.target.closest(".speechicle-actions, #queue-action-menu")
  ) {
    setOpenActionMenu(null);
  }
}, true);
window.addEventListener("blur", () => {
  cancelTimelinePointerDrag();
  cancelSpeechiclePointerGesture();
  cancelPendingSpeechicleExpansion();
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
