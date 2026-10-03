"use strict";

// The shell knows nothing about Postgres, Python or ports. It starts one
// runtime process and listens to it. The runtime reports its state as one JSON
// object per stdout line:
//
//   {"event": "progress", "step": "postgres", "message": "Starting the database"}
//   {"event": "ready", "url": "http://127.0.0.1:43117"}
//   {"event": "error", "message": "...", "fatal": true}
//
// Anything on stdout that is not a JSON object is treated as a log line.
// The shell asks the runtime to stop by closing its stdin. Signals do not
// reach a console-less child on Windows, and a closed pipe also covers the
// shell crashing: the runtime sees EOF either way and shuts its services down.

const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const readline = require("node:readline");

const STOP_GRACE_MS = 20_000;

class Runtime extends EventEmitter {
  constructor({ command, args = [], env = {}, cwd, logFile, registryFile }) {
    super();
    this.command = command;
    this.args = args;
    this.env = env;
    this.cwd = cwd;
    this.logFile = logFile;
    this.registryFile = registryFile;
    this.child = null;
    this.stopping = false;
    this.exited = null;
  }

  start() {
    const log = this.logFile
      ? fs.createWriteStream(this.logFile, { flags: "a" })
      : null;
    this.log = log;
    this.child = spawn(this.command, this.args, {
      cwd: this.cwd,
      env: { ...process.env, ...this.env },
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    this.exited = new Promise((resolve) => {
      this.child.once("exit", (code, signal) => {
        log?.end();
        this.emit("exit", { code, signal, expected: this.stopping });
        resolve({ code, signal });
      });
    });
    this.child.once("error", (error) => {
      this.emit("event", { event: "error", message: error.message, fatal: true });
    });

    readline.createInterface({ input: this.child.stdout }).on("line", (line) => {
      log?.write(`${line}\n`);
      const event = parseEvent(line);
      if (event) this.emit("event", event);
      else this.emit("log", line);
    });
    readline.createInterface({ input: this.child.stderr }).on("line", (line) => {
      log?.write(`${line}\n`);
      this.emit("log", line);
    });
  }

  async stop() {
    if (!this.child || this.child.exitCode !== null) return;
    this.stopping = true;
    this.child.stdin.end();
    const timer = setTimeout(() => {
      this.child.kill("SIGKILL");
      killRecorded(this.registryFile);
    }, STOP_GRACE_MS);
    await this.exited;
    clearTimeout(timer);
  }
}

// A runtime that had to be killed cannot stop its services. It keeps a list
// of them on disk for exactly this case (Windows also has a Job Object that
// takes them down with it; other systems rely on this).
function killRecorded(registryFile) {
  if (!registryFile) return;
  let entries;
  try {
    entries = JSON.parse(fs.readFileSync(registryFile, "utf8"));
  } catch {
    return;
  }
  for (const entry of Array.isArray(entries) ? entries : []) {
    try {
      process.kill(entry.pid, "SIGKILL");
    } catch {
      // already gone
    }
  }
}

function parseEvent(line) {
  if (!line.startsWith("{")) return null;
  try {
    const value = JSON.parse(line);
    return value && typeof value.event === "string" ? value : null;
  } catch {
    return null;
  }
}

module.exports = { Runtime, parseEvent, killRecorded };
