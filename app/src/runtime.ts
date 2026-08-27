export type RuntimeState =
  | "loading"
  | "playing"
  | "paused"
  | "idle"
  | "setup_required"
  | "stopped";

export const ENGINE_STATUS_VERSION = 11 as const;

const SPEECHICLE_ID = /^sp_[0-9a-f]{32}$/;

export function isSpeechicleId(value: unknown): value is string {
  return typeof value === "string" && SPEECHICLE_ID.test(value);
}

// Labels for the SHA-pinned Kokoro v1.0 voice archive bundled by prepare_resources
export const VOICE_OPTIONS = [
  ["af_alloy", "Alloy", "US female"],
  ["af_aoede", "Aoede", "US female"],
  ["af_bella", "Bella", "US female"],
  ["af_heart", "Heart", "US female"],
  ["af_jessica", "Jessica", "US female"],
  ["af_kore", "Kore", "US female"],
  ["af_nicole", "Nicole", "US female"],
  ["af_nova", "Nova", "US female"],
  ["af_river", "River", "US female"],
  ["af_sarah", "Sarah", "US female"],
  ["af_sky", "Sky", "US female"],
  ["am_adam", "Adam", "US male"],
  ["am_echo", "Echo", "US male"],
  ["am_eric", "Eric", "US male"],
  ["am_fenrir", "Fenrir", "US male"],
  ["am_liam", "Liam", "US male"],
  ["am_michael", "Michael", "US male"],
  ["am_onyx", "Onyx", "US male"],
  ["am_puck", "Puck", "US male"],
  ["am_santa", "Santa", "US male"],
  ["bf_alice", "Alice", "UK female"],
  ["bf_emma", "Emma", "UK female"],
  ["bf_isabella", "Isabella", "UK female"],
  ["bf_lily", "Lily", "UK female"],
  ["bm_daniel", "Daniel", "UK male"],
  ["bm_fable", "Fable", "UK male"],
  ["bm_george", "George", "UK male"],
  ["bm_lewis", "Lewis", "UK male"],
] as const;

export interface QueueItem {
  id: string;
  text: string;
  voice: string;
}

export interface CurrentItem extends QueueItem {
  piece: number;
  piece_count: number;
  piece_start: number | null;
  piece_end: number | null;
  elapsed_seconds: number;
}

export interface CurrentPieceSegments {
  before: string;
  current: string;
  after: string;
}

export function currentPieceSegments(
  item: CurrentItem,
): CurrentPieceSegments | null {
  if (item.piece_start === null || item.piece_end === null) {
    return null;
  }
  const characters = Array.from(item.text);
  return {
    before: characters.slice(0, item.piece_start).join(""),
    current: characters.slice(item.piece_start, item.piece_end).join(""),
    after: characters.slice(item.piece_end).join(""),
  };
}

export interface StartedItem {
  id: string;
  started_at: number;
}

export interface EngineStatus {
  version: typeof ENGINE_STATUS_VERSION;
  state: RuntimeState;
  updated_at: number;
  engine_pid: number | null;
  current: CurrentItem | null;
  recent_starts: StartedItem[];
  queue_count: number;
  queue: QueueItem[];
  history_count: number;
  history: QueueItem[];
}

export interface EngineProcessStatus {
  updated_at: number;
  engine_pid: number | null;
}

export function engineProcessIsLive(
  status: EngineProcessStatus | null,
  heartbeatFresh: boolean,
  processIsLive: (processId: number) => boolean,
  nowSeconds = Date.now() / 1000,
): boolean {
  return Boolean(
    status?.engine_pid &&
    processIsLive(status.engine_pid) &&
    (heartbeatFresh || nowSeconds - status.updated_at < 300),
  );
}

export interface RuntimeStatus extends EngineStatus {
  engine_running: boolean;
  installed: boolean;
}

export function compatibleEngineIsRunning(
  ownedEngineRunning: boolean,
  status: EngineStatus | null,
  storedProcessRunning: boolean,
): boolean {
  return ownedEngineRunning || Boolean(status && storedProcessRunning);
}

export function runtimeStateForSnapshot(
  installed: boolean,
  engineRunning: boolean,
  engineState: RuntimeState | undefined,
): RuntimeState {
  if (!installed || engineState === "setup_required") {
    return "setup_required";
  }
  if (!engineRunning) {
    return "stopped";
  }
  return engineState ?? "loading";
}

