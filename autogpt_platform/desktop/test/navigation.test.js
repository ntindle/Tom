"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");

const { classifyMainNavigation, classifyWindowOpen } = require("../src/navigation");

const APP = "http://127.0.0.1:17119";

test("the app's own pages open inside the app", () => {
  assert.equal(
    classifyWindowOpen({ url: `${APP}/build?flowID=1`, disposition: "foreground-tab" }, APP),
    "app",
  );
  assert.equal(classifyMainNavigation(`${APP}/library`, APP), "app");
});

test("sign-in popups stay in the app so they can report back", () => {
  // preOpenOAuthPopup() opens a blank, sized window and navigates it afterwards
  assert.equal(classifyWindowOpen({ url: "about:blank", disposition: "new-window" }, APP), "app");
  assert.equal(
    classifyWindowOpen(
      { url: "https://accounts.google.com/o/oauth2/auth", disposition: "new-window" },
      APP,
    ),
    "app",
  );
});

test("ordinary links to other sites go to the user's browser", () => {
  assert.equal(
    classifyWindowOpen({ url: "https://agpt.co/docs", disposition: "foreground-tab" }, APP),
    "browser",
  );
  assert.equal(
    classifyWindowOpen({ url: "mailto:help@agpt.co", disposition: "foreground-tab" }, APP),
    "browser",
  );
  assert.equal(classifyMainNavigation("https://example.com/", APP), "browser");
});

test("a different port on the same host is a different site", () => {
  assert.equal(classifyMainNavigation("http://127.0.0.1:9999/", APP), "browser");
});

test("anything that is neither the app nor a web link is refused", () => {
  assert.equal(
    classifyWindowOpen({ url: "file:///etc/passwd", disposition: "foreground-tab" }, APP),
    "deny",
  );
  assert.equal(
    classifyWindowOpen({ url: "about:blank", disposition: "foreground-tab" }, APP),
    "deny",
  );
  assert.equal(classifyMainNavigation("javascript:alert(1)", APP), "deny");
  assert.equal(classifyMainNavigation("not a url", APP), "deny");
});

test("before the app has a URL nothing counts as the app", () => {
  assert.equal(classifyMainNavigation(`${APP}/`, null), "browser");
});
