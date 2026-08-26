import assert from "node:assert/strict";
import test from "node:test";
import {
  isHistoryDropArea,
  pointerMovedBeyondThreshold,
  queueDropBeforeId,
  startQueueDrag,
  transitionQueueDrag,
  type QueueDragEvent,
  type QueueDragState,
} from "./queue-drag-model.ts";
import { moveQueueItemBefore } from "./runtime.ts";

const initialOrder = ["newest", "middle", "oldest"];

function begin(pointerId = 1): QueueDragState {
  const state = startQueueDrag(pointerId, "middle", initialOrder);
  assert(state);
  return state;
}

test("rejects a missing source and duplicate IDs", () => {
  assert.equal(startQueueDrag(1, "missing", initialOrder), null);
  assert.equal(
    startQueueDrag(1, "middle", ["middle", "middle"]),
    null,
  );
});

test("maps dragged-card centers to the first, middle, and last visual slots", () => {
  const rows = [
    { id: "newest", top: 0, height: 40 },
    { id: "middle", top: 47, height: 40 },
    { id: "oldest", top: 94, height: 40 },
  ];

  assert.equal(queueDropBeforeId("middle", rows, -10), "newest");
  assert.equal(queueDropBeforeId("middle", rows, 20.4), "newest");
  assert.equal(queueDropBeforeId("middle", rows, 20.6), "oldest");
  assert.equal(queueDropBeforeId("middle", rows, 70), "oldest");
  assert.equal(queueDropBeforeId("middle", rows, 114), "oldest");
  assert.equal(queueDropBeforeId("middle", rows, 200), null);
});

test("pointer movement activates at the threshold, not just beyond it", () => {
  assert.equal(pointerMovedBeyondThreshold(10, 10, 13, 13, 5), false);
  assert.equal(pointerMovedBeyondThreshold(10, 10, 13, 14, 5), true);
});

test("History accepts blank space inside the list but not the same Y outside it", () => {
  const list = { left: 10, right: 210, bottom: 500 };

  assert.equal(isHistoryDropArea(list, 300, 100, 450), true);
  assert.equal(isHistoryDropArea(list, 300, 5, 450), false);
  assert.equal(isHistoryDropArea(list, 300, 215, 450), false);
  assert.equal(isHistoryDropArea(list, 300, 100, 250), false);
  assert.equal(isHistoryDropArea(list, 300, 100, 550), false);
});

test("derives every preview from the original order", () => {
  const first = transitionQueueDrag(begin(), {
    type: "preview-queue",
    pointerId: 1,
    beforeId: "newest",
  });
  assert.deepEqual(first.visualOrder, ["middle", "newest", "oldest"]);
  assert(first.state);

  const last = transitionQueueDrag(first.state, {
    type: "preview-queue",
    pointerId: 1,
    beforeId: null,
  });
  assert.deepEqual(last.visualOrder, ["newest", "oldest", "middle"]);
  assert.deepEqual(first.state.initialVisualOrder, initialOrder);
});

test("every cancellation path restores the original order without a command", () => {
  const states = [
    begin(),
    transitionQueueDrag(begin(), {
      type: "preview-queue",
      pointerId: 1,
      beforeId: "newest",
    }).state,
    transitionQueueDrag(begin(), {
      type: "preview-history",
      pointerId: 1,
    }).state,
  ];

  for (const state of states) {
    assert(state);
    assert.deepEqual(transitionQueueDrag(state, { type: "cancel" }), {
      state: null,
      visualOrder: initialOrder,
      command: null,
    });
  }
  assert.deepEqual(transitionQueueDrag(null, { type: "cancel" }), {
    state: null,
    visualOrder: null,
    command: null,
  });
});

test("a terminal transition emits at most one engine command", () => {
  const preview = transitionQueueDrag(begin(), {
    type: "preview-queue",
    pointerId: 1,
    beforeId: "newest",
  });
  const finished = transitionQueueDrag(preview.state, {
    type: "finish",
    pointerId: 1,
    commit: true,
  });
  assert.deepEqual(finished, {
    state: null,
    visualOrder: ["middle", "newest", "oldest"],
    command: { type: "move", kind: "upcoming", id: "middle", beforeId: null },
  });
  assert.deepEqual(
    transitionQueueDrag(finished.state, {
      type: "finish",
      pointerId: 1,
      commit: true,
    }),
    { state: null, visualOrder: null, command: null },
  );
});

test("a no-op move emits no command and History emits archive", () => {
  const unchanged = transitionQueueDrag(begin(), {
    type: "preview-queue",
    pointerId: 1,
    beforeId: "oldest",
  });
  assert.equal(
    transitionQueueDrag(unchanged.state, {
      type: "finish",
      pointerId: 1,
      commit: true,
    }).command,
    null,
  );

  const history = transitionQueueDrag(begin(), {
    type: "preview-history",
    pointerId: 1,
  });
  assert.deepEqual(
    transitionQueueDrag(history.state, {
      type: "finish",
      pointerId: 1,
      commit: true,
    }).command,
    { type: "archive", id: "middle" },
  );
});

test("moving over History preserves the current queue preview until settlement", () => {
  const queuePreview = transitionQueueDrag(begin(), {
    type: "preview-queue",
    pointerId: 1,
    beforeId: "newest",
  });
  const historyPreview = transitionQueueDrag(queuePreview.state, {
    type: "preview-history",
    pointerId: 1,
  });

  assert.deepEqual(historyPreview.visualOrder, ["middle", "newest", "oldest"]);
  assert.deepEqual(
    transitionQueueDrag(historyPreview.state, {
      type: "finish",
      pointerId: 1,
      commit: true,
    }),
    {
      state: null,
      visualOrder: ["middle", "newest", "oldest"],
      command: { type: "archive", id: "middle" },
    },
  );
  assert.deepEqual(transitionQueueDrag(historyPreview.state, { type: "cancel" }), {
    state: null,
    visualOrder: initialOrder,
    command: null,
  });
});

