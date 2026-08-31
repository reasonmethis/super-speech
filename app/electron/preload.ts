import { contextBridge, ipcRenderer } from "electron";
import { IPC_CHANNELS, type DesktopApi } from "../src/runtime";

const api: DesktopApi = {
  getStatus: () => ipcRenderer.invoke(IPC_CHANNELS.getStatus),
  getVersions: () => ipcRenderer.invoke(IPC_CHANNELS.getVersions),
  setPaused: (paused) => ipcRenderer.invoke(IPC_CHANNELS.setPaused, paused),
  mutateTimeline: (mutation) =>
    ipcRenderer.invoke(IPC_CHANNELS.mutateTimeline, mutation),
  sendInboxMessage: (speechicleId, text) =>
    ipcRenderer.invoke(IPC_CHANNELS.sendInboxMessage, speechicleId, text),
  copyText: (text) => ipcRenderer.invoke(IPC_CHANNELS.copyText, text),
  openSetup: () => ipcRenderer.invoke(IPC_CHANNELS.openSetup),
  minimize: () => ipcRenderer.invoke(IPC_CHANNELS.minimize),
  toggleMaximize: () => ipcRenderer.invoke(IPC_CHANNELS.toggleMaximize),
  onMaximizedChange: (listener) => {
    ipcRenderer.on(IPC_CHANNELS.maximizedChanged, (_event, maximized: boolean) => {
      listener(maximized);
    });
  },
  hide: () => ipcRenderer.invoke(IPC_CHANNELS.hide),
};

contextBridge.exposeInMainWorld("superSpeech", api);
