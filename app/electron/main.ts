import {
  app,
  BrowserWindow,
  ipcMain,
  Menu,
  nativeImage,
  shell,
  Tray,
} from "electron";
import { spawn, type ChildProcess } from "node:child_process";
import {
  closeSync,
  copyFileSync,
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
  IPC_CHANNELS,
  type EngineStatus,
  type RuntimeState,
  type RuntimeStatus,
} from "../src/runtime";

const HEARTBEAT_FRESHNESS_MS = 15_000;
const MODEL_MIN_BYTES = 300_000_000;
const VOICES_MIN_BYTES = 20_000_000;
const SETUP_URL = "https://github.com/reasonmethis/super-speech#install";
const RUNTIME_STATES = new Set<RuntimeState>([
  "loading",
  "playing",
  "paused",
  "idle",
  "ready",
  "setup_required",
  "stopped",
]);
const moduleDir = path.dirname(fileURLToPath(import.meta.url));
const rendererDir = path.join(moduleDir, "..", "dist");
const smokeTest = process.argv.includes("--smoke-test");
const startHidden = smokeTest || process.argv.includes("--hidden");

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let ownedEngine: ChildProcess | null = null;
let engineStartFailed = false;
let quitting = false;

interface EngineLaunch {
  command: string;
  args: string[];
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
  const sourceEngine = path.join(app.getAppPath(), "..", "drainer-kokoro.py");
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

function engineIsRunning(base: string, status: EngineStatus | null): boolean {
  return (
    heartbeatIsFresh(path.join(base, "drainer.alive")) ||
    Boolean(
      status &&
      Date.now() / 1000 - status.updated_at < 300 &&
      processExists(status.engine_pid),
    )
  );
}

function isEngineStatus(value: unknown): value is EngineStatus {
  if (!value || typeof value !== "object") {
    return false;
  }
  const status = value as Partial<EngineStatus>;
  return (
    typeof status.version === "number" &&
    typeof status.state === "string" &&
    RUNTIME_STATES.has(status.state as RuntimeState) &&
    typeof status.updated_at === "number" &&
    typeof status.queue_count === "number" &&
    Array.isArray(status.queue)
  );
}

function readEngineStatus(base: string): EngineStatus | null {
  try {
    const parsed: unknown = JSON.parse(
      readFileSync(path.join(base, "status.json"), "utf8"),
    );
    return isEngineStatus(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function getStatus(): RuntimeStatus {
  const base = runtimeDir();
  const installed = modelsInstalled(modelDirectory());
  const ownedEngineRunning = ownedEngine !== null && ownedEngine.exitCode === null;
  const paused = existsSync(path.join(base, "PAUSE"));
  const engine = readEngineStatus(base);
  const engineRunning = ownedEngineRunning || engineIsRunning(base, engine);

  let state: RuntimeState;
  if (!installed || engine?.state === "setup_required") {
    state = "setup_required";
  } else if (paused) {
    state = "paused";
  } else if (engineRunning) {
    state = engine?.state ?? "loading";
  } else if (engineStartFailed) {
    state = "stopped";
  } else {
    state = "ready";
  }

  return {
    version: engine?.version ?? 1,
    state,
    updated_at: engine?.updated_at ?? 0,
    engine_pid: engineRunning ? (engine?.engine_pid ?? ownedEngine?.pid ?? null) : null,
    engine_running: engineRunning,
    installed,
    current: engineRunning ? (engine?.current ?? null) : null,
    queue_count: engine?.queue_count ?? 0,
    queue: engine?.queue ?? [],
  };
}

function clearTransientSignals(base: string): void {
  for (const name of ["STOP", "INTERRUPT", "SKIP", "CLEAR"]) {
    const signal = path.join(base, name);
    if (existsSync(signal)) {
      unlinkSync(signal);
    }
  }
}

function packagedSkillPath(): string {
  return app.isPackaged
    ? path.join(process.resourcesPath, "integrations", "super-speech", "SKILL.md")
    : path.join(app.getAppPath(), "resources", "agent-skill", "SKILL.md");
}

function installAgentSkills(): string[] {
  const agentHome = process.env.SUPER_SPEECH_AGENT_HOME;
  if ((!app.isPackaged && !agentHome) || process.env.SUPER_SPEECH_SKIP_SKILL_INSTALL) {
    return [];
  }
  const source = packagedSkillPath();
  if (!existsSync(source)) {
    return [];
  }

  const home = agentHome ?? homedir();
  const installed: string[] = [];
  for (const agentDirectory of [".codex", ".claude"]) {
    const agentRoot = path.join(home, agentDirectory);
    if (!existsSync(agentRoot)) {
      continue;
    }
    const targetDirectory = path.join(agentRoot, "skills", "super-speech");
    const target = path.join(targetDirectory, "SKILL.md");
    if (!existsSync(target)) {
      mkdirSync(targetDirectory, { recursive: true });
      copyFileSync(source, target);
    }
    installed.push(target);
  }
  return installed;
}

function writeInstallManifest(
  base: string,
  launch: EngineLaunch | null,
  agentSkills: string[],
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
        agent_skills: agentSkills,
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
}

function startEngine(): void {
  const base = runtimeDir();
  mkdirSync(path.join(base, "queue"), { recursive: true });
  mkdirSync(path.join(base, "spoken"), { recursive: true });
  mkdirSync(path.join(base, "failed"), { recursive: true });
  const launch = engineLaunch();
  writeInstallManifest(base, launch, installAgentSkills());

  if (engineIsRunning(base, readEngineStatus(base))) {
    engineStartFailed = false;
    return;
  }
  if (ownedEngine && ownedEngine.exitCode === null) {
    return;
  }
  if (!launch || !modelsInstalled(modelDirectory())) {
    engineStartFailed = true;
    return;
  }

  clearTransientSignals(base);
  const logDescriptor = openSync(path.join(base, "engine.log"), "a");
  try {
    ownedEngine = spawn(launch.command, launch.args, {
      env: {
        ...process.env,
        SUPER_SPEECH_HOME: base,
        SUPER_SPEECH_MODEL_DIR: modelDirectory(),
      },
      windowsHide: true,
      stdio: ["ignore", logDescriptor, logDescriptor],
    });
    engineStartFailed = false;
    ownedEngine.once("error", () => {
      engineStartFailed = true;
    });
    ownedEngine.once("exit", (code) => {
      ownedEngine = null;
      if (!quitting && code !== 0) {
        engineStartFailed = true;
      }
    });
  } catch {
    ownedEngine = null;
    engineStartFailed = true;
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
  const heartbeat = path.join(runtimeDir(), "drainer.alive");
  if (existsSync(heartbeat)) {
    unlinkSync(heartbeat);
  }
}

function runSmokeTest(): void {
  const startedAt = Date.now();
  const interval = setInterval(() => {
    const status = getStatus();
    if (status.engine_running && status.state === "idle") {
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

function setPaused(paused: boolean): RuntimeStatus {
  const base = runtimeDir();
  mkdirSync(base, { recursive: true });
  const pausePath = path.join(base, "PAUSE");
  if (paused) {
    writeFileSync(pausePath, "");
  } else if (existsSync(pausePath)) {
    unlinkSync(pausePath);
  }
  refreshTrayMenu();
  return getStatus();
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
    maximizable: false,
    fullscreenable: false,
    frame: false,
    transparent: true,
    backgroundColor: "#0b0d14",
    show: false,
    autoHideMenuBar: true,
    icon: assetPath("icon.ico"),
    webPreferences: {
      preload: path.join(moduleDir, "preload.mjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
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

function refreshTrayMenu(): void {
  if (!tray) {
    return;
  }
  const paused = existsSync(path.join(runtimeDir(), "PAUSE"));
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Open Super Speech", click: showWindow },
      {
        label: paused ? "Resume Speech" : "Pause Speech",
        click: () => setPaused(!paused),
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
  ipcMain.handle(IPC_CHANNELS.setPaused, (_event, paused: boolean) => setPaused(paused));
  ipcMain.handle(IPC_CHANNELS.openSetup, () => shell.openExternal(SETUP_URL));
  ipcMain.handle(IPC_CHANNELS.minimize, (event) => {
    BrowserWindow.fromWebContents(event.sender)?.minimize();
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
  void app.whenReady().then(() => {
    Menu.setApplicationMenu(null);
    registerIpc();
    startEngine();
    createWindow();
    createTray();
    if (smokeTest) {
      runSmokeTest();
    }
  });
}
