"use strict";

// Stands in for the real runtime: reports progress, then ready, then waits
// for the shell to close stdin and exits cleanly.
const mode = process.argv[2] || "normal";

console.log(JSON.stringify({ event: "progress", step: "boot", message: "Booting" }));
console.log("a plain log line");
if (mode === "crash") {
  console.log(JSON.stringify({ event: "error", message: "boom", fatal: true }));
  process.exit(3);
}
console.log(JSON.stringify({ event: "ready", url: "http://127.0.0.1:43117" }));

if (mode === "ignore-stdin") {
  setInterval(() => {}, 1000);
} else if (mode === "slow-stop") {
  process.stdin.on("end", () => {
    console.log(JSON.stringify({ event: "progress", message: "Finishing", grace_seconds: 30 }));
    setTimeout(() => process.exit(0), 1500);
  });
  process.stdin.resume();
} else {
  process.stdin.on("end", () => {
    console.log(JSON.stringify({ event: "progress", message: "stopped" }));
    process.exit(0);
  });
  process.stdin.resume();
}
