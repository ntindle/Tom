"use strict";

// How the installers are built:
//
//   npx electron-builder --config electron-builder.config.js --publish never
//
// One configuration for every kind of build. What differs between a
// developer's build and a signed release comes from the environment, so that
// nothing in the repository is edited to cut a release:
//
//   AUTOGPT_DESKTOP_VERSION    the app's version; package.json's
//                              0.0.0-dev.0 otherwise (a development build,
//                              which never looks for updates)
//   AUTOGPT_DESKTOP_OUTPUT     where the installers are written; dist
//   AUTOGPT_DESKTOP_MAC_SIGN   "developer-id": sign with the Developer ID
//                              certificate in CSC_LINK (or the keychain),
//                              hardened runtime, notarize. Anything else:
//                              ad-hoc signature, no hardened runtime.
//   AUTOGPT_DESKTOP_WIN_SIGN   "azure": Azure Trusted Signing, described by
//                              AZURE_SIGN_ENDPOINT, AZURE_SIGN_ACCOUNT,
//                              AZURE_SIGN_PROFILE and AZURE_SIGN_PUBLISHER.
//                              "pfx": the certificate in WIN_CSC_LINK.
//                              Anything else: unsigned.
//
// The certificates and passwords themselves are read by electron-builder
// from its own variables (CSC_LINK, CSC_KEY_PASSWORD, APPLE_API_KEY, ...,
// WIN_CSC_LINK, AZURE_CLIENT_SECRET, ...). They never pass through this file.

const path = require("node:path");

const {
  requireNotarizedApp,
  signMacApp,
  vendorSignedPaths,
  SHELL_ENTITLEMENTS,
  HELPER_ENTITLEMENTS,
} = require("./build/mac_sign");

// Semantic versioning to the letter: no leading zeros (1.2.03, 1.2.3-rc.01).
// electron-builder would take them, and electron-updater then refuses to
// start in the app that has such a version and to read it from latest.yml.
const NUMBER = "(0|[1-9][0-9]*)";
const IDENTIFIER = "(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)";
const SEMVER = new RegExp(`^${NUMBER}[.]${NUMBER}[.]${NUMBER}(-${IDENTIFIER}([.]${IDENTIFIER})*)?$`);
const AZURE_SETTINGS = {
  endpoint: "AZURE_SIGN_ENDPOINT",
  codeSigningAccountName: "AZURE_SIGN_ACCOUNT",
  certificateProfileName: "AZURE_SIGN_PROFILE",
  publisherName: "AZURE_SIGN_PUBLISHER",
};

const PRODUCT = "AutoGPT";

const env = process.env;
const macDeveloperId = env.AUTOGPT_DESKTOP_MAC_SIGN === "developer-id";
const windowsSigning = ["azure", "pfx"].includes(env.AUTOGPT_DESKTOP_WIN_SIGN)
  ? env.AUTOGPT_DESKTOP_WIN_SIGN
  : null;

function version() {
  const value = env.AUTOGPT_DESKTOP_VERSION;
  if (!value) return {};
  if (!SEMVER.test(value)) {
    throw new Error(`AUTOGPT_DESKTOP_VERSION must look like 1.2.3 or 1.2.3-rc.1, not "${value}"`);
  }
  return { version: value };
}

function mac() {
  const common = {
    category: "public.app-category.productivity",
    artifactName: "AutoGPT-${version}-${arch}.${ext}",
    gatekeeperAssess: false,
  };
  if (!macDeveloperId) {
    // No certificate. An unsigned bundle counts as damaged on Apple silicon;
    // an ad-hoc signature makes a download intact, though not trusted.
    return {
      ...common,
      target: [{ target: "dmg", arch: ["arm64"] }],
      identity: "-",
      hardenedRuntime: false,
      // The Claude Code CLI is Anthropic's signed program and ships
      // unmodified; an ad-hoc signature would replace Anthropic's.
      signIgnore: vendorSignedPaths(),
    };
  }
  return {
    ...common,
    // The zip is what an installed app updates itself from; the disk image
    // is for people. Neither is changed after it is built: the notarization
    // ticket is stapled to the app inside them, and a later change would
    // falsify the checksums in latest-mac.yml.
    target: [
      { target: "dmg", arch: ["arm64"] },
      { target: "zip", arch: ["arm64"] },
    ],
    hardenedRuntime: true,
    notarize: true,
    entitlements: SHELL_ENTITLEMENTS,
    entitlementsInherit: HELPER_ENTITLEMENTS,
    sign: signMacApp,
  };
}

