export type TrayPlaybackAction = "pause" | "resume" | null;

export function trayPlaybackAction(state: string): TrayPlaybackAction {
  return state === "paused" ? "resume" : state === "playing" ? "pause" : null;
}
