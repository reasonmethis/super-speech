import {
  app,
  BrowserWindow,
  clipboard,
  dialog,
  ipcMain,
  Menu,
  nativeImage,
  screen,
  shell,
  Tray,
} from "electron";
import { spawn, type ChildProcess } from "node:child_process";
import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  statSync,
  unlinkSync,
} from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  ENGINE_STATUS_VERSION,
  IPC_CHANNELS,
  compatibleEngineIsRunning,
  engineProcessIsLive,
  isSpeechicleId,
  mutationResultMatchesRequest,
  ownedEngineRestartReason,
  parseEngineStatus,
  parseEngineProcessStatus,
  parseTimelineMutation,
  parseTimelineMutationResult,
  runtimeStateForSnapshot,
  runtimeStatusForMutationSnapshot,
  statusAfterTransientRead,
  statusForEngineProcess,
  type EngineStatus,
  type EngineProcessStatus,
  type RuntimeStatus,
  type TimelineMutation,
  type TimelineMutationResult,
  type VersionInfo,
} from "../src/runtime";
import { appendAgentInboxMessage } from "./agent-inbox";
import { writeTextAtomically } from "./atomic-file";
import { parsePlaybackControlAck, runEngineControl } from "./engine-control";
import {
  MANAGED_SKILL_HASH_KIND,
  managedSkillHashForTarget,
  syncManagedSkillTree,
} from "./managed-skill";
import {
  trayPlaybackControl,
  trayPlaybackControlKey,
} from "./tray-menu";
import {
  MINIMUM_WINDOW_SIZE,
  readSavedWindowState,
  restoredWindowBounds,
  writeSavedWindowState,
  type SavedWindowState,
} from "./window-state";

const HEARTBEAT_FRESHNESS_MS = 15_000;
const ENGINE_STABLE_AFTER_MS = 30_000;
const ENGINE_START_TIMEOUT_MS = 120_000;
const ENGINE_UNRESPONSIVE_TIMEOUT_MS = 30_000;
const ENGINE_TERMINATE_TIMEOUT_MS = 5_000;
const ENGINE_RESTART_MAX_DELAY_MS = 30_000;
const ENGINE_FAILURES_BEFORE_ERROR = 3;
const MODEL_MIN_BYTES = 300_000_000;
const VOICES_MIN_BYTES = 20_000_000;
const SETUP_URL = "https://github.com/reasonmethis/super-speech#desktop-app";
const WINDOW_STATE_FILENAME = "window-state.json";
const WINDOW_STATE_SAVE_DELAY_MS = 250;
const moduleDir = path.dirname(fileURLToPath(import.meta.url));
const rendererDir = path.join(moduleDir, "..", "dist");
const smokeTest = process.argv.includes("--smoke-test");
const startHidden = smokeTest || process.argv.includes("--hidden");

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let ownedEngine: ChildProcess | null = null;
let ownedEngineStartedAt: number | null = null;
let ownedEngineReadyAt: number | null = null;
let ownedEngineTerminationRequestedAt: number | null = null;
let engineStartPromise: Promise<void> | null = null;
let engineHealthTimer: NodeJS.Timeout | null = null;
let engineRestartFailures = 0;
let engineRestartTimer: NodeJS.Timeout | null = null;
let quitting = false;
let lastEngineStatus: EngineStatus | null = null;
let lastTrayPlaybackControlKey: string | null = null;
let windowStateSaveTimer: NodeJS.Timeout | null = null;
let windowStateWrite = Promise.resolve();
let windowStateFlushStarted = false;
let inboxMessageWrite = Promise.resolve();

interface EngineLaunch {
  command: string;
  args: string[];
}

interface AgentSkillInstall {
  paths: string[];
  hash: string | null;
  hashKind: string | null;
  hashes: Record<string, string>;
}

function runtimeDir(): string {
  return process.env.SUPER_SPEECH_HOME ?? path.join(homedir(), ".super-speech");
}

function fileExceeds(filePath: string, minimumBytes: number): boolean {
  try {
    return statSync(filePath).size >= minimumBytes;
  } catch {
    return false;
  }
}

function modelDirectory(): string {
  if (process.env.SUPER_SPEECH_MODEL_DIR) {
    return process.env.SUPER_SPEECH_MODEL_DIR;
  }
  return app.isPackaged
    ? path.join(process.resourcesPath, "models", "kokoro")
    : path.join(app.getAppPath(), "build-resources", "models", "kokoro");
}

