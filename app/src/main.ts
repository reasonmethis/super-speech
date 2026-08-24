import "./styles.css";
import { INITIAL_STATUS, type QueueItem, type RuntimeStatus } from "./runtime";

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
      text: "The queue will stay visible here. Click this chunk to read its complete text without leaving the app. Click it again to collapse it.",
      voice: "bm_fable",
    },
    {
      id: "016-af_bella-say",
      filename: "016-af_bella-say.txt",
      text: "Every voice and source app will be easy to spot at a glance.",
      voice: "af_bella",
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
const queueList = requiredElement<HTMLDivElement>("queue-list");
const desktopApi = window.superSpeech;

let currentStatus = desktopApi ? INITIAL_STATUS : demoStatus;
let commandPending = false;
let expandedQueueItemId: string | null = null;
let renderedQueueKey: string | null = null;

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

  const queueItems = status.current ? [status.current, ...status.queue] : status.queue;
  const activeQueueCount = status.queue_count + (status.current ? 1 : 0);
  queueCount.textContent = String(activeQueueCount);
  renderQueue(queueItems, activeQueueCount, status.current?.id ?? null);
}

function renderQueue(items: QueueItem[], total: number, currentItemId: string | null): void {
  if (!items.some((item) => item.id === expandedQueueItemId)) {
    expandedQueueItemId = null;
  }
  const queueKey = JSON.stringify([
    expandedQueueItemId,
    total,
    currentItemId,
    items.map(({ id, text, voice }) => [id, text, voice]),
  ]);
  if (queueKey === renderedQueueKey) {
    return;
  }
  renderedQueueKey = queueKey;

  const previousScrollTop = queueList.scrollTop;
  queueList.replaceChildren();
  if (items.length === 0) {
    const empty = document.createElement("div");
    empty.className = "queue-empty";
    empty.innerHTML = '<span class="empty-check">&#10003;</span><span>Queue is clear</span>';
    queueList.append(empty);
    return;
  }

  for (const [index, item] of items.entries()) {
    const row = document.createElement("button");
    row.className = "queue-item";
    row.type = "button";
    const isCurrent = item.id === currentItemId;
    const isExpanded = item.id === expandedQueueItemId;
    if (isCurrent) {
      row.classList.add("is-current");
      row.setAttribute("aria-current", "true");
    }

    const order = document.createElement("span");
    order.className = "queue-order";
    order.textContent = isCurrent
      ? "NOW"
      : String(index + (currentItemId ? 0 : 1)).padStart(2, "0");

    const copy = document.createElement("div");
    copy.className = "queue-copy";
    const text = document.createElement("p");
    text.textContent = item.text;
    const meta = document.createElement("span");
    meta.textContent = formatVoice(item.voice);
    copy.append(text, meta);
    row.append(order, copy);
    queueList.append(row);

    row.setAttribute("aria-expanded", String(isExpanded));
    row.addEventListener("click", () => {
      const expanding = expandedQueueItemId !== item.id;
      expandedQueueItemId = expanding ? item.id : null;
      renderQueue(items, total, currentItemId);
      if (expanding) {
        queueList
          .querySelector<HTMLElement>('.queue-item[aria-expanded="true"]')
          ?.scrollIntoView({ block: "nearest" });
      }
    });
  }

  if (total > items.length) {
    const more = document.createElement("p");
    more.className = "queue-more";
    more.textContent = `+${total - items.length} more waiting`;
    queueList.append(more);
  }
  queueList.scrollTop = previousScrollTop;
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
    statusDot.className = "status-dot state-stopped";
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

render(currentStatus);
void refreshStatus();
window.setInterval(() => void refreshStatus(), 700);
