"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const { signMacApp } = require("../build/mac_sign");

const DESKTOP = path.join(__dirname, "..");
const CONFIG = path.join(DESKTOP, "electron-builder.config.js");
const SETTINGS = [
  "AUTOGPT_DESKTOP_VERSION",
  "AUTOGPT_DESKTOP_OUTPUT",
  "AUTOGPT_DESKTOP_VARIANT",
  "AUTOGPT_DESKTOP_MAC_SIGN",
  "AUTOGPT_DESKTOP_WIN_SIGN",
  "AZURE_SIGN_ENDPOINT",
  "AZURE_SIGN_ACCOUNT",
  "AZURE_SIGN_PROFILE",
  "AZURE_SIGN_PUBLISHER",
];

// The configuration as electron-builder would load it in this environment.
function configured(settings = {}) {
  const saved = Object.fromEntries(SETTINGS.map((name) => [name, process.env[name]]));
  for (const name of SETTINGS) delete process.env[name];
  Object.assign(process.env, settings);
  delete require.cache[require.resolve(CONFIG)];
  try {
    return require(CONFIG);
  } finally {
    delete require.cache[require.resolve(CONFIG)];
    for (const [name, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
}

function onPlatform(t, platform) {
  const original = Object.getOwnPropertyDescriptor(process, "platform");
  Object.defineProperty(process, "platform", { value: platform });
  t.after(() => Object.defineProperty(process, "platform", original));
}

test("with nothing set, macOS is ad-hoc signed and Windows unsigned, as before there was a certificate", () => {
  const config = configured();
  assert.deepEqual(config.mac, {
    category: "public.app-category.productivity",
    artifactName: "AutoGPT-${version}-${arch}.${ext}",
    gatekeeperAssess: false,
    target: [{ target: "dmg", arch: ["arm64"] }],
    identity: "-",
    hardenedRuntime: false,
    signIgnore: ["/Contents/Resources/runtime/site\\/claude_agent_sdk\\/_bundled\\/claude$"],
  });
  // electron-builder tests each of these against a file's full path.
  const [ignored] = config.mac.signIgnore.map((pattern) => new RegExp(pattern));
  const runtime = "/Users/me/dist/mac-arm64/AutoGPT.app/Contents/Resources/runtime";
  assert.ok(ignored.test(`${runtime}/site/claude_agent_sdk/_bundled/claude`));
  assert.ok(!ignored.test(`${runtime}/site/claude_agent_sdk/_bundled/claude.md`));
  assert.ok(!ignored.test(`${runtime}/python/bin/python3.13`));
  assert.deepEqual(config.win, { target: [{ target: "nsis", arch: ["x64"] }] });
  assert.equal(config.forceCodeSigning, false);
  assert.equal(config.directories.output, "dist");
  assert.deepEqual(config.extraMetadata, { autogptDesktop: { macDeveloperId: false } });
});

test("package.json no longer carries a second configuration", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(DESKTOP, "package.json"), "utf8"));
  assert.equal(manifest.build, undefined);
  for (const script of [manifest.scripts.dist, manifest.scripts["dist:dir"]]) {
    assert.match(script, /--config electron-builder\.config\.js/);
    assert.match(script, /--publish never/);
  }
});

test("the version comes from the environment and must be a version", () => {
  assert.equal(configured({ AUTOGPT_DESKTOP_VERSION: "1.4.0" }).extraMetadata.version, "1.4.0");
  assert.equal(configured({ AUTOGPT_DESKTOP_VERSION: "1.4.0-rc.1" }).extraMetadata.version, "1.4.0-rc.1");
  assert.equal(configured({ AUTOGPT_DESKTOP_VERSION: "0.0.0-dev.412" }).extraMetadata.version, "0.0.0-dev.412");
  for (const good of ["0.0.1-dev.0", "10.20.30", "1.4.0-rc.10", "1.4.0-0a", "1.4.0-alpha-1.x"]) {
    assert.equal(configured({ AUTOGPT_DESKTOP_VERSION: good }).extraMetadata.version, good);
  }
  // Leading zeros are not semantic versioning: electron-updater refuses to
  // start in an app with such a version, and to read one from latest.yml.
  const leadingZeros = ["01.4.0", "1.04.0", "1.4.00", "1.4.0-rc.01", "1.4.0-01"];
  for (const bad of ["v1.4.0", "1.4", "desktop-v1.4.0", "1.4.0 ", "1.4.0+build", "1.4.0-", "1.4.0-rc..1", ...leadingZeros]) {
    assert.throws(() => configured({ AUTOGPT_DESKTOP_VERSION: bad }), /AUTOGPT_DESKTOP_VERSION/, bad);
  }
});

