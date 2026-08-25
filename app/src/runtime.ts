export type RuntimeState =
  | "loading"
  | "playing"
  | "paused"
  | "idle"
  | "ready"
  | "setup_required"
  | "stopped";

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
  version: 2;
  state: RuntimeState;
  updated_at: number;
  engine_pid: number | null;
  current: CurrentItem | null;
  queue_count: number;
  queue: QueueItem[];
  history_count: number;
  history: QueueItem[];
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

interface EngineStatusV1 extends Omit<EngineStatus, "version" | "history_count" | "history"> {
  version: 1;
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

function isEngineStatusV1(
  value: Record<string, unknown>,
): value is Record<string, unknown> & EngineStatusV1 {
  return value.version === 1 && hasStatusCore(value);
}

function isEngineStatusV2(
  value: Record<string, unknown>,
): value is Record<string, unknown> & EngineStatus {
  return (
    value.version === 2 &&
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
  if (isEngineStatusV2(status)) {
    return status;
  }
  // A new app can briefly read v1 status from the outgoing engine; v1 had no history to recover
  if (isEngineStatusV1(status)) {
    return {
      ...status,
      version: 2,
      history_count: 0,
      history: [],
    };
  }
  return null;
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
  return [
    ...(status.current ? [timelineItem(status.current, "current", null)] : []),
    ...status.queue.map((item, index) => timelineItem(item, "upcoming", index + 1)),
    ...status.history.map((item) => timelineItem(item, "history", null)),
  ];
}

export interface DesktopApi {
  getStatus(): Promise<RuntimeStatus>;
  setPaused(paused: boolean): Promise<RuntimeStatus>;
  playChunk(id: string): Promise<void>;
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
  version: 2,
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