export function statusAfterPauseCommand(
  status: RuntimeStatus,
  paused: boolean,
): RuntimeStatus {
  if (["setup_required", "stopped"].includes(status.state)) {
    return status;
  }
  if (!status.engine_running) {
    return { ...status, state: "stopped" };
  }
  if (status.state === "loading") {
    return status;
  }
  const hasWork = status.current !== null;
  return {
    ...status,
    state: hasWork ? (paused ? "paused" : "playing") : "idle",
  };
}

export type TimelineItemKind = "current" | "upcoming" | "history";

export interface TimelineItem extends QueueItem {
  kind: TimelineItemKind;
  position: number | null;
}

export interface PlayAcceptance {
  id: string;
  acceptedAt: number;
}

export type PlayAcceptanceState = "pending" | "applied" | "failed";

export function pendingPlaybackState(
  status: EngineStatus,
  acceptance: PlayAcceptance | null,
  current: "playing" | "paused",
): "playing" | "paused" {
  return acceptance &&
      status.updated_at >= acceptance.acceptedAt &&
      (status.state === "playing" || status.state === "paused")
    ? status.state
    : current;
}

export function playAcceptanceState(
  status: EngineStatus,
  acceptance: PlayAcceptance,
): PlayAcceptanceState {
  if (
    status.recent_starts.some(
      ({ id, started_at }) => id === acceptance.id && started_at >= acceptance.acceptedAt,
    )
  ) {
    return "applied";
  }
  if (status.updated_at < acceptance.acceptedAt) {
    return "pending";
  }
  if (status.state === "stopped") {
    return "failed";
  }
  if (
    status.current?.id === acceptance.id ||
    status.queue.some(({ id }) => id === acceptance.id)
  ) {
    return "pending";
  }
  return "failed";
}

export type PlaybackPresentation =
  | { state: "playing" | "paused"; item: QueueItem }
  | { state: "loading"; item: QueueItem | null }
  | { state: "idle" | "setup_required" | "stopped"; item: null };

export type PendingPlayback = {
  item: QueueItem;
  state: "playing" | "paused";
};

export function parsePlayAcceptance(value: unknown): PlayAcceptance | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const acceptance = value as Record<string, unknown>;
  return isSpeechicleId(acceptance.id) && typeof acceptance.accepted_at === "number"
    ? { id: acceptance.id, acceptedAt: acceptance.accepted_at }
    : null;
}

export function playbackPresentation(
  status: EngineStatus,
  pendingPlayback: PendingPlayback | null,
): PlaybackPresentation {
  const selectedItem = pendingPlayback?.item ?? null;
  if (status.state === "setup_required") {
    return { state: "setup_required", item: null };
  }
  if (status.state === "stopped" && !selectedItem) {
    return { state: "stopped", item: null };
  }

  const activeItem = selectedItem ?? status.current;
  if (!activeItem) {
    return status.state === "loading"
      ? { state: "loading", item: null }
      : { state: "idle", item: null };
  }
  if (pendingPlayback) {
    return { state: pendingPlayback.state, item: pendingPlayback.item };
  }
  if (status.state === "paused") {
    return { state: "paused", item: activeItem };
  }
  if (status.state === "loading") {
    return { state: "loading", item: activeItem };
  }
  return { state: "playing", item: activeItem };
}

const RUNTIME_STATES = new Set<RuntimeState>([
  "loading",
  "playing",
  "paused",
  "idle",
  "setup_required",
  "stopped",
]);

function isQueueItem(value: unknown): value is QueueItem {
  if (!value || typeof value !== "object") {
    return false;
  }
  const item = value as Partial<QueueItem> & Record<string, unknown>;
  return (
    isSpeechicleId(item.id) &&
    !("filename" in item) &&
    typeof item.text === "string" &&
    typeof item.voice === "string"
  );
}

