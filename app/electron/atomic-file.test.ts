import assert from "node:assert/strict";
import test from "node:test";
import {
  cp,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { writeTextAtomically } from "./atomic-file.ts";
import {
  MANAGED_SKILL_HASH_KIND,
  managedSkillTreeHash,
  managedSkillHashForTarget,
  syncManagedSkillTree,
} from "./managed-skill.ts";
import { trayPlaybackAction } from "./tray-menu.ts";

async function writeTestTree(
  root: string,
  files: Record<string, string>,
): Promise<void> {
  for (const [relativePath, content] of Object.entries(files)) {
    const filePath = path.join(root, relativePath);
    await mkdir(path.dirname(filePath), { recursive: true });
    await writeFile(filePath, content, "utf8");
  }
}

test("readers see a complete file while atomic writes replace it", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "super-speech-atomic-"));
  const target = path.join(directory, "install.json");
  const payloads = ["a", "b"].map((value) =>
    `${JSON.stringify({ value, padding: value.repeat(128_000) })}\n`
  );
  await writeFile(target, payloads[0], "utf8");
  let writing = true;
  const reader = (async () => {
    while (writing) {
      const content = await readFile(target, "utf8");
      assert(payloads.includes(content));
    }
  })();

  try {
    for (let index = 0; index < 40; index += 1) {
      await writeTextAtomically(target, payloads[index % payloads.length]);
    }
  } finally {
    writing = false;
    try {
      await reader;
      assert.deepEqual(await readdir(directory), ["install.json"]);
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  }
});

