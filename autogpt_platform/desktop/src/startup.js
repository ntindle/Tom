"use strict";

const message = document.getElementById("message");

window.autogpt.onRuntimeEvent((event) => {
  if (event.event === "progress" && event.message) {
    message.textContent = event.message;
  } else if (event.event === "ready") {
    message.textContent = "Opening AutoGPT…";
  } else if (event.event === "error") {
    message.textContent = event.message || "Something went wrong.";
    if (event.fatal) document.body.classList.add("failed");
  }
});

document.getElementById("logs").addEventListener("click", () => window.autogpt.openLogs());
document.getElementById("quit").addEventListener("click", () => window.autogpt.quit());
