"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const APP_DIR_NAME = "AutoGPT";

// Databases and agent workspaces grow large, so they live in the per-machine
// location of each OS rather than a roaming profile.
function defaultDataDir(platform = process.platform, env = process.env) {
  if (env.AUTOGPT_DESKTOP_DATA_DIR) return env.AUTOGPT_DESKTOP_DATA_DIR;
  const home = os.homedir();
  if (platform === "win32") {
    return path.join(env.LOCALAPPDATA || path.join(home, "AppData", "Local"), APP_DIR_NAME);
  }
  if (platform === "darwin") {
    return path.join(home, "Library", "Application Support", APP_DIR_NAME);
  }
  return path.join(env.XDG_DATA_HOME || path.join(home, ".local", "share"), APP_DIR_NAME);
}

// A packaged app carries its runtime under resources/runtime. During
// development, point AUTOGPT_DESKTOP_RUNTIME at a staged runtime directory.
function runtimeDir({ isPackaged, resourcesPath, env = process.env }) {
  if (env.AUTOGPT_DESKTOP_RUNTIME) return env.AUTOGPT_DESKTOP_RUNTIME;
  if (isPackaged) return path.join(resourcesPath, "runtime");
  return path.join(__dirname, "..", "build", "runtime");
}

// runtime/manifest.json names the process the shell starts. Paths in it are
// relative to the runtime directory, so the manifest is identical wherever
// the app is installed.
function readRuntimeManifest(dir, platform = process.platform) {
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, "manifest.json"), "utf8"));
  const entry = manifest[platform] || manifest.default;
  if (!entry || typeof entry.command !== "string") {
    throw new Error(`runtime manifest has no command for ${platform}`);
  }
  const resolve = (value) =>
    typeof value === "string" ? value.replaceAll("{runtime}", dir) : value;
  return {
    command: path.isAbsolute(entry.command) ? entry.command : path.join(dir, entry.command),
    args: (entry.args || []).map(resolve),
    env: Object.fromEntries(
      Object.entries(entry.env || {}).map(([key, value]) => [key, resolve(value)]),
    ),
  };
}

module.exports = { defaultDataDir, runtimeDir, readRuntimeManifest };
