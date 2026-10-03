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
//
// A runtime that has not exited some time after being asked to is killed. One
// that knows its stop will be slow says how long it needs, on any event:
//
//   {"event": "progress", "message": "Finishing a database update", "grace_seconds": 960}

const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const readline = require("node:readline");

// A healthy runtime stops in a few seconds, and at worst takes about 45: the
// broker is given 30 to exit before it is killed. Past this it is hung.
const STOP_GRACE_MS = 60_000;
const MAX_GRACE_SECONDS = 30 * 60;

class Runtime extends EventEmitter {
  constructor({
    command,
    args = [],
    env = {},
    cwd,
    logFile,
    registryFile,
    stopGraceMs = STOP_GRACE_MS,
  }) {
    super();
    this.command = command;
    this.args = args;
    this.env = env;
    this.cwd = cwd;
    this.logFile = logFile;
    this.registryFile = registryFile;
    this.stopGraceMs = stopGraceMs;
    this.child = null;
    this.stopping = false;
    this.exited = null;
    this.hasExited = false;
    this.killTimer = null;
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
      const settle = (code, signal) => {
        if (this.hasExited) return;
        this.hasExited = true;
        clearTimeout(this.killTimer);
        // A runtime that stopped its services also removed the list of them,
        // so this only finds work after a crash or a kill.
        killRecorded(this.registryFile);
        log?.end();
        this.emit("exit", { code, signal, expected: this.stopping });
        resolve({ code, signal });
      };
      this.child.once("exit", settle);
      this.child.once("error", (error) => {
        this.emit("event", { event: "error", message: error.message, fatal: true });
        // A process that could not be started never exits either.
        if (this.child.pid === undefined) settle(null, null);
      });
    });

    readline.createInterface({ input: this.child.stdout }).on("line", (line) => {
      log?.write(`${line}\n`);
      const event = parseEvent(line);
      if (!event) return this.emit("log", line);
      if (this.stopping && Number.isFinite(event.grace_seconds)) {
        this.killAfter(Math.min(event.grace_seconds, MAX_GRACE_SECONDS) * 1000);
      }
      this.emit("event", event);
    });
    readline.createInterface({ input: this.child.stderr }).on("line", (line) => {
      log?.write(`${line}\n`);
      this.emit("log", line);
    });
  }

  async stop() {
    if (!this.child || this.hasExited) return;
    this.stopping = true;
    this.child.stdin?.end();
    this.killAfter(this.stopGraceMs);
    await this.exited;
  }

  killAfter(milliseconds) {
    clearTimeout(this.killTimer);
    this.killTimer = setTimeout(() => this.child.kill("SIGKILL"), milliseconds);
  }
}

// A runtime that crashed or had to be killed cannot stop its services. It
// keeps a list of them on disk for exactly this case (Windows also has a Job
// Object that takes them down with it; other systems rely on this).
//
// Outside Windows each service leads its own process group, and the group is
// what gets killed: RabbitMQ's entry is a shell script, and killing only the
// script would leave the broker it started running.
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
      process.kill(process.platform === "win32" ? entry.pid : -entry.pid, "SIGKILL");
    } catch {
      // already gone
    }
  }
  fs.rmSync(registryFile, { force: true });
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
