import { randomUUID } from "node:crypto";
import { open } from "node:fs/promises";
import path from "node:path";
import { AGENT_MESSAGE_TEXT_MAX, isSpeechicleId } from "../src/runtime.ts";

const INBOX_PATH_MAX = 4_096;

export interface AgentInboxMessage {
  version: 1;
  kind: "user_message";
  id: string;
  sent_at: string;
  speechicle_id: string;
  source?: string;
  text: string;
}

export interface AgentInboxMessageInput {
  speechicleId: string;
  source?: string;
  text: string;
}

function validInboxPath(inboxPath: string): boolean {
  return inboxPath.length <= INBOX_PATH_MAX &&
    inboxPath.trim() === inboxPath &&
    ![...inboxPath].some((character) => {
      const code = character.codePointAt(0) ?? 0;
      return code < 32 || code === 127;
    }) &&
    path.isAbsolute(inboxPath);
}

export async function appendAgentInboxMessage(
  inboxPath: string,
  input: AgentInboxMessageInput,
): Promise<void> {
  if (!validInboxPath(inboxPath)) {
    throw new Error("Invalid agent inbox path");
  }
  const text = input.text.trim();
  if (
    !isSpeechicleId(input.speechicleId) ||
    !text ||
    [...text].length > AGENT_MESSAGE_TEXT_MAX
  ) {
    throw new Error("Invalid agent message");
  }
  const message: AgentInboxMessage = {
    version: 1,
    kind: "user_message",
    id: randomUUID(),
    sent_at: new Date().toISOString(),
    speechicle_id: input.speechicleId,
    source: input.source,
    text,
  };
  const inbox = await open(inboxPath, "a");
  try {
    await inbox.writeFile(`${JSON.stringify(message)}\n`, "utf8");
    await inbox.sync();
  } finally {
    await inbox.close();
  }
}
