"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");

const { signMacApp } = require("../build/mac_sign");

const DESKTOP = path.join(__dirname, "..");
const CONFIG = path.join(DESKTOP, "electron-builder.config.js");
const SETTINGS = [
  "AUTOGPT_DESKTOP_VERSION",
  "AUTOGPT_DESKTOP_OUTPUT",
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
  // the disk image and the zip get the same answer from one look.
  const dmg = hook({ file: path.join(DESKTOP, "dist", "AutoGPT-1.4.0-arm64.dmg") });
  const zip = hook({ file: path.join(DESKTOP, "dist", "AutoGPT-1.4.0-arm64.zip") });
  assert.equal(dmg, zip);
  await assert.rejects(dmg, /A Developer ID build was asked for, but AutoGPT\.app is not signed/);
});

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
    "AUTOGPT_DESKTOP_VERSION",
    "AUTOGPT_DESKTOP_WIN_SIGN",
  ]);
});
