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
      text: "The queue will stay visible here, with richer controls arriving next.",
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
const playbackKicker = requiredElement<HTMLParagraphElement>("playback-kicker");
const playbackTitle = requiredElement<HTMLHeadingElement>("playback-title");
const currentText = requiredElement<HTMLParagraphElement>("current-text");
const voicePill = requiredElement<HTMLSpanElement>("voice-pill");
const voiceLabel = requiredElement<HTMLSpanElement>("voice-label");
const piecePill = requiredElement<HTMLSpanElement>("piece-pill");
const queueCount = requiredElement<HTMLSpanElement>("queue-count");
const queueList = requiredElement<HTMLDivElement>("queue-list");
const runtimeState = requiredElement<HTMLSpanElement>("runtime-state");
const desktopApi = window.superSpeech;

let currentStatus = desktopApi ? INITIAL_STATUS : demoStatus;
let commandPending = false;

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
  kicker: string;
  title: string;
  body: string;
} {
  if (status.state === "setup_required") {
    return {
      label: "Setup needed",
      kicker: "ONE-TIME SETUP",
      title: "Install the speech engine",
      body: "Open the setup guide, then give its one-line install request to your coding agent.",
    };
  }
  if (status.state === "loading") {
    return {
      label: "Loading",
      kicker: "WARMING UP",
      title: "Preparing your voice",
      body: "Kokoro is loading locally. This normally takes a few seconds.",
    };
  }
  if (status.state === "paused") {
    return {
      label: "Paused",
      kicker: status.current ? "PAUSED HERE" : "SPEECH PAUSED",
      title: status.current ? "Your place is saved" : "Nothing will speak",
      body: status.current?.text ?? "New speech will wait here until you resume.",
    };
  }
  if (status.state === "playing" && status.current) {
    return {
      label: "Speaking",
      kicker: "NOW SPEAKING",
      title: formatVoice(status.current.voice),
      body: status.current.text,
    };
  }
  return {
    label: "Ready",
    kicker: "SUPER SPEECH",
    title: "Ready when you are",
    body: "Your next voice reply will appear here as soon as it starts.",
  };
}

function render(status: RuntimeStatus): void {
  currentStatus = status;
  const paused = status.state === "paused";
  const copy = statusCopy(status);
  document.body.dataset.state = status.state;

  statusDot.className = `status-dot state-${status.state}`;
  statusLabel.textContent = copy.label;
  playbackKicker.textContent = copy.kicker;
  playbackTitle.textContent = copy.title;
  currentText.textContent = copy.body;

  playbackButton.dataset.action = status.state === "setup_required" ? "setup" : paused ? "resume" : "pause";
  playbackButton.setAttribute(
    "aria-label",
    status.state === "setup_required" ? "Open setup guide" : paused ? "Resume speech" : "Pause speech",
  );
  playbackButton.disabled = commandPending;
  playbackIcon.innerHTML = status.state === "setup_required"
    ? '<svg viewBox="0 0 32 32"><path d="M16 7v14m-6-5 6 6 6-6M8 25h16"/></svg>'
    : paused
      ? '<svg viewBox="0 0 32 32"><path class="solid" d="m11 8 13 8-13 8Z"/></svg>'
      : '<svg viewBox="0 0 32 32"><rect class="solid" x="9" y="8" width="5" height="16" rx="2"/><rect class="solid" x="18" y="8" width="5" height="16" rx="2"/></svg>';

  if (status.current) {
    voiceLabel.textContent = formatVoice(status.current.voice);
    voicePill.classList.remove("is-hidden");
    if (status.current.piece_count > 1) {
      piecePill.textContent = `Part ${status.current.piece} of ${status.current.piece_count}`;
      piecePill.classList.remove("is-hidden");
    } else {
      piecePill.classList.add("is-hidden");
    }
  } else {
    voicePill.classList.add("is-hidden");
    piecePill.classList.add("is-hidden");
  }

  queueCount.textContent = String(status.queue_count);
  renderQueue(status.queue, status.queue_count);
  runtimeState.textContent = !status.installed
    ? "Engine setup required"
    : status.engine_running
      ? "Engine running"
      : "Engine starts on demand";
}

function renderQueue(items: QueueItem[], total: number): void {
  queueList.replaceChildren();
  if (items.length === 0) {
    const empty = document.createElement("div");
    empty.className = "queue-empty";
    empty.innerHTML = '<span class="empty-check">&#10003;</span><span>Queue is clear</span>';
    queueList.append(empty);
    return;
  }

  for (const [index, item] of items.entries()) {
    const row = document.createElement("article");
    row.className = "queue-item";

    const order = document.createElement("span");
    order.className = "queue-order";
    order.textContent = String(index + 1).padStart(2, "0");

    const copy = document.createElement("div");
    copy.className = "queue-copy";
    const text = document.createElement("p");
    text.textContent = item.text;
    const meta = document.createElement("span");
    meta.textContent = formatVoice(item.voice);
    copy.append(text, meta);
    row.append(order, copy);
    queueList.append(row);
  }

  if (total > items.length) {
    const more = document.createElement("p");
    more.className = "queue-more";
    more.textContent = `+${total - items.length} more waiting`;
    queueList.append(more);
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
    runtimeState.textContent = "Controller disconnected";
  }
}

playbackButton.addEventListener("click", async () => {
  if (commandPending) {
    return;
  }
  commandPending = true;
  playbackButton.disabled = true;
  if (currentStatus.state === "setup_required") {
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
  const paused = currentStatus.state !== "paused";
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