function windows() {
  const common = { target: [{ target: "nsis", arch: ["x64"] }] };
  if (!windowsSigning) return common;
  return {
    ...common,
    // electron-builder signs every .exe it copies. The Claude Code CLI is
    // Anthropic's signed program and ships unmodified.
    signExts: ["!claude.exe"],
    ...(windowsSigning === "azure" ? { azureSignOptions: azureSignOptions() } : {}),
  };
}

function azureSignOptions() {
  const missing = Object.values(AZURE_SETTINGS).filter((name) => !env[name]);
  if (missing.length > 0) {
    throw new Error(`Azure Trusted Signing needs ${missing.join(", ")}`);
  }
  return Object.fromEntries(
    Object.entries(AZURE_SETTINGS).map(([option, name]) => [option, env[name]]),
  );
}

// On macOS electron-builder ignores forceCodeSigning when `mac.sign` is a
// function: with no Developer ID identity it packs an unsigned app and says
// nothing. This runs when it starts on the disk image and on the zip, and
// stops the build unless the app it packed is signed and notarized.
function signedAppOrNothing() {
  let checked = null;
  return (event) => {
    checked ||= requireNotarizedApp(path.join(path.dirname(event.file), "mac-arm64", `${PRODUCT}.app`));
    return checked;
  };
}

// A build that was asked to sign must not quietly come out unsigned. This
// is what makes electron-builder refuse on Windows.
function signsOnThisSystem() {
  if (process.platform === "darwin") return macDeveloperId;
  if (process.platform === "win32") return Boolean(windowsSigning);
  return false;
}

module.exports = {
  appId: "co.agpt.autogpt.desktop",
  productName: PRODUCT,
  directories: {
    output: env.AUTOGPT_DESKTOP_OUTPUT || "dist",
    buildResources: "resources",
  },
  files: ["src/**/*", "package.json"],
  extraResources: [{ from: "build/runtime", to: "runtime", filter: ["**/*"] }],
  asar: true,
  // The app has no native module, so there is nothing to compile for
  // Electron. Left on, electron-builder runs a rebuild of the dependencies
  // while it packages, which is also when it holds the certificates.
  npmRebuild: false,
  extraMetadata: {
    ...version(),
    // Read by src/updater.js: macOS replaces an app in place only when the
    // old and the new one are signed by the same Developer ID.
    autogptDesktop: { macDeveloperId },
  },
  forceCodeSigning: signsOnThisSystem(),
  ...(macDeveloperId && process.platform === "darwin" ? { artifactBuildStarted: signedAppOrNothing() } : {}),
  // Where an installed app looks for updates (written to app-update.yml).
  // Nothing is uploaded from here: the release workflow on `main` does that.
  publish: [{ provider: "github", owner: "ntindle", repo: "autogpt" }],
  // The update files are always latest*.yml, also for 1.2.3-rc.1: there is
  // one channel.
  detectUpdateChannel: false,
  win: windows(),
  nsis: {
    oneClick: true,
    perMachine: false,
    deleteAppDataOnUninstall: false,
    artifactName: "AutoGPT-Setup-${version}-${arch}.${ext}",
  },
  mac: mac(),
  linux: {
    target: ["AppImage", "deb"],
    category: "Utility",
  },
  // No version in the AppImage's name: it updates itself by replacing its
  // own file, and a launcher that points at it must keep working.
  appImage: { artifactName: "AutoGPT-${arch}.${ext}" },
  deb: { artifactName: "AutoGPT-${version}-${arch}.${ext}" },
};
