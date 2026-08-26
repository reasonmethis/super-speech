import { moveQueueItemBefore } from "./runtime.ts";

interface QueueDragBase {
  pointerId: number;
  sourceId: string;
  initialVisualOrder: readonly string[];
}

export type QueueDragState =
  | (QueueDragBase & { phase: "armed" })
  | (QueueDragBase & {
      phase: "queue";
      previewVisualOrder: readonly string[];
    })
  | (QueueDragBase & {
      phase: "history";
      previewVisualOrder: readonly string[];
    });

export type QueueDragCommand =
  | { type: "move"; id: string; beforeId: string | null }
  | { type: "archive"; id: string };

export type QueueDragEvent =
  | { type: "preview-queue"; pointerId: number; beforeId: string | null }
  | { type: "preview-history"; pointerId: number }
  | { type: "finish"; pointerId: number; commit: boolean }
  | { type: "cancel" };

export interface QueueDragTransition {
  state: QueueDragState | null;
  visualOrder: readonly string[] | null;
  command: QueueDragCommand | null;
}

export interface QueueRowBounds {
  id: string;
  top: number;
  height: number;
}

export interface QueueListBounds {
  left: number;
  right: number;
  bottom: number;
}

const NO_CHANGE = {
  visualOrder: null,
  command: null,
} as const;

export function startQueueDrag(
  pointerId: number,
  sourceId: string,
  visualOrder: readonly string[],
): QueueDragState | null {
  if (!visualOrder.includes(sourceId) || new Set(visualOrder).size !== visualOrder.length) {
    return null;
  }
  return {
    phase: "armed",
    pointerId,
    sourceId,
    initialVisualOrder: [...visualOrder],
  };
}

function settleDrag(state: QueueDragState, commit: boolean): QueueDragTransition {
  if (!commit || state.phase === "armed") {
    return {
      state: null,
      visualOrder: state.initialVisualOrder,
      command: null,
    };
  }
  if (state.phase === "history") {
    return {
      state: null,
      visualOrder: state.previewVisualOrder,
      command: { type: "archive", id: state.sourceId },
    };
  }

  const changed = state.previewVisualOrder.some(
    (id, index) => id !== state.initialVisualOrder[index],
  );
  if (!changed) {
    return {
      state: null,
      visualOrder: state.initialVisualOrder,
      command: null,
    };
  }
  const playbackOrder = [...state.previewVisualOrder].reverse();
  const sourceIndex = playbackOrder.indexOf(state.sourceId);
  return {
    state: null,
    visualOrder: state.previewVisualOrder,
    command: {
      type: "move",
      id: state.sourceId,
      beforeId: playbackOrder[sourceIndex + 1] ?? null,
    },
  };
}

export function transitionQueueDrag(
  state: QueueDragState | null,
  event: QueueDragEvent,
): QueueDragTransition {
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
    const previewVisualOrder = state.phase === "queue"
      ? state.previewVisualOrder
      : state.initialVisualOrder;
    return {
      state: {
        phase: "history",
        pointerId: state.pointerId,
        sourceId: state.sourceId,
        initialVisualOrder: state.initialVisualOrder,
        previewVisualOrder,
      },
      visualOrder: previewVisualOrder,
      command: null,
    };
  }

  const items = state.initialVisualOrder.map((id) => ({ id }));
  const previewVisualOrder = moveQueueItemBefore(
    items,
    state.sourceId,
    event.beforeId,
  ).map(({ id }) => id);
  return {
    state: {
      phase: "queue",
      pointerId: state.pointerId,
      sourceId: state.sourceId,
      initialVisualOrder: state.initialVisualOrder,
      previewVisualOrder,
    },
    visualOrder: previewVisualOrder,
    command: null,
  };
}

export function queueDropBeforeId(
  sourceId: string,
  rows: readonly QueueRowBounds[],
  draggedCenterY: number,
): string | null {
  return rows.find(
    (row) => row.id !== sourceId && draggedCenterY <= row.top + row.height / 2,
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
  list: QueueListBounds,
  dividerTop: number,
  pointerX: number,
  pointerY: number,
): boolean {
  return pointerX >= list.left &&
    pointerX <= list.right &&
    pointerY >= dividerTop &&
    pointerY <= list.bottom;
}
