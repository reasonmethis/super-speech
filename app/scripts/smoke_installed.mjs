import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const appDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const expectedVersion = JSON.parse(
  readFileSync(path.join(appDirectory, "package.json"), "utf8"),
).version;

function standardInstalledApp() {
  if (process.platform === "win32") {
    const localAppData = process.env.LOCALAPPDATA;
    if (!localAppData) {
      throw new Error("LOCALAPPDATA is unavailable; set SUPER_SPEECH_INSTALLED_APP");
    }
    return path.join(
      localAppData,
      "Programs",
      "super-speech-app",
      "Super Speech.exe",
    );
  }
  if (process.platform === "darwin") {
    return "/Applications/Super Speech.app/Contents/MacOS/Super Speech";
  }
  throw new Error("Set SUPER_SPEECH_INSTALLED_APP on this platform");
}

const installedApp = process.env.SUPER_SPEECH_INSTALLED_APP ?? standardInstalledApp();

if (!existsSync(installedApp)) {
  throw new Error(`Installed Super Speech app not found: ${installedApp}`);
}

const root = mkdtempSync(path.join(tmpdir(), "super-speech-installed-smoke-"));
const runtime = path.join(root, "runtime");
const profile = path.join(root, "profile");
let output = "";

try {
  const child = spawn(installedApp, ["--smoke-test", `--user-data-dir=${profile}`], {
    env: {
      ...process.env,
      SUPER_SPEECH_HOME: runtime,
      SUPER_SPEECH_SILENT: "1",
      SUPER_SPEECH_SKIP_SKILL_INSTALL: "1",
    },
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (data) => {
    output += data;
  });
  child.stderr.on("data", (data) => {
    output += data;
  });

  const exitCode = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      child.kill();
      reject(new Error("Installed supervision smoke test timed out"));
    }, 120_000);
    child.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.once("exit", (code) => {
      clearTimeout(timeout);
      resolve(code);
    });
  });

  if (exitCode !== 0) {
    const engineLog = path.join(runtime, "engine.log");
    const details = existsSync(engineLog) ? readFileSync(engineLog, "utf8") : output;
    throw new Error(`Installed supervision smoke test failed (${exitCode})\n${details}`);
  }

  const manifest = JSON.parse(readFileSync(path.join(runtime, "install.json"), "utf8"));
  assert.equal(manifest.version, expectedVersion, "The installed app version is stale");
  console.log(`Super Speech ${expectedVersion} installed supervision smoke test passed`);
} finally {
  rmSync(root, { recursive: true, force: true, maxRetries: 50, retryDelay: 100 });
}