function modelsInstalled(models: string): boolean {
  return (
    fileExceeds(path.join(models, "kokoro-v1.0.onnx"), MODEL_MIN_BYTES) &&
    fileExceeds(path.join(models, "voices-v1.0.bin"), VOICES_MIN_BYTES)
  );
}

function engineLaunch(): EngineLaunch | null {
  const executableName = process.platform === "win32"
    ? "super-speech-engine.exe"
    : "super-speech-engine";
  const configuredEngine = process.env.SUPER_SPEECH_ENGINE_PATH;
  if (configuredEngine) {
    return { command: configuredEngine, args: [] };
  }
  const bundledEngine = app.isPackaged
    ? path.join(process.resourcesPath, "engine", executableName)
    : path.join(app.getAppPath(), "build-resources", "engine", executableName);
  if (existsSync(bundledEngine)) {
    return { command: bundledEngine, args: [] };
  }
  const python = process.env.SUPER_SPEECH_PYTHON;
  const sourceEngine = path.join(
    app.getAppPath(),
    "..",
    "skills",
    "super-speech",
    "engine",
    "super_speech_engine.py",
  );
  return python && existsSync(sourceEngine)
    ? { command: python, args: [sourceEngine] }
    : null;
}

function heartbeatIsFresh(filePath: string): boolean {
  try {
    return Date.now() - statSync(filePath).mtimeMs < HEARTBEAT_FRESHNESS_MS;
  } catch {
    return false;
  }
}

function processExists(processId: number | null | undefined): boolean {
  if (!processId) {
    return false;
  }
  try {
    process.kill(processId, 0);
    return true;
  } catch {
    return false;
  }
}

function engineIsRunning(base: string, status: EngineProcessStatus | null): boolean {
  return engineProcessIsLive(
    status,
    heartbeatIsFresh(path.join(base, "engine.alive")),
    processExists,
  );
}

function readStatusSnapshot(base: string): unknown {
  try {
    return JSON.parse(readFileSync(path.join(base, "status.json"), "utf8"));
  } catch {
    return null;
  }
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function stopIncompatibleEngine(base: string): Promise<boolean> {
  try {
    await runEngineCommand("interrupt");
  } catch {
    return false;
  }
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    if (!engineIsRunning(base, parseEngineProcessStatus(readStatusSnapshot(base)))) {
      return true;
    }
    await delay(50);
  }
  return false;
}

function getStatus(): RuntimeStatus {
  const base = runtimeDir();
  const installed = modelsInstalled(modelDirectory());
  const ownedEngineRunning = ownedEngine !== null && ownedEngine.exitCode === null;
  const statusUnavailable = existsSync(path.join(base, "status.failed"));
  const storedSnapshot = statusUnavailable ? {} : readStatusSnapshot(base);
  const storedEngine = statusAfterTransientRead(storedSnapshot, lastEngineStatus);
  if (storedSnapshot !== null && storedEngine) {
    lastEngineStatus = storedEngine;
  }
  const storedProcess = parseEngineProcessStatus(storedSnapshot) ??
    (storedSnapshot === null ? lastEngineStatus : null);
  const engine = ownedEngineRunning
    ? statusForEngineProcess(storedEngine, ownedEngine?.pid)
    : storedEngine;
  const storedProcessRunning = engineIsRunning(base, storedProcess);
  const engineProcessRunning = compatibleEngineIsRunning(
    engine,
    storedProcessRunning,
  );
  const engineReady = engineProcessRunning &&
    engine?.state !== "loading" &&
    engine?.state !== "stopped";
  if (engineReady && ownedEngineRunning) {
    ownedEngineReadyAt ??= Date.now();
    if (Date.now() - ownedEngineReadyAt >= ENGINE_STABLE_AFTER_MS) {
      engineRestartFailures = 0;
    }
  } else if (!ownedEngineRunning) {
    ownedEngineReadyAt = null;
  }
  // A compatible external engine is already serving the shared runtime
  if (engineReady && !ownedEngineRunning) {
    engineRestartFailures = 0;
  }
  const engineRunning = engineProcessRunning && engine?.state !== "stopped";
  // Start recovery now instead of waiting for the one-second health check
  const recoveryExpected = !engineRunning && engineCanRecover(installed);
  if (
    recoveryExpected &&
    !ownedEngineRunning &&
    !engineRestartTimer &&
    !engineStartPromise
  ) {
    void startEngine();
  }
  // After three failed starts, show Stopped between retry attempts
  const recoveryVisible = recoveryExpected && (
    engineStartPromise !== null ||
    ownedEngineRunning ||
    engineRestartFailures < ENGINE_FAILURES_BEFORE_ERROR
  );

  const state = runtimeStateForSnapshot(
    installed,
    engineRunning,
    engineRunning ? engine?.state : recoveryVisible ? "loading" : undefined,
  );
  const timeline = statusUnavailable ? null : engine;

  const status: RuntimeStatus = {
    version: ENGINE_STATUS_VERSION,
    timeline_revision: timeline?.timeline_revision ?? 0,
    state,
    updated_at: engine?.updated_at ?? 0,
    engine_pid: engineRunning
      ? (timeline?.engine_pid ?? ownedEngine?.pid ?? null)
      : null,
    engine_running: engineRunning,
    installed,
    current: timeline?.current ?? null,
    queue_count: timeline?.queue_count ?? 0,
    queue: timeline?.queue ?? [],
    history_count: timeline?.history_count ?? 0,
    history: timeline?.history ?? [],
  };
  return status;
}

