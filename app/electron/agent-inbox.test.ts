import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  appendAgentInboxMessage,
  type AgentInboxMessage,
} from "./agent-inbox.ts";
import { AGENT_MESSAGE_TEXT_MAX } from "../src/runtime.ts";

const SPEECHICLE_ID = `sp_${"1".repeat(32)}`;

test("appends complete self-identifying JSON Lines messages", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "super-speech-inbox-"));
  const inbox = path.join(directory, "agent-inbox.jsonl");
  try {
    await appendAgentInboxMessage(inbox, {
      speechicleId: SPEECHICLE_ID,
      source: "Codex UI task",
      text: "  Please check the retry path.  ",
    });
    await appendAgentInboxMessage(inbox, {
      speechicleId: SPEECHICLE_ID,
      text: "No source label here",
    });
    const lines = (await readFile(inbox, "utf8")).trimEnd().split("\n");
    const [first, second] = lines.map((line) => JSON.parse(line) as AgentInboxMessage);

    assert.equal(lines.length, 2);
    assert.equal(first.version, 1);
    assert.equal(first.kind, "user_message");
    assert.equal(first.speechicle_id, SPEECHICLE_ID);
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

test("rejects unsafe inbox paths before opening them", async () => {
  const message = {
    speechicleId: SPEECHICLE_ID,
    text: "Message",
  };
  const absolutePath = path.resolve("inbox.jsonl");
  const driveRoot = path.parse(absolutePath).root;
  for (const inboxPath of [
    "relative.jsonl",
    `${absolutePath} `,
    `${absolutePath}\n`,
    path.join(driveRoot, "x".repeat(4_096)),
  ]) {
    await assert.rejects(
      appendAgentInboxMessage(inboxPath, message),
      /Invalid agent inbox path/,
    );
  }
});

test("rejects invalid message bodies before opening the inbox", async () => {
  const inbox = path.resolve("missing-parent", "inbox.jsonl");
  for (const message of [
    {
      speechicleId: "not-a-speechicle",
      text: "Message",
    },
    {
      speechicleId: SPEECHICLE_ID,
      text: " \n\t ",
    },
    {
      speechicleId: SPEECHICLE_ID,
      text: "x".repeat(AGENT_MESSAGE_TEXT_MAX + 1),
    },
  ]) {
    await assert.rejects(
      appendAgentInboxMessage(inbox, message),
      /Invalid agent message/,
    );
  }
});