test("every source and destination round-trips through engine playback order", () => {
  for (let size = 1; size <= 5; size += 1) {
    const visualOrder = Array.from({ length: size }, (_, index) => `item-${index}`);
    for (const sourceId of visualOrder) {
      for (const beforeId of [...visualOrder, null]) {
        const state = startQueueDrag(1, sourceId, visualOrder);
        assert(state);
        const preview = transitionQueueDrag(state, {
          type: "preview-queue",
          pointerId: 1,
          beforeId,
        });
        const finished = transitionQueueDrag(preview.state, {
          type: "finish",
          pointerId: 1,
          commit: true,
        });
        assert(finished.visualOrder);
        if (!finished.command || finished.command.type !== "move") {
          assert.deepEqual(finished.visualOrder, visualOrder);
          continue;
        }
        const engineOrder = [...visualOrder].reverse().map((id) => ({ id }));
        const persistedVisualOrder = moveQueueItemBefore(
          engineOrder,
          finished.command.id,
          finished.command.beforeId,
        ).map(({ id }) => id).reverse();
        assert.deepEqual(persistedVisualOrder, finished.visualOrder);
      }
    }
  }
});

test("History drag commands round-trip through matching visual and engine order", () => {
  const visualOrder = ["newest", "middle", "oldest"];
  for (const sourceId of visualOrder) {
    for (const beforeId of [...visualOrder, null]) {
      const state = startQueueDrag(1, sourceId, visualOrder, "history");
      assert(state);
      const preview = transitionQueueDrag(state, {
        type: "preview-queue",
        pointerId: 1,
        beforeId,
      });
      const finished = transitionQueueDrag(preview.state, {
        type: "finish",
        pointerId: 1,
        commit: true,
      });
      assert(finished.visualOrder);
      if (!finished.command || finished.command.type !== "move") {
        assert.deepEqual(finished.visualOrder, visualOrder);
        continue;
      }
      const persisted = moveQueueItemBefore(
        visualOrder.map((id) => ({ id })),
        finished.command.id,
        finished.command.beforeId,
      ).map(({ id }) => id);
      assert.deepEqual(persisted, finished.visualOrder);
    }
  }
});

test("History-origin drags cannot enter the archive drop phase", () => {
  const state = startQueueDrag(1, "middle", initialOrder, "history");
  assert(state);

  const transition = transitionQueueDrag(state, {
    type: "preview-history",
    pointerId: 1,
  });

  assert.equal(transition.state, state);
  assert.equal(transition.visualOrder, null);
  assert.equal(transition.command, null);
});

test("stale pointer events cannot affect a newer session", () => {
  const restarted = startQueueDrag(2, "oldest", initialOrder);
  assert(restarted);

  const staleFinish = transitionQueueDrag(restarted, {
    type: "finish",
    pointerId: 1,
    commit: true,
  });
  assert.equal(staleFinish.state, restarted);
  assert.equal(staleFinish.command, null);
});

test("long retargeting sequences preserve one copy of every queue item", () => {
  const previews: QueueDragEvent[] = [
    { type: "preview-queue", pointerId: 1, beforeId: "newest" },
    { type: "preview-history", pointerId: 1 },
    { type: "preview-queue", pointerId: 1, beforeId: "oldest" },
    { type: "preview-queue", pointerId: 1, beforeId: null },
  ];
  let state: QueueDragState | null = begin();

  for (let iteration = 0; iteration < 100; iteration += 1) {
    const transition = transitionQueueDrag(state, previews[iteration % previews.length]);
    state = transition.state;
    assert(state);
    const order = state.phase === "armed"
      ? state.initialVisualOrder
      : state.previewVisualOrder;
    assert.deepEqual([...order].sort(), [...initialOrder].sort());
    assert.equal(new Set(order).size, initialOrder.length);
  }
});

test("short adversarial event sequences preserve lifecycle invariants", () => {
  const events: QueueDragEvent[] = [
    { type: "preview-queue", pointerId: 1, beforeId: "newest" },
    { type: "preview-queue", pointerId: 2, beforeId: null },
    { type: "preview-history", pointerId: 1 },
    { type: "finish", pointerId: 1, commit: true },
    { type: "finish", pointerId: 2, commit: false },
    { type: "cancel" },
  ];

  const walk = (
    state: QueueDragState | null,
    depth: number,
    commandEmittedSinceStart: boolean,
  ): void => {
    if (depth === 0) {
      return;
    }
    for (const event of events) {
      const transition = transitionQueueDrag(state, event);
      const nextCommandEmitted = commandEmittedSinceStart || transition.command !== null;
      if (transition.command) {
        assert.equal(commandEmittedSinceStart, false);
        assert.equal(transition.state, null);
      }
      if (event.type === "cancel") {
        assert.equal(transition.state, null);
        assert.equal(transition.command, null);
      }
      if (transition.state) {
        const { initialVisualOrder, sourceId } = transition.state;
        assert(initialVisualOrder.includes(sourceId));
        assert.equal(new Set(initialVisualOrder).size, initialVisualOrder.length);
        if (transition.state.phase === "queue") {
          assert.deepEqual(
            [...transition.state.previewVisualOrder].sort(),
            [...initialVisualOrder].sort(),
          );
        }
      }
      if (transition.visualOrder) {
        assert.equal(
          new Set(transition.visualOrder).size,
          transition.visualOrder.length,
        );
      }
      walk(transition.state, depth - 1, nextCommandEmitted);
    }
  };

  walk(begin(), 5, false);
});