function runEngineCommand(...arguments_: string[]): Promise<string> {
  const launch = engineLaunch();
  if (!launch) {
    return Promise.reject(new Error("The Super Speech engine is not installed"));
  }
  return new Promise((resolve, reject) => {
    const child = spawn(launch.command, [...launch.args, ...arguments_], {
      env: {
        ...process.env,
        SUPER_SPEECH_HOME: runtimeDir(),
        SUPER_SPEECH_MODEL_DIR: modelDirectory(),
      },
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    let errorText = "";
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (data: string) => {
      output += data;
    });
    child.stderr?.setEncoding("utf8");
    child.stderr?.on("data", (data: string) => {
      errorText += data;
    });
    child.once("error", reject);
    child.once("exit", (code) => {
      if (code === 0) {
        resolve(output.trim());
      } else {
        reject(new Error(errorText.trim() || `Engine command exited with code ${code}`));
      }
    });
  });
}

function runtimeMutationResult(
  result: TimelineMutationResult,
): TimelineMutationResult<RuntimeStatus> {
  const runtime = getStatus();
  const snapshot = runtimeStatusForMutationSnapshot(result.snapshot, runtime);
  return { ...result, snapshot };
}

async function controlEngine(payload: object): Promise<{
  enginePid: number;
  result: unknown;
}> {
  const status = getStatus();
  if (!status.engine_running || status.engine_pid === null) {
    throw new Error("The Super Speech engine is not running");
  }
  return {
    enginePid: status.engine_pid,
    result: await runEngineControl(runtimeDir(), status.engine_pid, payload),
  };
}

async function mutateTimeline(
  input: unknown,
): Promise<TimelineMutationResult<RuntimeStatus>> {
  const mutation = parseTimelineMutation(input);
  if (!mutation) {
    throw new Error("Invalid timeline mutation");
  }
  const { result: value } = await controlEngine({ command: "mutate", mutation });
  const result = parseTimelineMutationResult(value);
  if (!result) {
    throw new Error("Engine protocol error: incomplete timeline mutation result");
  }
  if (!mutationResultMatchesRequest(mutation, result)) {
    throw new Error("Engine protocol error: timeline mutation did not confirm the selected ID");
  }
  return runtimeMutationResult(result);
}

function sendInboxMessage(speechicleId: unknown, text: unknown): Promise<void> {
  if (!isSpeechicleId(speechicleId) || typeof text !== "string") {
    return Promise.reject(new Error("Invalid agent message"));
  }
  const write = inboxMessageWrite.then(async () => {
    const status = getStatus();
    const item = [
      ...(status.current ? [status.current] : []),
      ...status.queue,
      ...status.history,
    ].find(({ id }) => id === speechicleId);
    if (!item?.inbox) {
      throw new Error("This Speechicle does not have an agent inbox");
    }
    await appendAgentInboxMessage(item.inbox, {
      speechicleId,
      ...(item.source === undefined ? {} : { source: item.source }),
      text,
    });
  });
  inboxMessageWrite = write.catch(() => undefined);
  return write;
}

async function getVersions(): Promise<VersionInfo> {
  const engine = await runEngineCommand("--version").catch(() => "unavailable");
  return { app: app.getVersion(), engine };
}

function packagedSkillDirectory(): string {
  return app.isPackaged
    ? path.join(process.resourcesPath, "integrations", "super-speech")
    : path.join(app.getAppPath(), "..", "skills", "super-speech");
}

function previousAgentSkillInstall(base: string): AgentSkillInstall {
  try {
    const manifest = JSON.parse(
      readFileSync(path.join(base, "install.json"), "utf8"),
    ) as Record<string, unknown>;
    const paths = Array.isArray(manifest.agent_skills)
      ? manifest.agent_skills.filter((item): item is string => typeof item === "string")
      : [];
    const hashes = manifest.agent_skill_hashes &&
        typeof manifest.agent_skill_hashes === "object"
      ? Object.fromEntries(
          Object.entries(manifest.agent_skill_hashes).filter(
            (entry): entry is [string, string] => typeof entry[1] === "string",
          ),
        )
      : {};
    return {
      paths,
      hash: typeof manifest.agent_skill_hash === "string"
        ? manifest.agent_skill_hash
        : null,
      hashKind: typeof manifest.agent_skill_hash_kind === "string"
        ? manifest.agent_skill_hash_kind
        : null,
      hashes,
    };
  } catch {
    return { paths: [], hash: null, hashKind: null, hashes: {} };
  }
}

function installAgentSkills(previous: AgentSkillInstall): AgentSkillInstall {
  const agentHome = process.env.SUPER_SPEECH_AGENT_HOME;
  if ((!app.isPackaged && !agentHome) || process.env.SUPER_SPEECH_SKIP_SKILL_INSTALL) {
    return previous;
  }
  const sourceDirectory = packagedSkillDirectory();
  const sourceSkill = path.join(sourceDirectory, "SKILL.md");
  if (!existsSync(sourceSkill)) {
    return previous;
  }

  const home = agentHome ?? homedir();
  const installed: string[] = [];
  const hashes: Record<string, string> = {};
  let sourceHash: string | null = null;
  for (const agentDirectory of [".codex", ".claude"]) {
    const agentRoot = path.join(home, agentDirectory);
    if (!existsSync(agentRoot)) {
      continue;
    }
    const targetDirectory = path.join(agentRoot, "skills", "super-speech");
    const target = path.join(targetDirectory, "SKILL.md");
    const previousHash = managedSkillHashForTarget(
      previous.hashKind,
      previous.hash,
      previous.hashes,
      target,
    );
    try {
      const result = syncManagedSkillTree(
        sourceDirectory,
        targetDirectory,
        previousHash,
      );
      if (result.hash) {
        hashes[target] = result.hash;
        sourceHash = result.sourceHash;
      }
    } catch (error) {
      console.error(`Could not update the agent skill at ${targetDirectory}`, error);
      if (previousHash) {
        hashes[target] = previousHash;
      }
    }
    installed.push(target);
  }
  return {
    paths: installed,
    hash: sourceHash ?? previous.hash,
    hashKind: Object.keys(hashes).length > 0
      ? MANAGED_SKILL_HASH_KIND
      : previous.hashKind,
    hashes,
  };
}

async function writeInstallManifest(
  base: string,
  launch: EngineLaunch | null,
  agentSkills: AgentSkillInstall,
): Promise<void> {
  await writeTextAtomically(
    path.join(base, "install.json"),
    `${JSON.stringify(
      {
        version: app.getVersion(),
        app_path: process.execPath,
        engine_path: launch?.args.length === 0 ? launch.command : null,
        runtime_home: base,
        model_directory: modelDirectory(),
        agent_skills: agentSkills.paths,
        agent_skill_hash: agentSkills.hash,
        agent_skill_hash_kind: agentSkills.hashKind,
        agent_skill_hashes: agentSkills.hashes,
      },
      null,
      2,
    )}\n`,
  );
}

function scheduleEngineRestart(): void {
  if (quitting || engineRestartTimer) {
    return;
  }
  const delayMilliseconds = Math.min(
    1_000 * 2 ** engineRestartFailures,
    ENGINE_RESTART_MAX_DELAY_MS,
  );
  engineRestartFailures += 1;
  engineRestartTimer = setTimeout(() => {
    engineRestartTimer = null;
    if (!quitting) {
      void startEngine();
    }
  }, delayMilliseconds);
}

function startEngine(): Promise<void> {
  if (quitting) {
    return Promise.resolve();
  }
  if (!engineStartPromise) {
    // App startup, status polling, and the watchdog may all notice the same stopped engine
    engineStartPromise = startEngineOnce()
      .then((started) => {
        if (!started) {
          scheduleEngineRestart();
        }
      })
      .catch((error) => {
        console.error("Super Speech engine startup failed", error);
        scheduleEngineRestart();
      })
      .finally(() => {
        engineStartPromise = null;
      });
  }
  return engineStartPromise;
}

async function startEngineOnce(): Promise<boolean> {
  if (quitting) {
    return true;
  }
  const base = runtimeDir();
  mkdirSync(path.join(base, "queue"), { recursive: true });
  mkdirSync(path.join(base, "spoken"), { recursive: true });
  mkdirSync(path.join(base, "failed"), { recursive: true });
  const launch = engineLaunch();
  const previousAgentSkills = previousAgentSkillInstall(base);
  let agentSkills = previousAgentSkills;
  try {
    agentSkills = installAgentSkills(previousAgentSkills);
  } catch (error) {
    console.error("Could not synchronize the optional agent skill", error);
  }
  await writeInstallManifest(base, launch, agentSkills);

  const storedSnapshot = readStatusSnapshot(base);
  const storedEngine = parseEngineStatus(storedSnapshot);
  const storedProcess = parseEngineProcessStatus(storedSnapshot);
  if (engineIsRunning(base, storedProcess)) {
    if (storedEngine) {
      return true;
    }
    if (!(await stopIncompatibleEngine(base))) {
      return false;
    }
    if (quitting) {
      return true;
    }
  }
  if (ownedEngine && ownedEngine.exitCode === null) {
    return true;
  }
  if (!launch || !modelsInstalled(modelDirectory())) {
    return true;
  }
  if (quitting) {
    return true;
  }

  const logDescriptor = openSync(path.join(base, "engine.log"), "a");
  try {
    const child = spawn(launch.command, [...launch.args, "serve"], {
      env: {
        ...process.env,
        SUPER_SPEECH_HOME: base,
        SUPER_SPEECH_MODEL_DIR: modelDirectory(),
      },
      windowsHide: true,
      stdio: ["ignore", logDescriptor, logDescriptor],
    });
    ownedEngine = child;
    ownedEngineStartedAt = Date.now();
    ownedEngineReadyAt = null;
    ownedEngineTerminationRequestedAt = null;
    const restartAfterUnexpectedExit = () => {
      if (ownedEngine !== child) {
        return;
      }
      ownedEngine = null;
      ownedEngineStartedAt = null;
      ownedEngineReadyAt = null;
      ownedEngineTerminationRequestedAt = null;
      scheduleEngineRestart();
    };
    child.once("error", restartAfterUnexpectedExit);
    child.once("exit", restartAfterUnexpectedExit);
    return true;
  } catch {
    ownedEngine = null;
    ownedEngineStartedAt = null;
    ownedEngineReadyAt = null;
    ownedEngineTerminationRequestedAt = null;
    return false;
  } finally {
    closeSync(logDescriptor);
  }
}

function engineCanRecover(installed: boolean): boolean {
  return !quitting && installed && engineLaunch() !== null;
}

function restartUnhealthyOwnedEngine(reason: string): void {
  const child = ownedEngine;
  if (!child || child.exitCode !== null) {
    return;
  }
  console.error(`Super Speech engine ${reason}; restarting`);
  if (child.kill()) {
    ownedEngineTerminationRequestedAt = Date.now();
  }
}

function startEngineHealthMonitor(): void {
  if (engineHealthTimer) {
    return;
  }
  engineHealthTimer = setInterval(() => {
    const status = getStatus();
    refreshTrayMenu(status);
    if (
      ownedEngine &&
      ownedEngine.exitCode === null &&
      ownedEngineTerminationRequestedAt !== null
    ) {
      if (
        Date.now() - ownedEngineTerminationRequestedAt >=
        ENGINE_TERMINATE_TIMEOUT_MS
      ) {
        console.error("Super Speech engine did not exit after termination; forcing it");
        ownedEngine.kill("SIGKILL");
        ownedEngineTerminationRequestedAt = Date.now();
      }
      return;
    }
    if (ownedEngine && ownedEngine.exitCode === null && ownedEngineStartedAt !== null) {
      const ownedPublication = statusForEngineProcess(
        lastEngineStatus,
        ownedEngine.pid,
      );
      const restartReason = ownedEngineRestartReason(
        {
          startedAtMs: ownedEngineStartedAt,
          ready: ownedEngineReadyAt !== null,
          statusUpdatedAtSeconds: ownedPublication?.updated_at ?? null,
        },
        Date.now(),
        ENGINE_START_TIMEOUT_MS,
        ENGINE_UNRESPONSIVE_TIMEOUT_MS,
      );
      if (restartReason) {
        restartUnhealthyOwnedEngine(restartReason);
        return;
      }
    }
    if (
      !quitting &&
      !engineRestartTimer &&
      !engineStartPromise &&
      engineCanRecover(status.installed) &&
      !status.engine_running
    ) {
      void startEngine();
    }
  }, 1_000);
}

function stopEngineHealthMonitor(): void {
  if (engineHealthTimer) {
    clearInterval(engineHealthTimer);
    engineHealthTimer = null;
  }
}

function stopOwnedEngine(): void {
  if (engineRestartTimer) {
    clearTimeout(engineRestartTimer);
    engineRestartTimer = null;
  }
  if (!ownedEngine || ownedEngine.exitCode !== null) {
    ownedEngineStartedAt = null;
    ownedEngineReadyAt = null;
    ownedEngineTerminationRequestedAt = null;
    return;
  }
  ownedEngine.kill();
  ownedEngine = null;
  ownedEngineStartedAt = null;
  ownedEngineReadyAt = null;
  ownedEngineTerminationRequestedAt = null;
  const heartbeat = path.join(runtimeDir(), "engine.alive");
  if (existsSync(heartbeat)) {
    unlinkSync(heartbeat);
  }
}

function runSmokeTest(): void {
  const startedAt = Date.now();
  let firstEnginePid: number | null = null;
  let restartedAt: number | null = null;
  const interval = setInterval(() => {
    const status = getStatus();
    if (
      firstEnginePid === null &&
      status.engine_running &&
      status.state === "idle" &&
      status.queue_count === 0 &&
      !status.current
    ) {
      if (!ownedEngine?.pid || status.engine_pid !== ownedEngine.pid || !ownedEngine.kill()) {
        clearInterval(interval);
        quitting = true;
        stopOwnedEngine();
        console.error("Super Speech desktop smoke test could not stop its engine fixture");
        app.exit(1);
        return;
      }
      firstEnginePid = status.engine_pid;
      return;
    }
    if (
      firstEnginePid !== null &&
      status.engine_running &&
      status.engine_pid !== firstEnginePid &&
      status.state === "idle" &&
      status.queue_count === 0 &&
      !status.current
    ) {
      restartedAt ??= Date.now();
      if (Date.now() - restartedAt >= 5_000) {
        clearInterval(interval);
        console.log("Super Speech desktop supervision smoke test passed");
        app.quit();
      }
      return;
    }
    restartedAt = null;
    if (
      status.state === "setup_required" ||
      (firstEnginePid === null && status.state === "stopped")
    ) {
      clearInterval(interval);
      quitting = true;
      stopOwnedEngine();
      console.error(`Super Speech desktop smoke test failed: ${status.state}`);
      app.exit(1);
      return;
    }
    if (Date.now() - startedAt > 90_000) {
      clearInterval(interval);
      quitting = true;
      stopOwnedEngine();
      console.error("Super Speech desktop smoke test timed out");
      app.exit(1);
    }
  }, 250);
}

async function setPaused(paused: boolean): Promise<RuntimeStatus> {
  const { enginePid, result } = await controlEngine({
    command: paused ? "pause" : "resume",
  });
  const ack = parsePlaybackControlAck(result, enginePid);
  if (!ack) {
    throw new Error("Engine protocol error: playback command returned invalid acknowledgement");
  }
  const runtime = getStatus();
  if (runtime.engine_pid !== enginePid) {
    throw new Error("The Super Speech engine restarted during the playback command");
  }
  const status: RuntimeStatus = {
    ...runtime,
    state: runtime.current ? ack.state : "idle",
    updated_at: Math.max(runtime.updated_at, ack.updated_at),
  };
  const expectedStates = paused ? ["paused", "idle"] : ["playing", "idle"];
  if (!expectedStates.includes(status.state)) {
    throw new Error("The engine did not enter the requested playback state");
  }
  refreshTrayMenu(status);
  return status;
}

function assetPath(name: string): string {
  return app.isPackaged
    ? path.join(process.resourcesPath, "assets", name)
    : path.join(app.getAppPath(), "build", name);
}

function noticesPath(): string {
  return app.isPackaged
    ? path.join(process.resourcesPath, "THIRD_PARTY_NOTICES.md")
    : path.join(app.getAppPath(), "..", "THIRD_PARTY_NOTICES.md");
}

function windowStatePath(): string {
  return path.join(app.getPath("userData"), WINDOW_STATE_FILENAME);
}

function captureWindowState(window: BrowserWindow): SavedWindowState {
  return {
    bounds: window.getNormalBounds(),
    maximized: window.isMaximized(),
  };
}

function persistWindowState(window: BrowserWindow): Promise<void> {
  const state = captureWindowState(window);
  windowStateWrite = windowStateWrite
    .then(() => writeSavedWindowState(windowStatePath(), state))
    .catch((error) => {
      console.error("Could not save window position", error);
    });
  return windowStateWrite;
}

function scheduleWindowStateSave(window: BrowserWindow): void {
  if (windowStateSaveTimer) {
    clearTimeout(windowStateSaveTimer);
  }
  windowStateSaveTimer = setTimeout(() => {
    windowStateSaveTimer = null;
    if (!window.isDestroyed()) {
      void persistWindowState(window);
    }
  }, WINDOW_STATE_SAVE_DELAY_MS);
}

function flushWindowState(window: BrowserWindow): Promise<void> {
  if (windowStateSaveTimer) {
    clearTimeout(windowStateSaveTimer);
    windowStateSaveTimer = null;
  }
  return persistWindowState(window);
}

function showWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow();
    return;
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.show();
  mainWindow.focus();
}