test("an atomic write accepts a rename that completed before reporting an error", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "super-speech-atomic-confirm-"));
  const target = path.join(directory, "install.json");
  const content = `${JSON.stringify({ version: "new" })}\n`;
  try {
    await writeTextAtomically(target, content, async (source, destination) => {
      await rename(source, destination);
      const error = new Error("rename reported an error after commit");
      Object.assign(error, { code: "EPERM" });
      throw error;
    });

    assert.equal(await readFile(target, "utf8"), content);
    assert.deepEqual(await readdir(directory), ["install.json"]);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("a failed atomic replacement preserves the target and removes its temporary file", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "super-speech-atomic-failure-"));
  const target = path.join(directory, "install.json");
  const original = `${JSON.stringify({ version: "old" })}\n`;
  await writeFile(target, original, "utf8");
  try {
    await assert.rejects(
      writeTextAtomically(target, "replacement", async () => {
        const error = new Error("replacement failed");
        Object.assign(error, { code: "EIO" });
        throw error;
      }),
      /replacement failed/,
    );

    assert.equal(await readFile(target, "utf8"), original);
    assert.deepEqual(await readdir(directory), ["install.json"]);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("managed skill hashing preserves runtime and detects edits anywhere else", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "super-speech-skill-hash-"));
  try {
    await writeTestTree(directory, {
      "SKILL.md": "skill",
      "engine/engine.py": "engine",
      "runtime/state.json": "one",
      "engine/build/engine.exe": "build",
      "engine/__pycache__/engine.pyc": "cache",
      "engine/super_speech.egg-info/PKG-INFO": "info",
    });
    const original = managedSkillTreeHash(directory);

    await writeFile(path.join(directory, "runtime", "state.json"), "two", "utf8");
    await writeFile(path.join(directory, "engine", "build", "engine.exe"), "rebuilt", "utf8");
    await writeFile(path.join(directory, "engine", "__pycache__", "engine.pyc"), "new cache", "utf8");
    await writeFile(path.join(directory, "engine", "super_speech.egg-info", "PKG-INFO"), "new info", "utf8");
    assert.equal(managedSkillTreeHash(directory), original);

    await writeFile(path.join(directory, "engine", "engine.py"), "edited", "utf8");
    assert.notEqual(managedSkillTreeHash(directory), original);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("managed skill updates remove retired files but preserve its private runtime", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "super-speech-skill-update-"));
  const previous = path.join(root, "previous");
  const source = path.join(root, "source");
  const target = path.join(root, "target");
  try {
    await writeTestTree(previous, {
      "SKILL.md": "old",
      "engine/retired.py": "retired",
    });
    await cp(previous, target, { recursive: true });
    await writeTestTree(target, { "runtime/queue.json": "saved" });
    await writeTestTree(source, {
      "SKILL.md": "new",
      "engine/current.py": "current",
    });

    const result = syncManagedSkillTree(
      source,
      target,
      managedSkillTreeHash(previous),
    );

    assert.equal(result.status, "updated");
    assert.equal(result.hash, managedSkillTreeHash(source));
    assert.equal(await readFile(path.join(target, "SKILL.md"), "utf8"), "new");
    assert.equal(await readFile(path.join(target, "runtime", "queue.json"), "utf8"), "saved");
    assert.deepEqual(await readdir(path.join(target, "engine")), ["current.py"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("managed skill updates discard an interrupted staging tree before retrying", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "super-speech-skill-stage-recovery-"));
  const previous = path.join(root, "previous");
  const source = path.join(root, "source");
  const target = path.join(root, "target");
  const staging = `${target}.super-speech-managed-staging`;
  try {
    await writeTestTree(previous, { "SKILL.md": "old" });
    await cp(previous, target, { recursive: true });
    await writeTestTree(target, { "runtime/queue.json": "saved" });
    await writeTestTree(source, { "SKILL.md": "new" });
    await writeTestTree(staging, { "SKILL.md": "partial" });

    const result = syncManagedSkillTree(
      source,
      target,
      managedSkillTreeHash(previous),
    );

    assert.equal(result.status, "updated");
    assert.equal(await readFile(path.join(target, "SKILL.md"), "utf8"), "new");
    assert.equal(await readFile(path.join(target, "runtime", "queue.json"), "utf8"), "saved");
    assert.deepEqual(
      (await readdir(root)).filter((name) => name.includes("super-speech-managed")),
      [],
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("managed skill updates finish an interrupted directory cutover", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "super-speech-skill-cutover-recovery-"));
  const previous = path.join(root, "previous");
  const source = path.join(root, "source");
  const target = path.join(root, "target");
  const backup = `${target}.super-speech-managed-backup`;
  const staging = `${target}.super-speech-managed-staging`;
  try {
    await writeTestTree(previous, {
      "SKILL.md": "old",
      "engine/retired.py": "retired",
    });
    await cp(previous, target, { recursive: true });
    await writeTestTree(target, {
      "runtime/queue.json": "saved",
      "engine/build/engine.exe": "built",
    });
    await writeTestTree(source, {
      "SKILL.md": "new",
      "engine/current.py": "current",
    });

    await cp(source, staging, { recursive: true });
    await rename(target, backup);

    const result = syncManagedSkillTree(
      source,
      target,
      managedSkillTreeHash(previous),
    );

    assert.equal(result.status, "current");
    assert.equal(result.hash, managedSkillTreeHash(source));
    assert.equal(await readFile(path.join(target, "runtime", "queue.json"), "utf8"), "saved");
    assert.equal(await readFile(path.join(target, "engine", "build", "engine.exe"), "utf8"), "built");
    assert.deepEqual(await readdir(path.join(target, "engine")), ["build", "current.py"]);
    assert.deepEqual(
      (await readdir(root)).filter((name) => name.includes("super-speech-managed")),
      [],
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("managed skill recovery preserves edits made after an interrupted cutover", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "super-speech-skill-cutover-edit-"));
  const previous = path.join(root, "previous");
  const source = path.join(root, "source");
  const target = path.join(root, "target");
  const backup = `${target}.super-speech-managed-backup`;
  try {
    await writeTestTree(previous, { "SKILL.md": "old" });
    await writeTestTree(source, { "SKILL.md": "new" });
    await cp(previous, backup, { recursive: true });
    await cp(source, target, { recursive: true });
    await writeTestTree(target, {
      "runtime/queue.json": "saved",
      "SKILL.md": "new with a local edit",
    });

    assert.throws(
      () => syncManagedSkillTree(source, target, managedSkillTreeHash(previous)),
      /cannot safely recover an edited agent skill update/,
    );

    assert.equal(
      await readFile(path.join(target, "SKILL.md"), "utf8"),
      "new with a local edit",
    );
    assert.equal(await readFile(path.join(target, "runtime", "queue.json"), "utf8"), "saved");
    assert.equal(await readFile(path.join(backup, "SKILL.md"), "utf8"), "old");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("managed skill updates roll back an incomplete cutover before retrying", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "super-speech-skill-cutover-rollback-"));
  const previous = path.join(root, "previous");
  const source = path.join(root, "source");
  const target = path.join(root, "target");
  const backup = `${target}.super-speech-managed-backup`;
  const staging = `${target}.super-speech-managed-staging`;
  try {
    await writeTestTree(previous, { "SKILL.md": "old" });
    await cp(previous, target, { recursive: true });
    await writeTestTree(target, { "runtime/queue.json": "saved" });
    await writeTestTree(source, { "SKILL.md": "new" });

    await cp(source, staging, { recursive: true });
    await rename(target, backup);
    await rename(path.join(backup, "runtime"), path.join(staging, "runtime"));
    await writeTestTree(staging, { "SKILL.md": "incomplete" });

    const result = syncManagedSkillTree(
      source,
      target,
      managedSkillTreeHash(previous),
    );

    assert.equal(result.status, "updated");
    assert.equal(await readFile(path.join(target, "SKILL.md"), "utf8"), "new");
    assert.equal(await readFile(path.join(target, "runtime", "queue.json"), "utf8"), "saved");
    assert.deepEqual(
      (await readdir(root)).filter((name) => name.includes("super-speech-managed")),
      [],
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("managed skill updates preserve edits outside SKILL.md", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "super-speech-skill-preserve-"));
  const source = path.join(root, "source");
  const target = path.join(root, "target");
  try {
    await writeTestTree(source, {
      "SKILL.md": "new",
      "engine/engine.py": "new engine",
    });
    await writeTestTree(target, {
      "SKILL.md": "old",
      "engine/engine.py": "local edit",
    });

    const result = syncManagedSkillTree(source, target, "not-the-current-hash");

    assert.equal(result.status, "preserved");
    assert.equal(result.hash, "not-the-current-hash");
    assert.equal(await readFile(path.join(target, "engine", "engine.py"), "utf8"), "local edit");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("legacy skill hashes never authorize replacing a managed tree", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "super-speech-skill-legacy-"));
  const source = path.join(root, "source");
  const target = path.join(root, "target");
  try {
    await writeTestTree(source, { "SKILL.md": "new" });
    await writeTestTree(target, { "SKILL.md": "old" });

    const legacyHash = "legacy-SKILL-md-only-hash";
    assert.equal(
      managedSkillHashForTarget(null, legacyHash, {}, target),
      null,
    );
    const result = syncManagedSkillTree(source, target, null);
    assert.equal(result.status, "preserved");
    assert.equal(result.hash, null);
    assert.equal(await readFile(path.join(target, "SKILL.md"), "utf8"), "old");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("per-target hashes do not fall back to another skill's hash", () => {
  const first = path.join("agent-one", "SKILL.md");
  const second = path.join("agent-two", "SKILL.md");
  assert.equal(
    managedSkillHashForTarget(
      MANAGED_SKILL_HASH_KIND,
      "shared-old-hash",
      { [first]: "first-tree-hash" },
      first,
    ),
    "first-tree-hash",
  );
  assert.equal(
    managedSkillHashForTarget(
      MANAGED_SKILL_HASH_KIND,
      "shared-old-hash",
      { [first]: "first-tree-hash" },
      second,
    ),
    null,
  );
});

test("managed skill installation creates a missing skills directory", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "super-speech-skill-install-"));
  const source = path.join(root, "source");
  const target = path.join(root, "agent", "skills", "super-speech");
  try {
    await writeTestTree(source, { "SKILL.md": "skill" });

    const result = syncManagedSkillTree(source, target, null);

    assert.equal(result.status, "installed");
    assert.equal(await readFile(path.join(target, "SKILL.md"), "utf8"), "skill");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("tray playback actions cover both active and future speech", () => {
  assert.equal(trayPlaybackAction("idle"), "pause");
  assert.equal(trayPlaybackAction("holding"), "resume");
  assert.equal(trayPlaybackAction("stopped"), null);
  assert.equal(trayPlaybackAction("playing"), "pause");
  assert.equal(trayPlaybackAction("paused"), "resume");
});
