import {
  app,
  BrowserWindow,
  ipcMain,
  Menu,
  nativeImage,
  shell,
  Tray,
} from "electron";
import {
  existsSync,
  mkdirSync,
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
const startHidden = process.argv.includes("--hidden");

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;

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

function modelsInstalled(base: string): boolean {
  const models = path.join(base, "models", "kokoro");
  return (
    fileExceeds(path.join(models, "kokoro-v1.0.onnx"), MODEL_MIN_BYTES) &&
    fileExceeds(path.join(models, "voices-v1.0.bin"), VOICES_MIN_BYTES)
  );
}

function heartbeatIsFresh(filePath: string): boolean {
  try {
    return Date.now() - statSync(filePath).mtimeMs < HEARTBEAT_FRESHNESS_MS;
  } catch {
    return false;
  }
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
  const installed = modelsInstalled(base);
  const engineRunning = heartbeatIsFresh(path.join(base, "drainer.alive"));
  const paused = existsSync(path.join(base, "PAUSE"));
  const engine = readEngineStatus(base);

  let state: RuntimeState;
  if (!installed || engine?.state === "setup_required") {
    state = "setup_required";
  } else if (paused) {
    state = "paused";
  } else if (engineRunning) {
    state = engine?.state ?? "idle";
  } else {
    state = "ready";
  }

  return {
    version: engine?.version ?? 1,
    state,
    updated_at: engine?.updated_at ?? 0,
    engine_pid: engineRunning ? (engine?.engine_pid ?? null) : null,
    engine_running: engineRunning,
    installed,
    current: engineRunning ? (engine?.current ?? null) : null,
    queue_count: engine?.queue_count ?? 0,
    queue: engine?.queue ?? [],
  };
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
  app.on("second-instance", showWindow);
  app.on("before-quit", () => {
    quitting = true;
  });
  app.on("activate", showWindow);
  app.on("window-all-closed", () => {
    // The tray owns the app lifetime
  });
  void app.whenReady().then(() => {
    Menu.setApplicationMenu(null);
    registerIpc();
    createWindow();
    createTray();
  });
}