function createWindow(): void {
  const primaryDisplay = screen.getPrimaryDisplay();
  const displayWorkAreas = [
    primaryDisplay.workArea,
    ...screen.getAllDisplays()
      .filter(({ id }) => id !== primaryDisplay.id)
      .map(({ workArea }) => workArea),
  ];
  const savedWindowState = readSavedWindowState(windowStatePath());
  const initialBounds = restoredWindowBounds(savedWindowState, displayWorkAreas);
  mainWindow = new BrowserWindow({
    title: "Super Speech",
    ...initialBounds,
    minWidth: Math.min(MINIMUM_WINDOW_SIZE.width, initialBounds.width),
    minHeight: Math.min(MINIMUM_WINDOW_SIZE.height, initialBounds.height),
    resizable: true,
    maximizable: true,
    fullscreenable: false,
    frame: false,
    roundedCorners: false,
    transparent: false,
    backgroundColor: "#0b0d14",
    show: false,
    autoHideMenuBar: true,
    icon: assetPath("icon.ico"),
    webPreferences: {
      preload: path.join(moduleDir, "preload.mjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  mainWindow.on("close", (event) => {
    if (!quitting) {
      event.preventDefault();
      mainWindow?.hide();
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  mainWindow.on("move", () => {
    if (mainWindow) {
      scheduleWindowStateSave(mainWindow);
    }
  });
  mainWindow.on("resize", () => {
    if (mainWindow) {
      scheduleWindowStateSave(mainWindow);
    }
  });
  mainWindow.on("maximize", () => {
    mainWindow?.webContents.send(IPC_CHANNELS.maximizedChanged, true);
    if (mainWindow) {
      scheduleWindowStateSave(mainWindow);
    }
  });
  mainWindow.on("unmaximize", () => {
    mainWindow?.webContents.send(IPC_CHANNELS.maximizedChanged, false);
    if (mainWindow) {
      scheduleWindowStateSave(mainWindow);
    }
  });
  mainWindow.once("ready-to-show", () => {
    // Reapply saved bounds after Windows creates the frameless native window
    mainWindow?.setBounds(initialBounds);
    if (savedWindowState?.maximized) {
      mainWindow?.maximize();
    }
    if (!startHidden) {
      mainWindow?.show();
    }
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://") || url.startsWith("http://")) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });

  if (process.env.VITE_DEV_SERVER_URL) {
    void mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else {
    void mainWindow.loadFile(path.join(rendererDir, "index.html"));
  }
}

function refreshTrayMenu(status = getStatus()): void {
  if (!tray) {
    return;
  }
  const controlKey = trayPlaybackControlKey(status.state);
  if (controlKey === lastTrayPlaybackControlKey) {
    return;
  }
  lastTrayPlaybackControlKey = controlKey;
  const control = trayPlaybackControl(status.state);
  const paused = status.state === "paused";
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Open Super Speech", click: showWindow },
      {
        label: control.label,
        enabled: control.enabled,
        click: () => {
          void setPaused(!paused).catch((error) => {
            console.error(
              `Could not ${paused ? "resume" : "pause"} speech from the tray`,
              error,
            );
            dialog.showErrorBox(
              "Super Speech",
              `Could not ${paused ? "resume" : "pause"} speech. Try again.`,
            );
          });
        },
      },
      { label: "Open runtime folder", click: () => void shell.openPath(runtimeDir()) },
      { label: "Third-party notices", click: () => void shell.openPath(noticesPath()) },
      { type: "separator" },
      {
        label: "Quit",
        click: () => {
          quitting = true;
          app.quit();
        },
      },
    ]),
  );
}

function createTray(): void {
  const name = process.platform === "darwin" ? "tray-iconTemplate.png" : "tray-icon.ico";
  const image = nativeImage.createFromPath(assetPath(name));
  tray = new Tray(image);
  tray.setToolTip("Super Speech");
  tray.on("click", showWindow);
  refreshTrayMenu();
}

function registerIpc(): void {
  ipcMain.handle(IPC_CHANNELS.getStatus, getStatus);
  ipcMain.handle(IPC_CHANNELS.getVersions, getVersions);
  ipcMain.handle(IPC_CHANNELS.setPaused, (_event, paused: boolean) => setPaused(paused));
  ipcMain.handle(IPC_CHANNELS.mutateTimeline, (_event, mutation: TimelineMutation) =>
    mutateTimeline(mutation)
  );
  ipcMain.handle(IPC_CHANNELS.sendInboxMessage, (_event, speechicleId, text) =>
    sendInboxMessage(speechicleId, text)
  );
  ipcMain.handle(IPC_CHANNELS.copyText, (_event, text: string) => clipboard.writeText(text));
  ipcMain.handle(IPC_CHANNELS.openSetup, () => shell.openExternal(SETUP_URL));
  ipcMain.handle(IPC_CHANNELS.minimize, (event) => {
    BrowserWindow.fromWebContents(event.sender)?.minimize();
  });
  ipcMain.handle(IPC_CHANNELS.toggleMaximize, (event) => {
    const window = BrowserWindow.fromWebContents(event.sender);
    if (!window) {
      return;
    }
    if (window.isMaximized()) {
      window.unmaximize();
      return;
    }
    window.maximize();
  });
  ipcMain.handle(IPC_CHANNELS.hide, (event) => {
    BrowserWindow.fromWebContents(event.sender)?.hide();
  });
}

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv) => {
    if (!argv.includes("--hidden")) {
      showWindow();
    }
  });
  app.on("before-quit", (event) => {
    quitting = true;
    stopEngineHealthMonitor();
    stopOwnedEngine();
    if (!windowStateFlushStarted && mainWindow && !mainWindow.isDestroyed()) {
      event.preventDefault();
      windowStateFlushStarted = true;
      void flushWindowState(mainWindow).finally(() => app.quit());
    }
  });
  app.on("activate", showWindow);
  app.on("window-all-closed", () => {
    // The tray owns the app lifetime
  });
  void app.whenReady().then(async () => {
    Menu.setApplicationMenu(null);
    registerIpc();
    await startEngine();
    startEngineHealthMonitor();
    createWindow();
    createTray();
    if (smokeTest) {
      runSmokeTest();
    }
  });
}
