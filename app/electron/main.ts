import {
  app,
  BrowserWindow,
  clipboard,
  ipcMain,
  Menu,
  nativeImage,
  shell,
  Tray,
} from "electron";
import { spawn, type ChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import {
  closeSync,
  cpSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  ENGINE_STATUS_VERSION,
  IPC_CHANNELS,
  compatibleEngineIsRunning,
  engineProcessIsLive,
  parseEngineStatus,
  parseEngineProcessStatus,
  parsePlayAcceptance,
  runtimeStateForSnapshot,
  statusAfterTransientRead,
  statusAfterPauseCommand,
  statusForEngineProcess,
  type EngineStatus,
  type EngineProcessStatus,
  type PlayAcceptance,
  type RuntimeStatus,
  type VersionInfo,
} from "../src/runtime";

const HEARTBEAT_FRESHNESS_MS = 15_000;
const MODEL_MIN_BYTES = 300_000_000;
const VOICES_MIN_BYTES = 20_000_000;
const SETUP_URL = "https://github.com/reasonmethis/super-speech#install";
const moduleDir = path.dirname(fileURLToPath(import.meta.url));
const rendererDir = path.join(moduleDir, "..", "dist");
const smokeTest = process.argv.includes("--smoke-test");
const startHidden = smokeTest || process.argv.includes("--hidden");

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let ownedEngine: ChildProcess | null = null;
let quitting = false;
let lastEngineStatus: EngineStatus | null = null;

interface EngineLaunch {
  command: string;
  args: string[];
}

interface AgentSkillInstall {
  paths: string[];
  hash: string | null;
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
  const engineRunning = compatibleEngineIsRunning(
    ownedEngineRunning,
    storedEngine,
    storedProcessRunning,
  );

  const state = runtimeStateForSnapshot(
    installed,
    engineRunning,
    engine?.state,
  );
  const timeline = statusUnavailable
    ? null
    : engine ?? (ownedEngineRunning ? lastEngineStatus : null);

  const status: RuntimeStatus = {
    version: ENGINE_STATUS_VERSION,
    state,
    updated_at: engine?.updated_at ?? 0,
    engine_pid: engineRunning ? (engine?.engine_pid ?? ownedEngine?.pid ?? null) : null,
    engine_running: engineRunning,
    installed,
    current: timeline?.current ?? null,
    recent_starts: timeline?.recent_starts ?? [],
    queue_count: timeline?.queue_count ?? 0,
    queue: timeline?.queue ?? [],
    history_count: timeline?.history_count ?? 0,
    history: timeline?.history ?? [],
  };
  return statusAfterPauseCommand(status, existsSync(path.join(base, "PAUSE")));
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

async function playChunk(id: string, voice?: string): Promise<PlayAcceptance> {
  const output = await runEngineCommand("play", id, ...(voice ? ["--voice", voice] : []));
  const acceptance = parsePlayAcceptance(JSON.parse(output));
  if (!acceptance) {
    throw new Error("Engine returned an incomplete play acknowledgement");
  }
  return acceptance;
}

async function moveQueueItem(id: string, beforeId: string | null): Promise<void> {
  await runEngineCommand("move", id, ...(beforeId ? [beforeId] : []));
}

async function moveHistoryItem(id: string, beforeId: string | null): Promise<void> {
  await runEngineCommand("move-history", id, ...(beforeId ? [beforeId] : []));
}

async function archiveQueueItem(id: string): Promise<void> {
  await runEngineCommand("archive", id);
}

async function deleteHistoryItem(id: string): Promise<void> {
  await runEngineCommand("delete", id);
}

async function clearQueue(): Promise<void> {
  await runEngineCommand("clear");
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

function fileHash(filePath: string): string {
  return createHash("sha256").update(readFileSync(filePath)).digest("hex");
}

function previousAgentSkillHash(base: string): string | null {
  try {
    const manifest = JSON.parse(
      readFileSync(path.join(base, "install.json"), "utf8"),
    ) as { agent_skill_hash?: unknown };
    return typeof manifest.agent_skill_hash === "string"
      ? manifest.agent_skill_hash
      : null;
  } catch {
    return null;
  }
}

function installAgentSkills(base: string): AgentSkillInstall {
  const agentHome = process.env.SUPER_SPEECH_AGENT_HOME;
  if ((!app.isPackaged && !agentHome) || process.env.SUPER_SPEECH_SKIP_SKILL_INSTALL) {
    return { paths: [], hash: null };
  }
  const sourceDirectory = packagedSkillDirectory();
  const sourceSkill = path.join(sourceDirectory, "SKILL.md");
  if (!existsSync(sourceSkill)) {
    return { paths: [], hash: null };
  }

  const home = agentHome ?? homedir();
  const sourceHash = fileHash(sourceSkill);
  const previousHash = previousAgentSkillHash(base);
  const installed: string[] = [];
  for (const agentDirectory of [".codex", ".claude"]) {
    const agentRoot = path.join(home, agentDirectory);
    if (!existsSync(agentRoot)) {
      continue;
    }
    const targetDirectory = path.join(agentRoot, "skills", "super-speech");
    const target = path.join(targetDirectory, "SKILL.md");
    if (!existsSync(target) || (previousHash && fileHash(target) === previousHash)) {
      mkdirSync(targetDirectory, { recursive: true });
      cpSync(sourceDirectory, targetDirectory, {
        recursive: true,
        filter: (source) => path.basename(source) !== "runtime",
      });
    }
    installed.push(target);
  }
  return { paths: installed, hash: sourceHash };
}

function writeInstallManifest(
  base: string,
  launch: EngineLaunch | null,
  agentSkills: AgentSkillInstall,
): void {
  writeFileSync(
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
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
}

async function startEngine(): Promise<void> {
  const base = runtimeDir();
  mkdirSync(path.join(base, "queue"), { recursive: true });
  mkdirSync(path.join(base, "spoken"), { recursive: true });
  mkdirSync(path.join(base, "failed"), { recursive: true });
  const launch = engineLaunch();
  writeInstallManifest(base, launch, installAgentSkills(base));

  const storedSnapshot = readStatusSnapshot(base);
  const storedEngine = parseEngineStatus(storedSnapshot);
  const storedProcess = parseEngineProcessStatus(storedSnapshot);
  if (engineIsRunning(base, storedProcess)) {
    if (storedEngine) {
      return;
    }
    if (!(await stopIncompatibleEngine(base))) {
      return;
    }
  }
  if (ownedEngine && ownedEngine.exitCode === null) {
    return;
  }
  if (!launch || !modelsInstalled(modelDirectory())) {
    return;
  }

  const logDescriptor = openSync(path.join(base, "engine.log"), "a");
  try {
    ownedEngine = spawn(launch.command, [...launch.args, "serve"], {
      env: {
        ...process.env,
        SUPER_SPEECH_HOME: base,
        SUPER_SPEECH_MODEL_DIR: modelDirectory(),
      },
      windowsHide: true,
      stdio: ["ignore", logDescriptor, logDescriptor],
    });
    ownedEngine.once("error", () => {
      ownedEngine = null;
    });
    ownedEngine.once("exit", () => {
      ownedEngine = null;
    });
  } catch {
    ownedEngine = null;
  } finally {
    closeSync(logDescriptor);
  }
}

function stopOwnedEngine(): void {
  if (!ownedEngine || ownedEngine.exitCode !== null) {
    return;
  }
  ownedEngine.kill();
  ownedEngine = null;
  const heartbeat = path.join(runtimeDir(), "engine.alive");
  if (existsSync(heartbeat)) {
    unlinkSync(heartbeat);
  }
}

function runSmokeTest(): void {
  const startedAt = Date.now();
  const interval = setInterval(() => {
    const status = getStatus();
    if (
      status.engine_running &&
      status.state === "idle" &&
      status.queue_count === 0 &&
      !status.current
    ) {
      clearInterval(interval);
      console.log("Super Speech desktop smoke test passed");
      app.quit();
      return;
    }
    if (status.state === "setup_required" || status.state === "stopped") {
      clearInterval(interval);
      quitting = true;
      stopOwnedEngine();
      console.error(`Super Speech desktop smoke test failed: ${status.state}`);
      app.exit(1);
      return;
    }
    if (Date.now() - startedAt > 45_000) {
      clearInterval(interval);
      quitting = true;
      stopOwnedEngine();
      console.error("Super Speech desktop smoke test timed out");
      app.exit(1);
    }
  }, 250);
}

async function setPaused(paused: boolean): Promise<RuntimeStatus> {
  await runEngineCommand(paused ? "pause" : "resume");
  const status = statusAfterPauseCommand(getStatus(), paused);
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
  mainWindow = new BrowserWindow({
    title: "Super Speech",
    width: 420,
    height: 680,
    minWidth: 380,
    minHeight: 620,
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
  mainWindow.on("maximize", () => {
    mainWindow?.webContents.send(IPC_CHANNELS.maximizedChanged, true);
  });
  mainWindow.on("unmaximize", () => {
    mainWindow?.webContents.send(IPC_CHANNELS.maximizedChanged, false);
  });
  mainWindow.once("ready-to-show", () => {
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
  const paused = status.state === "paused";
  const canPause = status.state === "playing" || status.state === "paused";
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Open Super Speech", click: showWindow },
      {
        label: paused ? "Resume Speech" : "Pause Speech",
        enabled: canPause,
        click: () => void setPaused(!paused),
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
  ipcMain.handle(IPC_CHANNELS.playChunk, (_event, id: string, voice?: string) =>
    playChunk(id, voice)
  );
  ipcMain.handle(
    IPC_CHANNELS.moveQueueItem,
    (_event, id: string, beforeId: string | null) => moveQueueItem(id, beforeId),
  );
  ipcMain.handle(
    IPC_CHANNELS.moveHistoryItem,
    (_event, id: string, beforeId: string | null) => moveHistoryItem(id, beforeId),
  );
  ipcMain.handle(IPC_CHANNELS.archiveQueueItem, (_event, id: string) =>
    archiveQueueItem(id)
  );
  ipcMain.handle(IPC_CHANNELS.deleteHistoryItem, (_event, id: string) =>
    deleteHistoryItem(id)
  );
  ipcMain.handle(IPC_CHANNELS.copyText, (_event, text: string) => clipboard.writeText(text));
  ipcMain.handle(IPC_CHANNELS.clearQueue, clearQueue);
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
  app.on("before-quit", () => {
    quitting = true;
    stopOwnedEngine();
  });
  app.on("activate", showWindow);
  app.on("window-all-closed", () => {
    // The tray owns the app lifetime
  });
  void app.whenReady().then(async () => {
    Menu.setApplicationMenu(null);
    registerIpc();
    await startEngine();
    createWindow();
    createTray();
    if (smokeTest) {
      runSmokeTest();
    }
  });
}
