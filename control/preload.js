// The page's only door to the main process: a few named calls, no Node, no shell.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pugrec", {
  init: () => ipcRenderer.invoke("init"),
  devices: () => ipcRenderer.invoke("devices"),
  sessions: () => ipcRenderer.invoke("sessions"),
  settings: (patch) => ipcRenderer.invoke("settings", patch),
  record: () => ipcRenderer.send("record"),
  task: (id) => ipcRenderer.send("task", id),
  preview: () => ipcRenderer.send("preview"),
  open: (what) => ipcRenderer.send("open", what),
  on: (channel, fn) => {
    if (!["rec", "log", "task", "sessions", "devices"].includes(channel)) return;
    ipcRenderer.on(channel, (_e, data) => fn(data));
  },
});
