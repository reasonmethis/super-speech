import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  renameSync,
  rmSync,
} from "node:fs";
import path from "node:path";

export const MANAGED_SKILL_HASH_KIND = "managed-tree-v1";

type ManagedSkillSyncStatus = "current" | "installed" | "preserved" | "updated";

export interface ManagedSkillSyncResult {
  hash: string | null;
  sourceHash: string;
  status: ManagedSkillSyncStatus;
}

export function managedSkillHashForTarget(
  hashKind: string | null,
  fallbackHash: string | null,
  hashes: Record<string, string>,
  target: string,
): string | null {
  if (hashKind !== MANAGED_SKILL_HASH_KIND) {
    return null;
  }
  if (Object.hasOwn(hashes, target)) {
    return hashes[target];
  }
  return Object.keys(hashes).length === 0 ? fallbackHash : null;
}

function excludedFromManagedTree(name: string): boolean {
  return name === "runtime" ||
    name === "build" ||
    name === "__pycache__" ||
    name.endsWith(".egg-info") ||
    name.endsWith(".pyc");
}

function addHashField(hash: ReturnType<typeof createHash>, value: string): void {
  const bytes = Buffer.from(value, "utf8");
  hash.update(String(bytes.length));
  hash.update(":");
  hash.update(bytes);
}

function hashManagedDirectory(
  hash: ReturnType<typeof createHash>,
  directory: string,
  relativeDirectory = "",
): void {
  const entries = readdirSync(directory, { withFileTypes: true })
    .filter((entry) => !excludedFromManagedTree(entry.name))
    .sort((left, right) => left.name.localeCompare(right.name, "en"));
  for (const entry of entries) {
    const relativePath = path.posix.join(
      relativeDirectory.replaceAll("\\", "/"),
      entry.name,
    );
    const absolutePath = path.join(directory, entry.name);
    const metadata = lstatSync(absolutePath);
    if (metadata.isDirectory()) {
      addHashField(hash, "directory");
      addHashField(hash, relativePath);
      hashManagedDirectory(hash, absolutePath, relativePath);
      continue;
    }
    if (metadata.isFile()) {
      addHashField(hash, "file");
      addHashField(hash, relativePath);
      hash.update(createHash("sha256").update(readFileSync(absolutePath)).digest());
      continue;
    }
    if (metadata.isSymbolicLink()) {
      addHashField(hash, "link");
      addHashField(hash, relativePath);
      addHashField(hash, readlinkSync(absolutePath));
      continue;
    }
    addHashField(hash, "other");
    addHashField(hash, relativePath);
  }
}

export function managedSkillTreeHash(directory: string): string {
  const hash = createHash("sha256");
  hashManagedDirectory(hash, directory);
  return hash.digest("hex");
}

function copyManagedTree(source: string, target: string): void {
  mkdirSync(path.dirname(target), { recursive: true });
  cpSync(source, target, {
    recursive: true,
    filter: (sourcePath) => !excludedFromManagedTree(path.basename(sourcePath)),
  });
}

function renameWithConfirmation(source: string, target: string): void {
  try {
    renameSync(source, target);
  } catch (error) {
    if (!existsSync(source) && existsSync(target)) {
      return;
    }
    throw error;
  }
}

function moveExcludedEntries(source: string, target: string): void {
  if (!existsSync(source)) {
    return;
  }
  for (const entry of readdirSync(source, { withFileTypes: true })) {
    const sourcePath = path.join(source, entry.name);
    const targetPath = path.join(target, entry.name);
    if (excludedFromManagedTree(entry.name)) {
      mkdirSync(path.dirname(targetPath), { recursive: true });
      renameWithConfirmation(sourcePath, targetPath);
      continue;
    }
    if (entry.isDirectory()) {
      moveExcludedEntries(sourcePath, targetPath);
    }
  }
}

function transactionPaths(target: string): { backup: string; staging: string } {
  return {
    backup: `${target}.super-speech-managed-backup`,
    staging: `${target}.super-speech-managed-staging`,
  };
}

