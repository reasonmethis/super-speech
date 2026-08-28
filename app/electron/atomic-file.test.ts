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
import { trayPlaybackControl, trayPlaybackControlKey } from "./tray-menu.ts";

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
      JSON.parse(content);
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

test("managed skill hashing preserves runtime and detects edits anywhere else", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "super-speech-skill-hash-"));
  try {
    await mkdir(path.join(directory, "engine"));
    await mkdir(path.join(directory, "runtime"));
    await mkdir(path.join(directory, "engine", "build"));
    await mkdir(path.join(directory, "engine", "__pycache__"));
    await mkdir(path.join(directory, "engine", "super_speech.egg-info"));
    await writeFile(path.join(directory, "SKILL.md"), "skill", "utf8");
    await writeFile(path.join(directory, "engine", "engine.py"), "engine", "utf8");
    await writeFile(path.join(directory, "runtime", "state.json"), "one", "utf8");
    await writeFile(path.join(directory, "engine", "build", "engine.exe"), "build", "utf8");
    await writeFile(path.join(directory, "engine", "__pycache__", "engine.pyc"), "cache", "utf8");
    await writeFile(path.join(directory, "engine", "super_speech.egg-info", "PKG-INFO"), "info", "utf8");
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
    await mkdir(path.join(previous, "engine"), { recursive: true });
    await writeFile(path.join(previous, "SKILL.md"), "old", "utf8");
    await writeFile(path.join(previous, "engine", "retired.py"), "retired", "utf8");
    await cp(previous, target, { recursive: true });
    await mkdir(path.join(target, "runtime"));
    await writeFile(path.join(target, "runtime", "queue.json"), "saved", "utf8");
    await mkdir(path.join(source, "engine"), { recursive: true });
    await writeFile(path.join(source, "SKILL.md"), "new", "utf8");
    await writeFile(path.join(source, "engine", "current.py"), "current", "utf8");

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
    await mkdir(previous);
    await writeFile(path.join(previous, "SKILL.md"), "old", "utf8");
    await cp(previous, target, { recursive: true });
    await mkdir(path.join(target, "runtime"));
    await writeFile(path.join(target, "runtime", "queue.json"), "saved", "utf8");
    await mkdir(source);
    await writeFile(path.join(source, "SKILL.md"), "new", "utf8");

    await mkdir(staging);
    await writeFile(path.join(staging, "SKILL.md"), "partial", "utf8");

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
    await mkdir(path.join(previous, "engine"), { recursive: true });
    await writeFile(path.join(previous, "SKILL.md"), "old", "utf8");
    await writeFile(path.join(previous, "engine", "retired.py"), "retired", "utf8");
    await cp(previous, target, { recursive: true });
    await mkdir(path.join(target, "runtime"));
    await writeFile(path.join(target, "runtime", "queue.json"), "saved", "utf8");
    await mkdir(path.join(target, "engine", "build"));
    await writeFile(path.join(target, "engine", "build", "engine.exe"), "built", "utf8");
    await mkdir(path.join(source, "engine"), { recursive: true });
    await writeFile(path.join(source, "SKILL.md"), "new", "utf8");
    await writeFile(path.join(source, "engine", "current.py"), "current", "utf8");

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
    await mkdir(previous);
    await writeFile(path.join(previous, "SKILL.md"), "old", "utf8");
    await mkdir(source);
    await writeFile(path.join(source, "SKILL.md"), "new", "utf8");
    await cp(previous, backup, { recursive: true });
    await cp(source, target, { recursive: true });
    await mkdir(path.join(target, "runtime"));
    await writeFile(path.join(target, "runtime", "queue.json"), "saved", "utf8");
    await writeFile(path.join(target, "SKILL.md"), "new with a local edit", "utf8");

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
    await mkdir(previous);
    await writeFile(path.join(previous, "SKILL.md"), "old", "utf8");
    await cp(previous, target, { recursive: true });
    await mkdir(path.join(target, "runtime"));
    await writeFile(path.join(target, "runtime", "queue.json"), "saved", "utf8");
    await mkdir(source);
    await writeFile(path.join(source, "SKILL.md"), "new", "utf8");

    await cp(source, staging, { recursive: true });
    await rename(target, backup);
    await rename(path.join(backup, "runtime"), path.join(staging, "runtime"));
    await writeFile(path.join(staging, "SKILL.md"), "incomplete", "utf8");

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
    await mkdir(path.join(source, "engine"), { recursive: true });
    await writeFile(path.join(source, "SKILL.md"), "new", "utf8");
    await writeFile(path.join(source, "engine", "engine.py"), "new engine", "utf8");
    await mkdir(path.join(target, "engine"), { recursive: true });
    await writeFile(path.join(target, "SKILL.md"), "old", "utf8");
    await writeFile(path.join(target, "engine", "engine.py"), "local edit", "utf8");

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
    await mkdir(source);
    await mkdir(target);
    await writeFile(path.join(source, "SKILL.md"), "new", "utf8");
    await writeFile(path.join(target, "SKILL.md"), "old", "utf8");

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
    await mkdir(source);
    await writeFile(path.join(source, "SKILL.md"), "skill", "utf8");

    const result = syncManagedSkillTree(source, target, null);

    assert.equal(result.status, "installed");
    assert.equal(await readFile(path.join(target, "SKILL.md"), "utf8"), "skill");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("tray playback controls change only across pause-relevant states", () => {
  assert.deepEqual(trayPlaybackControl("idle"), {
    enabled: false,
    label: "Pause Speech",
  });
  assert.deepEqual(trayPlaybackControl("playing"), {
    enabled: true,
    label: "Pause Speech",
  });
  assert.deepEqual(trayPlaybackControl("paused"), {
    enabled: true,
    label: "Resume Speech",
  });
  assert.equal(trayPlaybackControlKey("idle"), trayPlaybackControlKey("stopped"));
  assert.notEqual(trayPlaybackControlKey("idle"), trayPlaybackControlKey("playing"));
});
