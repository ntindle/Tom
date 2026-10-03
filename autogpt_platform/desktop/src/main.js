"use strict";

const fs = require("node:fs");
const path = require("node:path");
const {
  app,
  BrowserWindow,
  Menu,
  Notification,
  Tray,
  clipboard,
  dialog,
  ipcMain,
  nativeImage,
  session,
  shell,
} = require("electron");

const { allowsPermission, classifyMainNavigation, classifyWindowOpen } = require("./navigation");
const { hearsRuntime } = require("./lifecycle");
const { applicationMenuTemplate, ownerMenuItems } = require("./owner");
const { defaultDataDir, readRuntimeManifest, runtimeDir } = require("./paths");
const { openResetPasswordWindow } = require("./reset-password-window");
const { Runtime } = require("./runtime");
const {
  chooseAutoUpdater,
  createUpdater,
  dropsUpdaterQuit,
  failedStartOffer,
  fileLogger,
  installsFromDownloadedFile,
  releaseUrl,
  updateMenuItems,
  updateMode,
  withUpdatesMenu,
} = require("./updater");

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
let restarting = false;
let failure = null;
let updater = null;
let updaterStarted = false;
let confirmingUpdate = false;
let installingUpdate = false;
let quitIsForUpdate = false;
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
  restrictPermissions(session.defaultSession);
  createTray();
  refreshMenus();
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
    lookForAFixedVersion();
    return;
  }

  const started = new Runtime({
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
  runtime = started;
  // See lifecycle.js: a runtime that is being replaced is not listened to,
  // and neither is one that is being stopped to install an update.
  const heard = (event) =>
    hearsRuntime({ current: runtime, sender: started, restarting: restarting || installingUpdate, event });
  started.on("event", (event) => heard(event) && onRuntimeEvent(event));
  started.on("exit", ({ code, expected }) => {
    if (expected || quitting || runtime !== started) return;
    // A runtime that failed has usually said why already; keep its words.
    const message = failure || `AutoGPT stopped unexpectedly (exit code ${code}).`;
    if (!failure) report({ event: "error", fatal: true, message });
    if (mainWindow) explainCrash(message);
    else lookForAFixedVersion();
  });
  started.start();
}

function onRuntimeEvent(event) {
  report(event);
  if (event.event === "ready" && typeof event.url === "string") {
    appUrl = event.url;
    openMainWindow();
    // Not before the first `ready`: migrations are over by then.
    theUpdater()?.arm();
    refreshMenus();
  }
}

// AutoGPT could not start and its runtime has exited. If that is this
// version's fault, the fix is a newer version, and the user must not have
// to go and find it: look once, now, and offer it next to the error.
function lookForAFixedVersion() {
  theUpdater()?.check();
  refreshMenus();
}

// See updater.js for when the app looks for updates and what it accepts.
// Null when this build does not update itself, and when electron-updater
// cannot be started: updating is optional, the app is not.
function theUpdater() {
  if (updaterStarted) return updater;
  updaterStarted = true;
  try {
    updater = startUpdater();
  } catch (error) {
    fileLogger(path.join(logsDir, "updater.log")).error(`Updates are off: ${error.stack || error}`);
  }
  return updater;
}

function startUpdater() {
  const mode = updateMode({
    version: app.getVersion(),
    isPackaged: app.isPackaged,
    platform: process.platform,
    env: process.env,
    macDeveloperId: Boolean(require("../package.json").autogptDesktop?.macDeveloperId),
    appPath: app.getAppPath(),
  });
  if (mode === "off") return null;
  // electron-updater says so on Electron's own updater, immediately before
  // it quits the app.
  require("electron").autoUpdater.on("before-quit-for-update", () => {
    quitIsForUpdate = true;
  });
  return createUpdater({
    autoUpdater: chooseAutoUpdater(require("electron-updater"), { platform: process.platform, env: process.env }),
    mode,
    logger: fileLogger(path.join(logsDir, "updater.log")),
    onChange: onUpdateState,
    needsDownloadedFile: installsFromDownloadedFile(process.platform),
  });
}

function onUpdateState({ phase, version }) {
  refreshMenus();
  sendUpdateOffer();
  if (phase === "failed" && installingUpdate) {
    return resumeAfterFailedUpdate("The update could not be installed. Starting AutoGPT again…");
  }
  if (phase !== "ready" && phase !== "available") return;
  if (!Notification.isSupported()) return;
  const body =
    phase === "ready"
      ? "Choose Restart to update in the AutoGPT menu when it suits you."
      : "Open the AutoGPT menu to download it.";
  new Notification({ title: `AutoGPT ${version} is available`, body }).show();
}

// Stops the runtime as a quit does, which waits out a database migration,
// and only then lets the installer replace the app.
async function restartToUpdate() {
  if (quitting || restarting || confirmingUpdate || updater?.state().phase !== "ready") return;
  confirmingUpdate = true;
  try {
    if (!(await userWantsTheUpdate())) return;
  } finally {
    confirmingUpdate = false;
  }
  quitting = true;
  installingUpdate = true;
  appUrl = null;
  history.length = 0;
  // On macOS the new version is only unpacked now, which takes a while.
  history.push({ event: "progress", message: "Installing the update…" });
  showProgressInsteadOfTheApp();
  await runtime?.stop().catch(() => {});
  // The AppImage starts the new version before this one has gone, and a
  // second instance quits at once while the first holds the lock.
  if (process.platform === "linux") app.releaseSingleInstanceLock();
  if (!updater.install()) resumeAfterFailedUpdate("The update has to be downloaded again. Starting AutoGPT again…");
  refreshMenus();
}

// Asks, but only for a version that is still the latest release and whose
// download is still there (updater.js looks once more). The answer counts
// only if nothing changed while the question was on screen.
async function userWantsTheUpdate() {
  if (!(await updater.stillReady())) return false;
  const { version } = updater.state();
  const question = {
    type: "question",
    message: `Restart AutoGPT to update to ${version}?`,
    detail: "Agents that are running now will be stopped.",
    buttons: ["Restart", "Later"],
    defaultId: 0,
    cancelId: 1,
  };
  const parent = mainWindow || startupWindow;
  const { response } = await (parent ? dialog.showMessageBox(parent, question) : dialog.showMessageBox(question));
  const { phase, version: current } = updater.state();
  return response === 0 && !quitting && !restarting && phase === "ready" && current === version;
}

// The installer was not started, or could not be: the old version is
// intact, so run it. electron-updater may already have asked the app to
// quit; `before-quit` drops that request (dropsUpdaterQuit).
function resumeAfterFailedUpdate(message) {
  installingUpdate = false;
  quitting = false;
  if (process.platform === "linux") app.requestSingleInstanceLock();
  failure = null;
  history.length = 0;
  history.push({ event: "progress", message });
  showProgressInsteadOfTheApp();
  startRuntime();
}

// What the startup window shows next to a failed start (updater.js).
function sendUpdateOffer() {
  startupWindow?.webContents.send("update-offer", failedStartOffer(updater?.state()));
}

function takeUpdateOffer() {
  const offer = failedStartOffer(updater?.state());
  if (offer?.action === "install") restartToUpdate();
  if (offer?.action === "open") shell.openExternal(releaseUrl(updater.state().version));
}

function updateItems() {
  return updateMenuItems({
    state: updater?.state(),
    check: () => updater.check(),
    install: restartToUpdate,
    openRelease: (version) => shell.openExternal(releaseUrl(version)),
  });
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
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "startup-preload.js"),
      contextIsolation: true,
      sandbox: true,
    },
  });
  startupWindow.loadFile(path.join(__dirname, "startup.html"));
  startupWindow.once("ready-to-show", () => startupWindow.show());
  startupWindow.webContents.on("did-finish-load", () => {
    for (const event of history) startupWindow?.webContents.send("runtime-event", event);
    sendUpdateOffer();
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
    autoHideMenuBar: true,
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

// Stop the runtime and start it again, showing progress as a first start
// does. Used after a change that the runtime only picks up while starting.
async function restartRuntime() {
  if (restarting || quitting) return;
  restarting = true;
  appUrl = null;
  failure = null;
  history.length = 0;
  history.push({ event: "progress", message: "Restarting AutoGPT…" });
  showProgressInsteadOfTheApp();
  refreshMenus();
  try {
    await runtime?.stop();
  } finally {
    restarting = false;
  }
  if (!quitting) startRuntime();
}

function showProgressInsteadOfTheApp() {
  // A reload clears a failure the startup window may be showing; loading
  // replays `history` either way.
  if (startupWindow) startupWindow.webContents.reload();
  else showStartupWindow();
  if (!mainWindow) return;
  mainWindow.removeAllListeners("closed"); // closing it here is not a quit
  mainWindow.destroy();
  mainWindow = null;
}

function resetOwnerPassword() {
  openResetPasswordWindow({ icon: ICON, dataDir, onWritten: restartRuntime });
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
  function route(event, url) {
    const destination = classifyMainNavigation(url, appUrl);
    if (destination === "app") return;
    event.preventDefault();
    if (destination === "browser") shell.openExternal(url);
  }
  window.webContents.on("will-navigate", route);
  // One of the app's own links can answer with a redirect to another site.
  window.webContents.on("will-redirect", route);
}

// See navigation.js for what is allowed and why.
function restrictPermissions(target) {
  target.setPermissionRequestHandler((contents, permission, callback, details) => {
    const origin = details.requestingUrl || contents.getURL();
    callback(allowsPermission({ permission, origin, mediaTypes: details.mediaTypes }, appUrl));
  });
  target.setPermissionCheckHandler((_contents, permission, origin, details) =>
    allowsPermission({ permission, origin, mediaTypes: [details.mediaType] }, appUrl),
  );
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

// The tray icon can be hidden or missing, so its owner actions are in the
// application menu as well (owner.js; outside macOS it shows on Alt).
function applicationMenu() {
  const template = applicationMenuTemplate(process.platform, ownerItems());
  return Menu.buildFromTemplate(withUpdatesMenu(template, updateItems(), process.platform));
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
  tray.on("click", focusWindow);
}

function trayMenu() {
  return Menu.buildFromTemplate([
    { label: "Open AutoGPT", click: focusWindow },
    { label: "Open in browser", click: () => appUrl && shell.openExternal(appUrl) },
    { label: "Settings file (API keys)", click: () => shell.openPath(settingsFile) },
    { label: "Show logs", click: () => shell.openPath(logsDir) },
    { type: "separator" },
    ...ownerItems(),
    { type: "separator" },
    ...updateItems(),
    { label: "Quit AutoGPT", click: () => app.quit() },
  ]);
}

function ownerItems() {
  return ownerMenuItems({
    appUrl,
    copyText: (text) => clipboard.writeText(text),
    resetPassword: resetOwnerPassword,
  });
}

// The menus show the app's address, which is only known once it is ready.
function refreshMenus() {
  Menu.setApplicationMenu(applicationMenu());
  tray?.setContextMenu(trayMenu());
}

ipcMain.on("open-logs", () => shell.openPath(logsDir));
ipcMain.on("quit", () => app.quit());
ipcMain.on("take-update-offer", takeUpdateOffer);

app.on("window-all-closed", () => {});

app.on("before-quit", (event) => {
  const dropped = dropsUpdaterQuit({ quitIsForUpdate, installing: installingUpdate });
  quitIsForUpdate = false;
  if (dropped) return event.preventDefault();
  if (quitting || !runtime) return;
  event.preventDefault();
  quitting = true;
  startupWindow?.webContents.send("runtime-event", { event: "progress", message: "Stopping AutoGPT…" });
  runtime
    .stop()
    .catch((error) => dialog.showErrorBox("AutoGPT", `Could not stop cleanly: ${error.message}`))
    .finally(() => app.exit(0));
});
