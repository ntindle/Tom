// Remove the app. The program goes; the user's data stays.

import fs from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { verifyFirewall } from "../lib/app";
import { dataDir, kind, mayUninstall } from "../lib/config";
import { appProcesses, platform, run } from "../lib/platform";
import { describe } from "../lib/processes";

test.skip(!mayUninstall, "the app was not installed by these tests (set AUTOGPT_E2E_UNINSTALL=1 to remove it)");

test("uninstalls the app and keeps the data", async () => {
  test.setTimeout(45 * 60_000);
  expect(platform.isInstalled()).toBe(true);
  await platform.uninstall();

  expect(platform.isInstalled()).toBe(false);
  const installDir = platform.installDir();
  if (installDir) expect(fs.existsSync(installDir), `${installDir} is gone`).toBe(false);
  expect(describe(appProcesses()), "processes still running after uninstalling").toBe("");
  expect(await registeredWithSystem(), "the system no longer lists the app").toBe(false);

  // The database, with the account and the agent in it (electron-builder.config.js
  // `deleteAppDataOnUninstall: false`; the other installers never touch it).
  expect(fs.existsSync(path.join(dataDir(), "postgres", "PG_VERSION")), `${dataDir()} is kept`).toBe(true);
  await verifyFirewall();
});

/** Whether the OS still believes the app is installed. */
async function registeredWithSystem(): Promise<boolean> {
  if (kind === "nsis") {
    // Exits with 1 when no uninstall entry mentions the app.
    const entries = await run(
      "reg.exe",
      ["query", "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall", "/s", "/f", "AutoGPT", "/d"],
      { check: false },
    );
    return entries.code === 0;
  }
  if (kind === "deb") {
    const status = await run("dpkg-query", ["-W", "-f=${Status}", "autogpt"], { check: false });
    return status.code === 0 && status.output.includes("install ok installed");
  }
  return false;
}