function isCurrentItem(value: unknown): value is CurrentItem {
  if (!isQueueItem(value)) {
    return false;
  }
  const item = value as Partial<CurrentItem>;
  if (
    !Number.isInteger(item.piece) ||
    !Number.isInteger(item.piece_count) ||
    item.piece_count === undefined ||
    item.piece_count < 1 ||
    item.piece === undefined ||
    item.piece < 0 ||
    item.piece > item.piece_count ||
    typeof item.elapsed_seconds !== "number"
  ) {
    return false;
  }
  if (item.piece === 0) {
    return item.piece_start === null && item.piece_end === null;
  }
  const start = item.piece_start;
  const end = item.piece_end;
  const length = Array.from(item.text ?? "").length;
  return Number.isInteger(start) &&
    Number.isInteger(end) &&
    start !== null && start !== undefined &&
    end !== null && end !== undefined &&
    start >= 0 &&
    end > start &&
    end <= length;
}

function isStartedItem(value: unknown): value is StartedItem {
  if (!value || typeof value !== "object") {
    return false;
  }
  const item = value as Partial<StartedItem>;
  return isSpeechicleId(item.id) && typeof item.started_at === "number";
}

function playbackBoundaryMatchesState(value: Record<string, unknown>): boolean {
  const hasCurrent = value.current !== null;
  if (value.state === "playing" || value.state === "paused") {
    return hasCurrent;
  }
  if (value.state === "idle") {
    return !hasCurrent;
  }
  return true;
}

function hasStatusCore(value: Record<string, unknown>): boolean {
  const queue = Array.isArray(value.queue) && value.queue.every(isQueueItem)
    ? value.queue
    : null;
  const current = value.current === null || isCurrentItem(value.current)
    ? value.current
    : undefined;
  return (
    typeof value.state === "string" &&
    RUNTIME_STATES.has(value.state as RuntimeState) &&
    typeof value.updated_at === "number" &&
    (value.engine_pid === null || typeof value.engine_pid === "number") &&
    current !== undefined &&
    Array.isArray(value.recent_starts) &&
    value.recent_starts.every(isStartedItem) &&
    Number.isInteger(value.queue_count) &&
    queue !== null &&
    (current !== null || queue.length === 0) &&
    (current === null || !queue.some(({ id }) => id === current.id)) &&
    new Set(queue.map(({ id }) => id)).size === queue.length &&
    value.queue_count === queue.length &&
    playbackBoundaryMatchesState(value)
  );
}

function isEngineStatusCurrent(
  value: Record<string, unknown>,
): value is Record<string, unknown> & EngineStatus {
  if (!(
    value.version === ENGINE_STATUS_VERSION &&
    hasStatusCore(value) &&
    Array.isArray(value.history) &&
    value.history.every(isQueueItem) &&
    new Set(value.history.map(({ id }) => id)).size === value.history.length &&
    Number.isInteger(value.history_count) &&
    (value.history_count as number) >= value.history.length
  )) {
    return false;
  }
  const current = value.current as CurrentItem | null;
  const queue = value.queue as QueueItem[];
  const activeIds = new Set([
    ...(current ? [current.id] : []),
    ...queue.map(({ id }) => id),
  ]);
  return (value.history as QueueItem[]).every(({ id }) => !activeIds.has(id));
}

export function parseEngineStatus(value: unknown): EngineStatus | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const status = value as Record<string, unknown>;
  if (isEngineStatusCurrent(status)) {
    return status;
  }
  return null;
}

export function statusAfterTransientRead(
  value: unknown,
  previous: EngineStatus | null,
): EngineStatus | null {
  return parseEngineStatus(value) ?? (value === null ? previous : null);
}

export function parseEngineProcessStatus(value: unknown): EngineProcessStatus | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const status = value as Record<string, unknown>;
  if (
    typeof status.updated_at !== "number" ||
    (status.engine_pid !== null && typeof status.engine_pid !== "number")
  ) {
    return null;
  }
  return { updated_at: status.updated_at, engine_pid: status.engine_pid };
}

function timelineItem(
  item: QueueItem,
  kind: TimelineItemKind,
  position: number | null,
): TimelineItem {
  return {
    id: item.id,
    text: item.text,
    voice: item.voice,
    kind,
    position,
  };
}

export function timelineItems(
  status: Pick<EngineStatus, "current" | "queue" | "history">,
): TimelineItem[] {
  const upcoming = status.queue.map((item, index) =>
    timelineItem(item, "upcoming", index + 1)
  );
  return [
    ...upcoming.reverse(),
    ...(status.current ? [timelineItem(status.current, "current", null)] : []),
    ...status.history.map((item) => timelineItem(item, "history", null)),
  ];
}

