import { randomUUID } from "node:crypto";
import { open } from "node:fs/promises";
import path from "node:path";
import { isSpeechicleId } from "../src/runtime.ts";

export const AGENT_MESSAGE_TEXT_MAX = 4_000;
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

function messageFrom(input: AgentInboxMessageInput): AgentInboxMessage {
  const text = input.text.trim();
  if (
    !isSpeechicleId(input.speechicleId) ||
    !text ||
    [...text].length > AGENT_MESSAGE_TEXT_MAX
  ) {
    throw new Error("Invalid agent message");
  }
  return {
    version: 1,
    kind: "user_message",
    id: randomUUID(),
    sent_at: new Date().toISOString(),
    speechicle_id: input.speechicleId,
    ...(input.source === undefined ? {} : { source: input.source }),
    text,
  };
}

export async function appendAgentInboxMessage(
  inboxPath: string,
  input: AgentInboxMessageInput,
): Promise<AgentInboxMessage> {
  if (!validInboxPath(inboxPath)) {
    throw new Error("Invalid agent inbox path");
  }
  const message = messageFrom(input);
  const inbox = await open(inboxPath, "a");
  try {
    await inbox.writeFile(`${JSON.stringify(message)}\n`, "utf8");
    await inbox.sync();
  } finally {
    await inbox.close();
  }
  return message;
}