function rollbackCutover(target: string, staging: string, backup: string): void {
  const candidate = existsSync(target) ? target : staging;
  if (existsSync(candidate) && existsSync(backup)) {
    moveExcludedEntries(candidate, backup);
  }
  if (existsSync(target)) {
    rmSync(target, { recursive: true, force: true });
  }
  if (existsSync(staging)) {
    rmSync(staging, { recursive: true, force: true });
  }
  if (existsSync(backup)) {
    renameWithConfirmation(backup, target);
  }
}

function recoverInterruptedCutover(
  sourceHash: string,
  target: string,
  previousManagedHash: string | null,
): void {
  const { backup, staging } = transactionPaths(target);
  if (existsSync(target)) {
    if (!existsSync(backup)) {
      rmSync(staging, { recursive: true, force: true });
      return;
    }
    if (
      previousManagedHash &&
      managedSkillTreeHash(backup) === previousManagedHash
    ) {
      if (managedSkillTreeHash(target) === sourceHash) {
        rmSync(backup, { recursive: true, force: true });
        rmSync(staging, { recursive: true, force: true });
        return;
      }
      throw new Error("cannot safely recover an edited agent skill update");
    }
    throw new Error("cannot safely recover an unrecognized agent skill update");
  }

  if (existsSync(backup)) {
    if (
      existsSync(staging) &&
      previousManagedHash &&
      managedSkillTreeHash(backup) === previousManagedHash &&
      managedSkillTreeHash(staging) === sourceHash
    ) {
      try {
        moveExcludedEntries(backup, staging);
        renameWithConfirmation(staging, target);
        if (managedSkillTreeHash(target) !== sourceHash) {
          throw new Error("recovered agent skill does not match its packaged source");
        }
        rmSync(backup, { recursive: true, force: true });
        return;
      } catch (error) {
        rollbackCutover(target, staging, backup);
        throw error;
      }
    }
    rollbackCutover(target, staging, backup);
    return;
  }

  if (existsSync(staging)) {
    if (managedSkillTreeHash(staging) === sourceHash) {
      renameWithConfirmation(staging, target);
      return;
    }
    rmSync(staging, { recursive: true, force: true });
  }
}

function replaceManagedTree(
  source: string,
  target: string,
  sourceHash: string,
): void {
  const { backup, staging } = transactionPaths(target);
  rmSync(staging, { recursive: true, force: true });
  rmSync(backup, { recursive: true, force: true });
  copyManagedTree(source, staging);
  if (managedSkillTreeHash(staging) !== sourceHash) {
    throw new Error("staged agent skill does not match its packaged source");
  }

  renameWithConfirmation(target, backup);
  try {
    moveExcludedEntries(backup, staging);
    renameWithConfirmation(staging, target);
    if (managedSkillTreeHash(target) !== sourceHash) {
      throw new Error("updated agent skill does not match its packaged source");
    }
    rmSync(backup, { recursive: true, force: true });
  } catch (error) {
    rollbackCutover(target, staging, backup);
    throw error;
  }
}

export function syncManagedSkillTree(
  source: string,
  target: string,
  previousManagedHash: string | null,
): ManagedSkillSyncResult {
  const sourceHash = managedSkillTreeHash(source);
  recoverInterruptedCutover(sourceHash, target, previousManagedHash);
  if (!existsSync(target)) {
    const { staging } = transactionPaths(target);
    copyManagedTree(source, staging);
    if (managedSkillTreeHash(staging) !== sourceHash) {
      throw new Error("staged agent skill does not match its packaged source");
    }
    renameWithConfirmation(staging, target);
    const installedHash = managedSkillTreeHash(target);
    if (installedHash !== sourceHash) {
      throw new Error("installed agent skill does not match its packaged source");
    }
    return { hash: sourceHash, sourceHash, status: "installed" };
  }

  const currentHash = managedSkillTreeHash(target);
  if (currentHash === sourceHash) {
    return { hash: sourceHash, sourceHash, status: "current" };
  }
  if (!previousManagedHash || currentHash !== previousManagedHash) {
    return {
      hash: previousManagedHash,
      sourceHash,
      status: "preserved",
    };
  }

  replaceManagedTree(source, target, sourceHash);
  const updatedHash = managedSkillTreeHash(target);
  if (updatedHash !== sourceHash) {
    throw new Error("updated agent skill does not match its packaged source");
  }
  return { hash: sourceHash, sourceHash, status: "updated" };
}
