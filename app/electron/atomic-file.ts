import { randomUUID } from "node:crypto";
import { readFile, rename, rm, writeFile } from "node:fs/promises";

const WINDOWS_REPLACE_RETRY_MS = 25;
const WINDOWS_REPLACE_TIMEOUT_MS = 5_000;
const WINDOWS_REPLACE_ERRORS = new Set(["EACCES", "EBUSY", "EPERM"]);

function canRetryWindowsReplace(error: unknown): boolean {
  return process.platform === "win32" &&
    error instanceof Error &&
    "code" in error &&
    WINDOWS_REPLACE_ERRORS.has(String(error.code));
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

type RenameFile = (oldPath: string, newPath: string) => Promise<void>;

async function targetContains(targetPath: string, content: string): Promise<boolean> {
  try {
    return await readFile(targetPath, "utf8") === content;
  } catch {
    return false;
  }
}

async function replaceFile(
  tempPath: string,
  targetPath: string,
  content: string,
  renameFile: RenameFile,
): Promise<void> {
  const deadline = Date.now() + WINDOWS_REPLACE_TIMEOUT_MS;
  while (true) {
    try {
      await renameFile(tempPath, targetPath);
      return;
    } catch (error) {
      // Windows can report a rename error after the target already changed
      if (await targetContains(targetPath, content)) {
        return;
      }
      if (!canRetryWindowsReplace(error) || Date.now() >= deadline) {
        throw error;
      }
      await delay(WINDOWS_REPLACE_RETRY_MS);
    }
  }
}

export async function writeTextAtomically(
  targetPath: string,
  content: string,
  renameFile: RenameFile = rename,
): Promise<void> {
  const tempPath = `${targetPath}.${process.pid}.${randomUUID()}.tmp`;
  try {
    await writeFile(tempPath, content, { encoding: "utf8", flag: "wx" });
    await replaceFile(tempPath, targetPath, content, renameFile);
  } finally {
    await rm(tempPath, { force: true }).catch(() => undefined);
  }
}
