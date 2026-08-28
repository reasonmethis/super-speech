export interface TrayPlaybackControl {
  enabled: boolean;
  label: "Pause Speech" | "Resume Speech";
}

export function trayPlaybackControl(state: string): TrayPlaybackControl {
  return {
    enabled: state === "playing" || state === "paused",
    label: state === "paused" ? "Resume Speech" : "Pause Speech",
  };
}

export function trayPlaybackControlKey(state: string): string {
  const control = trayPlaybackControl(state);
  return `${control.label}:${control.enabled}`;
}
