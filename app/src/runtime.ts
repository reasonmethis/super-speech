export type RuntimeState =
  | "loading"
  | "playing"
  | "paused"
  | "idle"
  | "ready"
  | "setup_required"
  | "stopped";

export const ENGINE_STATUS_VERSION = 4 as const;

export interface QueueItem {
  id: string;
  filename: string;
  text: string;
  voice: string;
}

export interface CurrentItem extends QueueItem {
  piece: number;
  piece_count: number;
  elapsed_seconds: number;
}

export interface EngineStatus {
  version: typeof ENGINE_STATUS_VERSION;
  state: RuntimeState;
  updated_at: number;
  engine_pid: number | null;
  current: CurrentItem | null;
  queue_count: number;
  queue: QueueItem[];
  history_count: number;
  history: QueueItem[];
}

export interface EngineProcessStatus {
  updated_at: number;
  engine_pid: number | null;
}

export interface RuntimeStatus extends EngineStatus {
  engine_running: boolean;
  installed: boolean;
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

export function parsePlayAcceptance(value: unknown): PlayAcceptance | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const acceptance = value as Record<string, unknown>;
  return typeof acceptance.id === "string" && typeof acceptance.accepted_at === "number"
    ? { id: acceptance.id, acceptedAt: acceptance.accepted_at }
    : null;
}

const RUNTIME_STATES = new Set<RuntimeState>([
  "loading",
  "playing",
  "paused",
  "idle",
  "ready",
  "setup_required",
  "stopped",
]);

function isQueueItem(value: unknown): value is QueueItem {
  if (!value || typeof value !== "object") {
    return false;
  }
  const item = value as Partial<QueueItem>;
  return (
    typeof item.id === "string" &&
    typeof item.filename === "string" &&
    typeof item.text === "string" &&
    typeof item.voice === "string"
  );
}

function isCurrentItem(value: unknown): value is CurrentItem {
  if (!isQueueItem(value)) {
    return false;
  }
  const item = value as Partial<CurrentItem>;
  return (
    typeof item.piece === "number" &&
    typeof item.piece_count === "number" &&
    typeof item.elapsed_seconds === "number"
  );
}

function hasStatusCore(value: Record<string, unknown>): boolean {
  return (
    typeof value.state === "string" &&
    RUNTIME_STATES.has(value.state as RuntimeState) &&
    typeof value.updated_at === "number" &&
    (value.engine_pid === null || typeof value.engine_pid === "number") &&
    (value.current === null || isCurrentItem(value.current)) &&
    typeof value.queue_count === "number" &&
    Array.isArray(value.queue) &&
    value.queue.every(isQueueItem)
  );
}

function isEngineStatusCurrent(
  value: Record<string, unknown>,
): value is Record<string, unknown> & EngineStatus {
  return (
    value.version === ENGINE_STATUS_VERSION &&
    hasStatusCore(value) &&
    typeof value.history_count === "number" &&
    Array.isArray(value.history) &&
    value.history.every(isQueueItem)
  );
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
    filename: item.filename,
    text: item.text,
    voice: item.voice,
    kind,
    position,
  };
}

export function timelineItems(
  status: Pick<EngineStatus, "current" | "queue" | "history">,
): TimelineItem[] {
  const activeIds = new Set([
    ...(status.current ? [status.current.id] : []),
    ...status.queue.map(({ id }) => id),
  ]);
  return [
    ...(status.current ? [timelineItem(status.current, "current", null)] : []),
    ...status.queue.map((item, index) => timelineItem(item, "upcoming", index + 1)),
    ...status.history
      .filter(({ id }) => !activeIds.has(id))
      .map((item) => timelineItem(item, "history", null)),
  ];
}

export interface DesktopApi {
  getStatus(): Promise<RuntimeStatus>;
  setPaused(paused: boolean): Promise<RuntimeStatus>;
  playChunk(id: string): Promise<PlayAcceptance>;
  clearQueue(): Promise<void>;
  openSetup(): Promise<void>;
  minimize(): Promise<void>;
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
  setPaused: "runtime:set-paused",
  playChunk: "runtime:play-chunk",
  clearQueue: "runtime:clear-queue",
  openSetup: "app:open-setup",
  minimize: "window:minimize",
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
  queue_count: 0,
  queue: [],
  history_count: 0,
  history: [],
};