test("a Developer ID build is hardened, notarized, and signed by build/mac_sign.js", (t) => {
  onPlatform(t, "darwin");
  const config = configured({ AUTOGPT_DESKTOP_MAC_SIGN: "developer-id" });
  assert.equal(config.mac.identity, undefined);
  assert.equal(config.mac.hardenedRuntime, true);
  assert.equal(config.mac.notarize, true);
  assert.equal(config.mac.sign, signMacApp);
  assert.ok(fs.existsSync(config.mac.entitlements));
  assert.ok(fs.existsSync(config.mac.entitlementsInherit));
  // Squirrel.Mac updates from the zip.
  assert.deepEqual(config.mac.target.map((target) => target.target), ["dmg", "zip"]);
  assert.equal(config.extraMetadata.autogptDesktop.macDeveloperId, true);
  assert.equal(config.forceCodeSigning, true);
});

test("only a Developer ID build on a Mac looks at the packed app", (t) => {
  assert.equal(configured().artifactBuildStarted, undefined);
  onPlatform(t, "linux");
  assert.equal(configured({ AUTOGPT_DESKTOP_MAC_SIGN: "developer-id" }).artifactBuildStarted, undefined);
});

test("a Developer ID build looks at the packed app before making an installer from it", async (t) => {
  // electron-builder ignores forceCodeSigning on macOS when `sign` is a function.
  onPlatform(t, "darwin");
  const hook = configured({ AUTOGPT_DESKTOP_MAC_SIGN: "developer-id" }).artifactBuildStarted;
  assert.equal(typeof hook, "function");
  // No codesign here (or no app there): either way the build is refused, and
  // the disk image and the zip get the same answer from one look. An output
  // directory of its own: dist/ may hold an app someone built.
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "no-app-"));
  t.after(() => fs.rmSync(output, { recursive: true, force: true }));
  const dmg = hook({ file: path.join(output, "AutoGPT-1.4.0-arm64.dmg") });
  const zip = hook({ file: path.join(output, "AutoGPT-1.4.0-arm64.zip") });
  assert.equal(dmg, zip);
  await assert.rejects(dmg, /A Developer ID build was asked for, but AutoGPT\.app is not signed/);
});

test("the configuration loads where nothing has been installed", () => {
  // The unit tests run before `npm install` on the build machines.
  const { execFileSync } = require("node:child_process");
  const bare = `
    const Module = require("node:module");
    const resolve = Module._resolveFilename;
    Module._resolveFilename = function (request, ...rest) {
      if (!request.startsWith(".") && !request.startsWith("node:") && !require("node:path").isAbsolute(request) && !Module.builtinModules.includes(request)) {
        throw Object.assign(new Error("Cannot find module '" + request + "'"), { code: "MODULE_NOT_FOUND" });
      }
      return resolve.call(this, request, ...rest);
    };
    const config = require(process.argv[1]);
    require("node:fs").writeSync(1, config.appId);
  `;
  const appId = execFileSync(process.execPath, ["-e", bare, path.join(DESKTOP, "electron-builder.config.js")], {
    cwd: DESKTOP,
    encoding: "utf8",
    env: { ...process.env, AUTOGPT_DESKTOP_VARIANT: "", AUTOGPT_DESKTOP_MAC_SIGN: "", AUTOGPT_DESKTOP_WIN_SIGN: "" },
  });
  assert.equal(appId, "co.agpt.autogpt.desktop");
});

// What @electron/osx-sign does to every file of the app, in a process that
// may have fewer files open than the directory holds.
const OPEN_EVERY_FILE = `
  const [, configFile, dir] = process.argv;
  if (configFile) require(configFile);
  const { isBinaryFile } = require("isbinaryfile");
  const fs = require("node:fs"), path = require("node:path");
  const files = fs.readdirSync(dir).map((name) => path.join(dir, name));
  // Written straight to the descriptor: with none to spare, process.stdout
  // cannot even be set up.
  Promise.all(files.map((file) => isBinaryFile(file))).then(
    () => fs.writeSync(1, "opened them all"),
    (error) => { fs.writeSync(1, String(error.code)); process.exit(0); },
  );
`;

function openEveryFile(dir, configFile) {
  const { execFileSync } = require("node:child_process");
  const command = 'ulimit -n 256 && exec "$0" -e "$1" "$2" "$3"';
  return execFileSync("sh", ["-c", command, process.execPath, OPEN_EVERY_FILE, configFile, dir], {
    cwd: DESKTOP,
    encoding: "utf8",
  }).trim();
}

