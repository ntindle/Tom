"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { app, BrowserWindow, Menu, Tray, dialog, ipcMain, nativeImage, shell } = require("electron");

const { defaultDataDir, readRuntimeManifest, runtimeDir } = require("./paths");
const { Runtime } = require("./runtime");

const dataDir = defaultDataDir();
const logsDir = path.join(dataDir, "logs");

let runtime = null;
let startupWindow = null;
let mainWindow = null;
let tray = null;
let appUrl = null;
let quitting = false;
const history = [];

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => focusWindow());
  app.whenReady().then(boot);
}

function boot() {
  fs.mkdirSync(logsDir, { recursive: true });
  Menu.setApplicationMenu(null);
  createTray();
  showStartupWindow();
  startRuntime();
}

function startRuntime() {
  let manifest;
  const dir = runtimeDir({ isPackaged: app.isPackaged, resourcesPath: process.resourcesPath });
  try {
    manifest = readRuntimeManifest(dir);
  } catch (error) {
    report({ event: "error", fatal: true, message: `The runtime is missing: ${error.message}` });
    return;
  }

  runtime = new Runtime({
    command: manifest.command,
    args: manifest.args,
    cwd: dir,
    env: {
      ...manifest.env,
      AUTOGPT_DESKTOP_DATA_DIR: dataDir,
      AUTOGPT_DESKTOP_SHELL_VERSION: app.getVersion(),
    },
    logFile: path.join(logsDir, "runtime.log"),
  });
  runtime.on("event", onRuntimeEvent);
  runtime.on("exit", ({ code, expected }) => {
    if (!expected && !quitting) {
      report({
        event: "error",
        fatal: true,
        message: `AutoGPT stopped unexpectedly (exit code ${code}).`,
      });
    }
  });
  runtime.start();
}

function onRuntimeEvent(event) {
  report(event);
  if (event.event === "ready" && typeof event.url === "string") {
    appUrl = event.url;
    openMainWindow();
  }
}

function report(event) {
  history.push(event);
  startupWindow?.webContents.send("runtime-event", event);
  if (event.event === "error" && event.fatal) focusWindow();
}

function showStartupWindow() {
  startupWindow = new BrowserWindow({
    width: 520,
    height: 360,
    resizable: false,
    title: "AutoGPT",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "startup-preload.js"),
      contextIsolation: true,
      sandbox: true,
    },
  });
  startupWindow.loadFile(path.join(__dirname, "startup.html"));
  startupWindow.once("ready-to-show", () => startupWindow.show());
  startupWindow.webContents.once("did-finish-load", () => {
    for (const event of history) startupWindow?.webContents.send("runtime-event", event);
  });
  startupWindow.on("closed", () => {
    startupWindow = null;
    if (!mainWindow) app.quit();
  });
}

function openMainWindow() {
  if (mainWindow) return focusWindow();
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: "AutoGPT",
    show: false,
    webPreferences: { contextIsolation: true, sandbox: true },
  });
  keepNavigationInApp(mainWindow);
  mainWindow.loadURL(appUrl);
  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
    startupWindow?.destroy();
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
    app.quit();
  });
}

// The app's own origin stays in the window; everything else (docs, OAuth
// consent screens, marketplace links) belongs in the user's browser.
function keepNavigationInApp(window) {
  const isAppUrl = (url) => appUrl && new URL(url).origin === new URL(appUrl).origin;
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (!isAppUrl(url)) shell.openExternal(url);
    return { action: isAppUrl(url) ? "allow" : "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (!isAppUrl(url)) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });
}

function focusWindow() {
  const window = mainWindow || startupWindow;
  if (!window) return;
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

function createTray() {
  const icon = nativeImage.createFromPath(path.join(__dirname, "icon.png"));
  tray = new Tray(icon.isEmpty() ? nativeImage.createEmpty() : icon.resize({ width: 16 }));
  tray.setToolTip("AutoGPT");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Open AutoGPT", click: focusWindow },
      { label: "Open in browser", click: () => appUrl && shell.openExternal(appUrl) },
      { label: "Show logs", click: () => shell.openPath(logsDir) },
      { type: "separator" },
      { label: "Quit AutoGPT", click: () => app.quit() },
    ]),
  );
  tray.on("click", focusWindow);
}

ipcMain.on("open-logs", () => shell.openPath(logsDir));
ipcMain.on("quit", () => app.quit());

app.on("window-all-closed", () => {});

app.on("before-quit", (event) => {
  if (quitting || !runtime) return;
  event.preventDefault();
  quitting = true;
  startupWindow?.webContents.send("runtime-event", { event: "progress", message: "Stopping AutoGPT…" });
  runtime
    .stop()
    .catch((error) => dialog.showErrorBox("AutoGPT", `Could not stop cleanly: ${error.message}`))
    .finally(() => app.exit(0));
});
