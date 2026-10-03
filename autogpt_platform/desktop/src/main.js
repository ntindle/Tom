"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { app, BrowserWindow, Menu, Tray, dialog, ipcMain, nativeImage, shell } = require("electron");

const { classifyMainNavigation, classifyWindowOpen } = require("./navigation");
const { defaultDataDir, readRuntimeManifest, runtimeDir } = require("./paths");
const { Runtime } = require("./runtime");

const ICON = path.join(__dirname, "icon.png");
const dataDir = defaultDataDir();
const logsDir = path.join(dataDir, "logs");
const settingsFile = path.join(dataDir, "config", "settings.env");

let runtime = null;
let startupWindow = null;
let mainWindow = null;
let tray = null;
let appUrl = null;
let quitting = false;
let failure = null;
const history = [];

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => focusWindow());
  app.on("web-contents-created", (_, contents) => routeNewWindows(contents));
  app.whenReady().then(boot);
}

function boot() {
  fs.mkdirSync(logsDir, { recursive: true });
  Menu.setApplicationMenu(applicationMenu());
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
    registryFile: path.join(dataDir, "run", "children.json"),
  });
  runtime.on("event", onRuntimeEvent);
  runtime.on("exit", ({ code, expected }) => {
    if (expected || quitting) return;
    // A runtime that failed has usually said why already; keep its words.
    const message = failure || `AutoGPT stopped unexpectedly (exit code ${code}).`;
    if (!failure) report({ event: "error", fatal: true, message });
    if (mainWindow) explainCrash(message);
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
  if (event.event === "error" && event.fatal) failure = event.message;
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
    icon: ICON,
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
    icon: ICON,
    show: false,
    webPreferences: { contextIsolation: true, sandbox: true },
  });
  keepMainWindowInApp(mainWindow);
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

// See navigation.js for what stays in the app and why.
function routeNewWindows(contents) {
  contents.setWindowOpenHandler((details) => {
    const destination = classifyWindowOpen(details, appUrl);
    if (destination === "browser") shell.openExternal(details.url);
    if (destination !== "app") return { action: "deny" };
    return {
      action: "allow",
      overrideBrowserWindowOptions: {
        icon: ICON,
        autoHideMenuBar: true,
        webPreferences: { contextIsolation: true, sandbox: true },
      },
    };
  });
}

function keepMainWindowInApp(window) {
  window.webContents.on("will-navigate", (event, url) => {
    const destination = classifyMainNavigation(url, appUrl);
    if (destination === "app") return;
    event.preventDefault();
    if (destination === "browser") shell.openExternal(url);
  });
}

// The backend is gone, so the page in the window can only fail from here on.
async function explainCrash(message) {
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: "error",
    message,
    detail: "The logs usually say why. AutoGPT will close.",
    buttons: ["Show logs", "Close"],
    defaultId: 1,
  });
  if (response === 0) shell.openPath(logsDir);
  app.quit();
}

// macOS routes Cmd+C/V/A/Q through the application menu, so removing the
// menu there removes the shortcuts. Elsewhere the menu bar is just clutter.
function applicationMenu() {
  if (process.platform !== "darwin") return null;
  return Menu.buildFromTemplate([
    { role: "appMenu" },
    { role: "editMenu" },
    { role: "viewMenu" },
    { role: "windowMenu" },
  ]);
}

function focusWindow() {
  const window = mainWindow || startupWindow;
  if (!window) return;
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

function createTray() {
  tray = new Tray(nativeImage.createFromPath(ICON).resize({ width: 16, height: 16 }));
  tray.setToolTip("AutoGPT");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Open AutoGPT", click: focusWindow },
      { label: "Open in browser", click: () => appUrl && shell.openExternal(appUrl) },
      { label: "Settings file (API keys)", click: () => shell.openPath(settingsFile) },
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