test(
  "signing a bundle of more files than may be open at once waits instead of failing",
  { skip: process.platform === "win32" && "Windows has no such limit, and nothing is signed this way there" },
  (t) => {
    try {
      require.resolve("isbinaryfile");
    } catch {
      return t.skip("npm install has not been run");
    }
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "open-files-"));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    for (let index = 0; index < 1000; index += 1) fs.writeFileSync(path.join(dir, `f${index}`), "text");
    assert.equal(
      openEveryFile(dir, ""),
      "EMFILE",
      "the library that signs no longer fails on its own when it opens more files than are allowed: " +
        "the graceful-fs line at the top of electron-builder.config.js, and this test, can go",
    );
    assert.equal(openEveryFile(dir, path.join(DESKTOP, "electron-builder.config.js")), "opened them all");
  },
);

test("any other value for the macOS setting is the unsigned build", () => {
  for (const value of ["true", "1", "Developer-ID", ""]) {
    const config = configured({ AUTOGPT_DESKTOP_MAC_SIGN: value });
    assert.equal(config.mac.identity, "-");
    assert.equal(config.extraMetadata.autogptDesktop.macDeveloperId, false);
  }
});

test("a signed Windows build leaves the Claude Code CLI as Anthropic shipped it", (t) => {
  onPlatform(t, "win32");
  const pfx = configured({ AUTOGPT_DESKTOP_WIN_SIGN: "pfx" });
  assert.deepEqual(pfx.win.signExts, ["!claude.exe"]);
  assert.equal(pfx.win.azureSignOptions, undefined);
  assert.equal(pfx.forceCodeSigning, true);
});

test("Azure Trusted Signing needs all four of its settings", (t) => {
  onPlatform(t, "win32");
  assert.throws(() => configured({ AUTOGPT_DESKTOP_WIN_SIGN: "azure" }), /AZURE_SIGN_ENDPOINT, AZURE_SIGN_ACCOUNT/);
  const config = configured({
    AUTOGPT_DESKTOP_WIN_SIGN: "azure",
    AZURE_SIGN_ENDPOINT: "https://eus.codesigning.azure.net",
    AZURE_SIGN_ACCOUNT: "account",
    AZURE_SIGN_PROFILE: "profile",
    AZURE_SIGN_PUBLISHER: "CN=Example",
  });
  assert.deepEqual(config.win.azureSignOptions, {
    endpoint: "https://eus.codesigning.azure.net",
    codeSigningAccountName: "account",
    certificateProfileName: "profile",
    publisherName: "CN=Example",
  });
});

test("a signing setting for another system does not force signing here", (t) => {
  onPlatform(t, "linux");
  const config = configured({ AUTOGPT_DESKTOP_MAC_SIGN: "developer-id", AUTOGPT_DESKTOP_WIN_SIGN: "pfx" });
  assert.equal(config.forceCodeSigning, false);
});

test("the AppImage's name has no version in it, so updating keeps the file's name", () => {
  const config = configured({ AUTOGPT_DESKTOP_VERSION: "1.4.0" });
  assert.ok(!config.appImage.artifactName.includes("version"));
  // Every other file of a release is named after its version.
  for (const name of [config.nsis.artifactName, config.mac.artifactName, config.deb.artifactName]) {
    assert.ok(name.includes("${version}"), name);
  }
});

test("update files are always latest*.yml and nothing is published from a build", () => {
  const config = configured({ AUTOGPT_DESKTOP_VERSION: "1.4.0-rc.1" });
  assert.equal(config.detectUpdateChannel, false);
  assert.deepEqual(config.publish, [{ provider: "github", owner: "ntindle", repo: "autogpt" }]);
});

test("the runtime ships inside the app, and the updater ships with the shell", () => {
  const config = configured();
  assert.deepEqual(config.extraResources, [{ from: "build/runtime", to: "runtime", filter: ["**/*"] }]);
  const manifest = JSON.parse(fs.readFileSync(path.join(DESKTOP, "package.json"), "utf8"));
  // devDependencies are not packed into the app.
  assert.ok(manifest.dependencies["electron-updater"], "electron-updater must be a dependency");
  assert.match(manifest.dependencies["electron-updater"], /^\d/, "pin electron-updater to one version");
});

test("nothing is compiled or run from the dependencies while packaging", () => {
  assert.equal(configured().npmRebuild, false);
  // What makes that safe: no package that ships in the app builds itself
  // on install. A native module would need npmRebuild again.
  const lock = JSON.parse(fs.readFileSync(path.join(DESKTOP, "package-lock.json"), "utf8"));
  const building = Object.entries(lock.packages)
    .filter(([, entry]) => entry.hasInstallScript && !entry.dev)
    .map(([name]) => name);
  assert.deepEqual(building, []);
});

