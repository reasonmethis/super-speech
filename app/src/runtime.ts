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
  version: number;
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

export function timelineItems(
  status: Pick<EngineStatus, "current" | "queue" | "history">,
): TimelineItem[] {
  return [
    ...(status.current
      ? [{ ...status.current, kind: "current" as const, position: null }]
      : []),
    ...status.queue.map((item, index) => ({
      ...item,
      kind: "upcoming" as const,
      position: index + 1,
    })),
    ...status.history.map((item) => ({
      ...item,
      kind: "history" as const,
      position: null,
    })),
  ];
}

export interface DesktopApi {
  getStatus(): Promise<RuntimeStatus>;
  setPaused(paused: boolean): Promise<RuntimeStatus>;
  playChunk(id: string): Promise<RuntimeStatus>;
  clearQueue(): Promise<RuntimeStatus>;
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
  version: 1,
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
