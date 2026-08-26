import { contextBridge, ipcRenderer } from "electron";
import { IPC_CHANNELS, type DesktopApi } from "../src/runtime";

const api: DesktopApi = {
  getStatus: () => ipcRenderer.invoke(IPC_CHANNELS.getStatus),
  getVersions: () => ipcRenderer.invoke(IPC_CHANNELS.getVersions),
  setPaused: (paused) => ipcRenderer.invoke(IPC_CHANNELS.setPaused, paused),
  playChunk: (id) => ipcRenderer.invoke(IPC_CHANNELS.playChunk, id),
  moveQueueItem: (id, beforeId) =>
    ipcRenderer.invoke(IPC_CHANNELS.moveQueueItem, id, beforeId),
  archiveQueueItem: (id) => ipcRenderer.invoke(IPC_CHANNELS.archiveQueueItem, id),
  copyText: (text) => ipcRenderer.invoke(IPC_CHANNELS.copyText, text),
  clearQueue: () => ipcRenderer.invoke(IPC_CHANNELS.clearQueue),
  openSetup: () => ipcRenderer.invoke(IPC_CHANNELS.openSetup),
  minimize: () => ipcRenderer.invoke(IPC_CHANNELS.minimize),
  hide: () => ipcRenderer.invoke(IPC_CHANNELS.hide),
};

contextBridge.exposeInMainWorld("superSpeech", api);
