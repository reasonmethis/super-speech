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
}

export interface RuntimeStatus extends EngineStatus {
  engine_running: boolean;
  installed: boolean;
}

export interface DesktopApi {
  getStatus(): Promise<RuntimeStatus>;
  setPaused(paused: boolean): Promise<RuntimeStatus>;
  openSetup(): Promise<void>;
  minimize(): Promise<void>;
  hide(): Promise<void>;
}

export const IPC_CHANNELS = {
  getStatus: "runtime:get-status",
  setPaused: "runtime:set-paused",
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
};
