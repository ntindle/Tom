"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const { spawn } = require("node:child_process");
const { Runtime, parseEvent, killRecorded } = require("../src/runtime");
const { defaultDataDir, readRuntimeManifest } = require("../src/paths");

const FAKE = path.join(__dirname, "fake-runtime.js");

function collect(runtime) {
  const events = [];
  const logs = [];
  runtime.on("event", (event) => events.push(event));
  runtime.on("log", (line) => logs.push(line));
  return { events, logs };
}

test("parseEvent accepts only JSON objects with an event name", () => {
  assert.deepEqual(parseEvent('{"event":"ready","url":"x"}'), { event: "ready", url: "x" });
  assert.equal(parseEvent("plain text"), null);
  assert.equal(parseEvent('{"no":"event"}'), null);
  assert.equal(parseEvent("{not json"), null);
});

test("runtime reports progress and ready, and stops when stdin closes", async () => {
  const runtime = new Runtime({ command: process.execPath, args: [FAKE] });
  const { events, logs } = collect(runtime);
  const ready = new Promise((resolve) =>
    runtime.on("event", (event) => event.event === "ready" && resolve()),
  );
  const exit = new Promise((resolve) => runtime.once("exit", resolve));
  runtime.start();
  await ready;
  await runtime.stop();
  const { code, expected } = await exit;

  assert.equal(code, 0);
  assert.equal(expected, true);
  assert.deepEqual(
    events.map((event) => event.event),
    ["progress", "ready", "progress"],
  );
  assert.deepEqual(logs, ["a plain log line"]);
});

test("an unexpected exit is reported as not expected", async () => {
  const runtime = new Runtime({ command: process.execPath, args: [FAKE, "crash"] });
  const { events } = collect(runtime);
  const exit = new Promise((resolve) => runtime.once("exit", resolve));
  runtime.start();
  const { code, expected } = await exit;

  assert.equal(code, 3);
  assert.equal(expected, false);
  assert.ok(events.some((event) => event.event === "error" && event.fatal));
});

test("a runtime that ignores stdin is killed after the grace period", { timeout: 40_000 }, async () => {
  const runtime = new Runtime({ command: process.execPath, args: [FAKE, "ignore-stdin"] });
  const ready = new Promise((resolve) =>
    runtime.on("event", (event) => event.event === "ready" && resolve()),
  );
  runtime.start();
  await ready;
  const started = Date.now();
  await runtime.stop();
  assert.ok(Date.now() - started >= 19_000);
});

test("runtime output is appended to the log file", async () => {
  const logFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "agpt-desktop-")), "runtime.log");
  const runtime = new Runtime({ command: process.execPath, args: [FAKE, "crash"], logFile });
  const exit = new Promise((resolve) => runtime.once("exit", resolve));
  runtime.start();
  await exit;
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.match(fs.readFileSync(logFile, "utf8"), /a plain log line/);
});

test("data directory follows each platform's per-machine location", () => {
  assert.equal(
    defaultDataDir("win32", { LOCALAPPDATA: "C:\\Users\\u\\AppData\\Local" }),
    path.join("C:\\Users\\u\\AppData\\Local", "AutoGPT"),
  );
  assert.equal(
    defaultDataDir("linux", { XDG_DATA_HOME: "/x/share" }),
    path.join("/x/share", "AutoGPT"),
  );
  assert.match(defaultDataDir("darwin", {}), /Library[\\/]Application Support[\\/]AutoGPT$/);
  assert.equal(defaultDataDir("linux", { AUTOGPT_DESKTOP_DATA_DIR: "/custom" }), "/custom");
});

test("manifest commands resolve relative to the runtime directory", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "agpt-runtime-"));
  fs.writeFileSync(
    path.join(dir, "manifest.json"),
    JSON.stringify({
      win32: { command: "python/python.exe", args: ["-m", "autogpt_desktop", "{runtime}/x"] },
      default: { command: "python/bin/python3", env: { A: "{runtime}/a" } },
    }),
  );
  const win = readRuntimeManifest(dir, "win32");
  assert.equal(win.command, path.join(dir, "python/python.exe"));
  assert.deepEqual(win.args, ["-m", "autogpt_desktop", `${dir}/x`]);
  const mac = readRuntimeManifest(dir, "darwin");
  assert.equal(mac.command, path.join(dir, "python/bin/python3"));
  assert.deepEqual(mac.env, { A: `${dir}/a` });
});

test("services recorded by a runtime that had to be killed are killed too", async () => {
  const service = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" });
  const exited = new Promise((resolve) => service.once("exit", (code, signal) => resolve(signal || code)));
  const registry = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "agpt-registry-")), "children.json");
  fs.writeFileSync(registry, JSON.stringify([{ name: "service", pid: service.pid }, { name: "gone", pid: 999999 }]));

  killRecorded(registry);

  assert.ok(await exited);
  assert.doesNotThrow(() => killRecorded(path.join(os.tmpdir(), "no-such-registry.json")));
});
