import "./styles.css";
import {
  INITIAL_STATUS,
  timelineItems,
  type RuntimeStatus,
  type TimelineItem,
} from "./runtime";

const demoStatus: RuntimeStatus = {
  version: 1,
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
      text: "Click this chunk to play it now. Use its arrow to expand or collapse the complete text.",
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
const piecePill = requiredElement<HTMLSpanElement>("piece-pill");
const metadataRow = requiredElement<HTMLDivElement>("metadata-row");
const queueCount = requiredElement<HTMLSpanElement>("queue-count");
const clearQueueButton = requiredElement<HTMLButtonElement>("clear-queue-button");
const queueList = requiredElement<HTMLDivElement>("queue-list");
const desktopApi = window.superSpeech;

let currentStatus = desktopApi ? INITIAL_STATUS : demoStatus;
let commandPending = false;
let pendingChunkId: string | null = null;
let failedChunkId: string | null = null;
let clearPending = false;
let clearFailed = false;
let expandedItemId: string | null = null;
let renderedTimelineKey: string | null = null;

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
  if (pending) {
    return "Starting...";
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
  playbackButton.disabled = commandPending || action === "ready";
  playbackButton.setAttribute("aria-busy", String(commandPending || status.state === "loading"));
  playbackIcon.innerHTML = playbackIconMarkup(status);

  const current = paused ? null : status.current;
  metadataRow.classList.toggle("is-hidden", !current);
  if (current) {
    voiceLabel.textContent = formatVoice(current.voice);
    voicePill.classList.remove("is-hidden");
    if (current.piece_count > 1) {
      piecePill.textContent = `Part ${current.piece} of ${current.piece_count}`;
      piecePill.classList.remove("is-hidden");
    } else {
      piecePill.classList.add("is-hidden");
    }
  } else {
    voicePill.classList.add("is-hidden");
    piecePill.classList.add("is-hidden");
  }

  queueCount.textContent = String(status.queue_count);
  queueCount.setAttribute(
    "aria-label",
    `${status.queue_count} ${status.queue_count === 1 ? "chunk" : "chunks"} waiting`,
  );
  clearQueueButton.classList.toggle("is-hidden", status.queue_count === 0);
  clearQueueButton.disabled = clearPending;
  clearQueueButton.textContent = clearPending
    ? "Clearing..."
    : clearFailed
      ? "Retry clear"
      : "Clear";
  renderTimeline(timelineItems(status), status.history_count);
}

function renderTimeline(items: TimelineItem[], historyTotal: number): void {
  if (!items.some((item) => item.id === expandedItemId)) {
    expandedItemId = null;
  }
  if (!items.some((item) => item.id === failedChunkId)) {
    failedChunkId = null;
  }
  const timelineKey = JSON.stringify([
    pendingChunkId,
    failedChunkId,
    historyTotal,
    items.map(({ id, text, voice, kind, position }) => [id, text, voice, kind, position]),
  ]);
  if (timelineKey === renderedTimelineKey) {
    return;
  }
  renderedTimelineKey = timelineKey;

  const previousScrollTop = queueList.scrollTop;
  queueList.replaceChildren();
  if (items.length === 0) {
    const empty = document.createElement("div");
    empty.className = "queue-empty";
    empty.innerHTML = '<span class="empty-check">&#10003;</span><span>No speech yet</span>';
    queueList.append(empty);
    updateTimelineFade();
    return;
  }

  let historyDividerAdded = false;
  for (const item of items) {
    if (item.kind === "history" && !historyDividerAdded) {
      historyDividerAdded = true;
      const divider = document.createElement("div");
      divider.className = "timeline-divider";
      divider.innerHTML = `<span>Earlier</span><span>${historyTotal}</span>`;
      queueList.append(divider);
    }

    const row = document.createElement("div");
    row.className = "queue-item";
    row.dataset.itemId = item.id;
    const isCurrent = item.kind === "current";
    const isExpanded = item.id === expandedItemId;
    const isPending = item.id === pendingChunkId;
    const isFailed = item.id === failedChunkId;
    if (isCurrent) {
      row.classList.add("is-current");
      row.setAttribute("aria-current", "true");
    }
    if (isPending) {
      row.classList.add("is-pending");
    }
    if (isFailed) {
      row.classList.add("is-error");
    }

    const play = document.createElement("button");
    play.className = "queue-play";
    play.type = "button";
    play.disabled = isCurrent || pendingChunkId !== null;
    play.setAttribute(
      "aria-label",
      isCurrent
        ? "Currently speaking"
        : item.kind === "history"
          ? `Replay: ${item.text}`
          : `Play now: ${item.text}`,
    );

    const order = document.createElement("span");
    order.className = "queue-order";
    order.textContent = isCurrent
      ? "NOW"
      : item.kind === "history"
        ? "PAST"
        : String(item.position).padStart(2, "0");

    const copy = document.createElement("div");
    copy.className = "queue-copy";
    const text = document.createElement("p");
    text.textContent = item.text;
    const meta = document.createElement("span");
    const action = timelineAction(item, isPending, isFailed);
    meta.textContent = `${formatVoice(item.voice)}  /  ${action}`;
    copy.append(text, meta);
    play.append(order, copy);

    const disclosure = document.createElement("button");
    disclosure.className = "queue-disclosure";
    disclosure.type = "button";
    disclosure.setAttribute("aria-expanded", String(isExpanded));
    disclosure.setAttribute(
      "aria-label",
      `${isExpanded ? "Collapse" : "Expand"} full text`,
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
    row.classList.toggle("is-expanded", isExpanded);
    row.append(play, disclosure);
    queueList.append(row);
  }
  queueList.scrollTop = previousScrollTop;
  updateTimelineFade();
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
    disclosure?.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} full text`);
  }
}

async function playTimelineItem(item: TimelineItem): Promise<void> {
  if (item.kind === "current" || pendingChunkId !== null) {
    return;
  }
  pendingChunkId = item.id;
  failedChunkId = null;
  renderedTimelineKey = null;
  render(currentStatus);
  try {
    if (desktopApi) {
      render(await desktopApi.playChunk(item.id));
    } else {
      console.info(`Demo playback requested for ${item.id}`);
    }
  } catch (error) {
    console.error("Could not start the selected speech", error);
    failedChunkId = item.id;
  } finally {
    pendingChunkId = null;
    renderedTimelineKey = null;
    render(currentStatus);
  }
}

async function refreshStatus(): Promise<void> {
  if (!desktopApi) {
    render(currentStatus);
    return;
  }
  try {
    render(await desktopApi.getStatus());
  } catch (error) {
    console.error("Could not read Super Speech status", error);
    render({ ...currentStatus, state: "stopped", engine_running: false });
    statusLabel.textContent = "Disconnected";
  }
}

playbackButton.addEventListener("click", async () => {
  const action = playbackAction(currentStatus);
  if (commandPending || action === "ready") {
    return;
  }
  commandPending = true;
  playbackButton.disabled = true;
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
  if (clearPending || currentStatus.queue_count === 0) {
    return;
  }
  clearPending = true;
  clearFailed = false;
  render(currentStatus);
  try {
    if (desktopApi) {
      render(await desktopApi.clearQueue());
    } else {
      console.info("Demo queue clear requested");
    }
  } catch (error) {
    console.error("Could not clear the speech queue", error);
    clearFailed = true;
  } finally {
    clearPending = false;
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
  if (!document.hidden) {
    void refreshStatus();
  }
});

queueList.addEventListener("scroll", updateTimelineFade, { passive: true });

render(currentStatus);
void refreshStatus();
window.setInterval(() => void refreshStatus(), 700);