test("certificates and passwords never pass through the configuration", () => {
  const source = fs.readFileSync(CONFIG, "utf8");
  const read = [...source.matchAll(/env\.([A-Z_]+)|env\[name\]/g)].map((match) => match[1]).filter(Boolean);
  assert.deepEqual([...new Set(read)].sort(), [
    "AUTOGPT_DESKTOP_MAC_SIGN",
    "AUTOGPT_DESKTOP_OUTPUT",
    "AUTOGPT_DESKTOP_VARIANT",
    "AUTOGPT_DESKTOP_VERSION",
    "AUTOGPT_DESKTOP_WIN_SIGN",
  ]);
});

// --- the Windows installer and other installs ---------------------------------

const INCLUDE = path.join(DESKTOP, "resources", "installer.nsh");
const TEMPLATE = path.join(
  DESKTOP,
  "node_modules",
  "app-builder-lib",
  "templates",
  "nsis",
  "include",
  "allowOnlyOneInstallerInstance.nsh",
);

function macro(source, name) {
  const found = new RegExp(`^!macro ${name}\\b[\\s\\S]*?^!macroend[ \\t]*$`, "m").exec(source);
  return found ? found[0].replace(/[ \t]+$/gm, "") : null;
}

test("every Windows installer closes the programs of its own install only", () => {
  // Install directories are siblings whose names start the same:
  // ...\Programs\autogpt, ...\Programs\autogpt-voice, ...\Programs\autogpt-voice2.
  for (const variant of ["", "voice"]) {
    const config = configured({ AUTOGPT_DESKTOP_VARIANT: variant });
    assert.equal(config.nsis.include, "resources/installer.nsh");
    assert.equal(config.directories.buildResources, "resources");
  }
  const include = fs.readFileSync(INCLUDE, "utf8").replace(/\r\n/g, "\n");
  // This macro is what makes electron-builder's template use the check below.
  assert.match(include, /^!macro customCheckAppRunning\n {2}!insertmacro IS_POWERSHELL_AVAILABLE\n {2}!insertmacro OWN_CHECK_APP_RUNNING\n!macroend$/m);
  // A program is the install's if its path starts with the directory and a
  // backslash; the directory's name alone is also the start of a sibling's.
  const compared = [...include.matchAll(/^[^#\n]*StartsWith\('([^']*)'/gm)].map((match) => match[1]);
  assert.deepEqual(compared, ["$INSTDIR\\", "$INSTDIR\\"]);
  for (const body of ["OWN_FIND_PROCESS", "OWN_KILL_PROCESS", "OWN_CHECK_APP_RUNNING"].map((name) => macro(include, name))) {
    assert.ok(body, "installer.nsh lost one of its macros");
    assert.ok(!/!insertmacro (FIND|KILL)_PROCESS/.test(body), "a check of electron-builder's own is still used");
  }
});

test("the installer's check is electron-builder's, changed in two places", (t) => {
  if (!fs.existsSync(TEMPLATE)) return t.skip("electron-builder is not installed (npm ci)");
  const template = fs.readFileSync(TEMPLATE, "utf8").replace(/\r\n/g, "\n");
  const include = fs.readFileSync(INCLUDE, "utf8").replace(/\r\n/g, "\n");
  // The template still takes a replacement, and still needs one.
  assert.match(template, /!ifmacrodef customCheckAppRunning\n\s+!insertmacro customCheckAppRunning\n\s+!else/);
  assert.equal(template.split("StartsWith('$INSTDIR', 'CurrentCultureIgnoreCase')").length - 1, 2);
  // What installer.nsh needs from the template and from its include folder.
  assert.ok(macro(template, "IS_POWERSHELL_AVAILABLE"));
  assert.match(template, /!ifmacrondef customCheckAppRunning\n\s+!include "getProcessInfo\.nsh"\n\s+Var pid\n!endif/);
  assert.ok(fs.existsSync(path.join(path.dirname(TEMPLATE), "getProcessInfo.nsh")));
  assert.match(include, /^!include "getProcessInfo\.nsh"\nVar pid$/m);

  const own = (text) =>
    text
      .replaceAll("'$INSTDIR'", "'$INSTDIR\\'")
      .replace("if ((Get-CimInstance", "if (@(Get-CimInstance")
      .replace(/^!macro _CHECK_APP_RUNNING/, "!macro OWN_CHECK_APP_RUNNING")
      .replace(/!(insert)?macro (FIND|KILL)_PROCESS/g, "!$1macro OWN_$2_PROCESS");
  for (const [theirs, ours] of [
    ["FIND_PROCESS", "OWN_FIND_PROCESS"],
    ["KILL_PROCESS", "OWN_KILL_PROCESS"],
    ["_CHECK_APP_RUNNING", "OWN_CHECK_APP_RUNNING"],
  ]) {
    assert.equal(
      macro(include, ours),
      own(macro(template, theirs)),
      `electron-builder changed ${theirs}: carry the change over to ${ours} in resources/installer.nsh`,
    );
  }
});
