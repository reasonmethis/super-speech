import { moveSpeechicleItemBefore } from "./runtime.ts";

interface TimelineDragBase {
  pointerId: number;
  sourceId: string;
  initialVisualOrder: readonly string[];
  kind: "waiting" | "history";
}

export type TimelineDragState =
  | (TimelineDragBase & { phase: "armed" })
  | (TimelineDragBase & {
      phase: "section";
      previewVisualOrder: readonly string[];
    })
  | (TimelineDragBase & {
      kind: "waiting";
      phase: "history";
      previewVisualOrder: readonly string[];
    });

export type TimelineDragCommand =
  | {
      type: "move";
      kind: "waiting" | "history";
      id: string;
      beforeId: string | null;
    }
  | { type: "archive"; id: string };

export type TimelineDragEvent =
  | { type: "preview-section"; pointerId: number; beforeId: string | null }
  | { type: "preview-history"; pointerId: number }
  | { type: "finish"; pointerId: number; commit: boolean }
  | { type: "cancel" };

export interface TimelineDragTransition {
  state: TimelineDragState | null;
  visualOrder: readonly string[] | null;
  command: TimelineDragCommand | null;
}

export interface TimelineRowBounds {
  id: string;
  top: number;
  height: number;
}

export interface TimelineListBounds {
  left: number;
  right: number;
  bottom: number;
}

const NO_CHANGE = {
  visualOrder: null,
  command: null,
} as const;

export function startTimelineDrag(
  pointerId: number,
  sourceId: string,
  visualOrder: readonly string[],
  kind: "waiting" | "history",
): TimelineDragState | null {
  if (!visualOrder.includes(sourceId) || new Set(visualOrder).size !== visualOrder.length) {
    return null;
  }
  return {
    phase: "armed",
    pointerId,
    sourceId,
    initialVisualOrder: [...visualOrder],
    kind,
  };
}

function settleDrag(state: TimelineDragState, commit: boolean): TimelineDragTransition {
  if (commit && state.phase === "history") {
    return {
      state: null,
      visualOrder: state.previewVisualOrder,
      command: { type: "archive", id: state.sourceId },
    };
  }

  if (
    commit &&
    state.phase === "section" &&
    state.previewVisualOrder.some(
      (id, index) => id !== state.initialVisualOrder[index]
    )
  ) {
    const engineOrder = state.kind === "waiting"
      ? [...state.previewVisualOrder].reverse()
      : [...state.previewVisualOrder];
    const sourceIndex = engineOrder.indexOf(state.sourceId);
    return {
      state: null,
      visualOrder: state.previewVisualOrder,
      command: {
        type: "move",
        kind: state.kind,
        id: state.sourceId,
        beforeId: engineOrder[sourceIndex + 1] ?? null,
      },
    };
  }
  return {
    state: null,
    visualOrder: state.initialVisualOrder,
    command: null,
  };
}

export function transitionTimelineDrag(
  state: TimelineDragState | null,
  event: TimelineDragEvent,
): TimelineDragTransition {
  if (event.type === "cancel") {
    return state ? settleDrag(state, false) : { state: null, ...NO_CHANGE };
  }
  if (!state || event.pointerId !== state.pointerId) {
    return { state, ...NO_CHANGE };
  }
  if (event.type === "finish") {
    return settleDrag(state, event.commit);
  }
  if (event.type === "preview-history") {
    if (state.kind !== "waiting") {
      return { state, ...NO_CHANGE };
    }
    const previewVisualOrder = state.phase === "armed"
      ? state.initialVisualOrder
      : state.previewVisualOrder;
    return {
      state: {
        ...state,
        kind: "waiting",
        phase: "history",
        previewVisualOrder,
      },
      visualOrder: previewVisualOrder,
      command: null,
    };
  }

  const items = state.initialVisualOrder.map((id) => ({ id }));
  const previewVisualOrder = moveSpeechicleItemBefore(
    items,
    state.sourceId,
    event.beforeId,
  ).map(({ id }) => id);
  return {
    state: {
      ...state,
      phase: "section",
      previewVisualOrder,
    },
    visualOrder: previewVisualOrder,
    command: null,
  };
}

export function sectionDropBeforeId(
  sourceId: string,
  rows: readonly TimelineRowBounds[],
  draggedCenterY: number,
): string | null {
  return rows.find(
    (row) => row.id !== sourceId && draggedCenterY <= row.top + row.height / 2 + 0.5,
  )?.id ?? null;
}

export function pointerMovedBeyondThreshold(
  startX: number,
  startY: number,
  currentX: number,
  currentY: number,
  threshold: number,
): boolean {
  return Math.hypot(currentX - startX, currentY - startY) >= threshold;
}

export function isHistoryDropArea(
  list: TimelineListBounds,
  dividerTop: number,
  pointerX: number,
  pointerY: number,
): boolean {
  return pointerX >= list.left &&
    pointerX <= list.right &&
    pointerY >= dividerTop &&
    pointerY <= list.bottom;
}
