import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  AGENT_MESSAGE_TEXT_MAX,
  appendAgentInboxMessage,
  type AgentInboxMessage,
} from "./agent-inbox.ts";

const SPEECHICLE_ID = `sp_${"1".repeat(32)}`;

test("agent replies append complete self-identifying JSON Lines messages", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "super-speech-inbox-"));
  const inbox = path.join(directory, "agent-inbox.jsonl");
  try {
    const first = await appendAgentInboxMessage(inbox, {
      speechicleId: SPEECHICLE_ID,
      source: "Codex UI task",
      text: "  Please check the retry path.  ",
    });
    const second = await appendAgentInboxMessage(inbox, {
      speechicleId: SPEECHICLE_ID,
      text: "No source label here",
    });
    const lines = (await readFile(inbox, "utf8")).trimEnd().split("\n");
    const messages = lines.map((line) => JSON.parse(line) as AgentInboxMessage);

    assert.equal(lines.length, 2);
    assert.deepEqual(messages, [first, second]);
    assert.equal(first.text, "Please check the retry path.");
    assert.equal(first.source, "Codex UI task");
    assert.equal(second.source, undefined);
    assert.notEqual(first.id, second.id);
    assert.match(first.id, /^[0-9a-f-]{36}$/);
    assert.equal(new Date(first.sent_at).toISOString(), first.sent_at);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("agent replies reject invalid destinations and message bodies", async () => {
  await assert.rejects(
    appendAgentInboxMessage("relative.jsonl", {
      speechicleId: SPEECHICLE_ID,
      text: "Message",
    }),
    /Invalid agent inbox path/,
  );
  await assert.rejects(
    appendAgentInboxMessage(path.resolve("missing-parent", "inbox.jsonl"), {
      speechicleId: "not-a-speechicle",
      text: "Message",
    }),
    /Invalid agent message/,
  );
  await assert.rejects(
    appendAgentInboxMessage(path.resolve("missing-parent", "inbox.jsonl"), {
      speechicleId: SPEECHICLE_ID,
      text: "x".repeat(AGENT_MESSAGE_TEXT_MAX + 1),
    }),
    /Invalid agent message/,
  );
});