export function activeTimelineIds(
  status: Pick<EngineStatus, "current" | "queue">,
): Set<string> {
  return new Set([
    ...(status.current ? [status.current.id] : []),
    ...status.queue.map(({ id }) => id),
  ]);
}

export function clearRequestWasApplied(
  status: Pick<EngineStatus, "updated_at" | "current" | "queue">,
  baselineIds: ReadonlySet<string>,
  requestedAfter: number,
): boolean {
  if (baselineIds.size === 0 || status.updated_at <= requestedAfter) {
    return false;
  }
  const activeIds = activeTimelineIds(status);
  return [...baselineIds].every((id) => !activeIds.has(id));
}

/** Reclassify one existing row as Current without changing visual order */
export function timelineItemsAtBoundary(
  status: Pick<EngineStatus, "current" | "queue" | "history">,
  boundary: QueueItem,
): TimelineItem[] {
  const items = timelineItems(status);
  const boundaryIndex = items.findIndex(({ id }) => id === boundary.id);
  if (boundaryIndex < 0) {
    return items;
  }
  return items.map((item, index) => {
    if (index < boundaryIndex) {
      return { ...item, kind: "upcoming", position: boundaryIndex - index };
    }
    if (index === boundaryIndex) {
      return timelineItem(boundary, "current", null);
    }
    return { ...item, kind: "history", position: null };
  });
}

export function moveQueueItemBefore<T extends { id: string }>(
  items: readonly T[],
  id: string,
  beforeId: string | null,
): T[] {
  const source = items.find((item) => item.id === id);
  if (!source || beforeId === id) {
    return [...items];
  }
  const reordered = items.filter((item) => item.id !== id);
  if (beforeId === null) {
    reordered.push(source);
    return reordered;
  }
  const destination = reordered.findIndex((item) => item.id === beforeId);
  if (destination < 0) {
    return [...items];
  }
  reordered.splice(destination, 0, source);
  return reordered;
}

export interface VersionInfo {
  app: string;
  engine: string;
}

export interface DesktopApi {
  getStatus(): Promise<RuntimeStatus>;
  getVersions(): Promise<VersionInfo>;
  setPaused(paused: boolean): Promise<RuntimeStatus>;
  playChunk(id: string, voice?: string): Promise<PlayAcceptance>;
  moveQueueItem(id: string, beforeId: string | null): Promise<void>;
  moveHistoryItem(id: string, beforeId: string | null): Promise<void>;
  archiveQueueItem(id: string): Promise<void>;
  deleteHistoryItem(id: string): Promise<void>;
  copyText(text: string): Promise<void>;
  clearQueue(): Promise<void>;
  openSetup(): Promise<void>;
  minimize(): Promise<void>;
  toggleMaximize(): Promise<void>;
  onMaximizedChange(listener: (maximized: boolean) => void): void;
  hide(): Promise<void>;
}

export function statusForEngineProcess(
  status: EngineStatus | null,
  processId: number | undefined,
): EngineStatus | null {
  return status?.engine_pid === processId ? status : null;
}

export const IPC_CHANNELS = {
  getStatus: "runtime:get-status",
  getVersions: "runtime:get-versions",
  setPaused: "runtime:set-paused",
  playChunk: "runtime:play-chunk",
  moveQueueItem: "runtime:move-queue-item",
  moveHistoryItem: "runtime:move-history-item",
  archiveQueueItem: "runtime:archive-queue-item",
  deleteHistoryItem: "runtime:delete-history-item",
  copyText: "runtime:copy-text",
  clearQueue: "runtime:clear-queue",
  openSetup: "app:open-setup",
  minimize: "window:minimize",
  toggleMaximize: "window:toggle-maximize",
  maximizedChanged: "window:maximized-changed",
  hide: "window:hide",
} as const;

export const INITIAL_STATUS: RuntimeStatus = {
  version: ENGINE_STATUS_VERSION,
  state: "loading",
  updated_at: 0,
  engine_pid: null,
  engine_running: false,
  installed: true,
  current: null,
  recent_starts: [],
  queue_count: 0,
  queue: [],
  history_count: 0,
  history: [],
};
