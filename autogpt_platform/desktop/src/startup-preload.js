"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("autogpt", {
  onRuntimeEvent: (callback) => ipcRenderer.on("runtime-event", (_, event) => callback(event)),
  openLogs: () => ipcRenderer.send("open-logs"),
  quit: () => ipcRenderer.send("quit"),
});
