export type TrayPlaybackAction = "pause" | "resume" | null;

export function trayPlaybackAction(state: string): TrayPlaybackAction {
  if (state === "paused" || state === "holding") {
    return "resume";
  }
  if (state === "playing" || state === "idle") {
    return "pause";
  }
  return null;
}
