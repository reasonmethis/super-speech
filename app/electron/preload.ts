import { contextBridge, ipcRenderer } from "electron";
import { IPC_CHANNELS, type DesktopApi } from "../src/runtime";

const api: DesktopApi = {
  getStatus: () => ipcRenderer.invoke(IPC_CHANNELS.getStatus),
  setPaused: (paused) => ipcRenderer.invoke(IPC_CHANNELS.setPaused, paused),
  openSetup: () => ipcRenderer.invoke(IPC_CHANNELS.openSetup),
  minimize: () => ipcRenderer.invoke(IPC_CHANNELS.minimize),
  hide: () => ipcRenderer.invoke(IPC_CHANNELS.hide),
};

contextBridge.exposeInMainWorld("superSpeech", api);
