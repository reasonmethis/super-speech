import { readFileSync } from "node:fs";
import { writeTextAtomically } from "./atomic-file.ts";

export interface WindowBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface SavedWindowState {
  bounds: WindowBounds;
  maximized: boolean;
}

export const DEFAULT_WINDOW_SIZE = { width: 420, height: 680 } as const;
export const MINIMUM_WINDOW_SIZE = { width: 380, height: 620 } as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value);
}

export function parseSavedWindowState(value: unknown): SavedWindowState | null {
  if (!isRecord(value) || !isRecord(value.bounds) || typeof value.maximized !== "boolean") {
    return null;
  }
  const { x, y, width, height } = value.bounds;
  if (
    !isInteger(x) ||
    !isInteger(y) ||
    !isInteger(width) ||
    !isInteger(height) ||
    width <= 0 ||
    height <= 0
  ) {
    return null;
  }
  return { bounds: { x, y, width, height }, maximized: value.maximized };
}

export function readSavedWindowState(filePath: string): SavedWindowState | null {
  try {
    return parseSavedWindowState(JSON.parse(readFileSync(filePath, "utf8")));
  } catch {
    return null;
  }
}

export function writeSavedWindowState(
  filePath: string,
  state: SavedWindowState,
): Promise<void> {
  return writeTextAtomically(filePath, `${JSON.stringify(state)}\n`);
}

function intersectionArea(first: WindowBounds, second: WindowBounds): number {
  const width = Math.max(
    0,
    Math.min(first.x + first.width, second.x + second.width) - Math.max(first.x, second.x),
  );
  const height = Math.max(
    0,
    Math.min(first.y + first.height, second.y + second.height) - Math.max(first.y, second.y),
  );
  return width * height;
}

function fittedSize(
  size: Pick<WindowBounds, "width" | "height">,
  workArea: WindowBounds,
): Pick<WindowBounds, "width" | "height"> {
  return {
    width: Math.min(Math.max(size.width, MINIMUM_WINDOW_SIZE.width), workArea.width),
    height: Math.min(Math.max(size.height, MINIMUM_WINDOW_SIZE.height), workArea.height),
  };
}

function centeredBounds(
  size: Pick<WindowBounds, "width" | "height">,
  workArea: WindowBounds,
): WindowBounds {
  const fitted = fittedSize(size, workArea);
  return {
    x: workArea.x + Math.round((workArea.width - fitted.width) / 2),
    y: workArea.y + Math.round((workArea.height - fitted.height) / 2),
    ...fitted,
  };
}

export function restoredWindowBounds(
  saved: SavedWindowState | null,
  workAreas: readonly WindowBounds[],
): WindowBounds {
  const primary = workAreas[0] ?? {
    x: 0,
    y: 0,
    ...DEFAULT_WINDOW_SIZE,
  };
  if (!saved) {
    return centeredBounds(DEFAULT_WINDOW_SIZE, primary);
  }

  let destination = primary;
  let overlap = 0;
  for (const workArea of workAreas) {
    const candidateOverlap = intersectionArea(saved.bounds, workArea);
    if (candidateOverlap > overlap) {
      destination = workArea;
      overlap = candidateOverlap;
    }
  }
  if (overlap === 0) {
    return centeredBounds(saved.bounds, primary);
  }

  const size = fittedSize(saved.bounds, destination);
  return {
    x: Math.min(
      Math.max(saved.bounds.x, destination.x),
      destination.x + destination.width - size.width,
    ),
    y: Math.min(
      Math.max(saved.bounds.y, destination.y),
      destination.y + destination.height - size.height,
    ),
    ...size,
  };
}
