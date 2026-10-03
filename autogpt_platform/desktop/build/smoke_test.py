"""Boot an assembled runtime, check that it serves, stop it, check it is gone.

    <runtime>/python/python smoke_test.py <runtime> [--timeout 600]

Run it with the bundle's own interpreter (it uses psutil from the bundle).
This is the shell's contract exercised without the shell: start the runtime
as the bundle's manifest says to, read JSON events from stdout until `ready`,
close stdin, expect exit code 0, no process from the bundle left running, and
no file in the bundle changed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

# The shell kills a runtime that takes a minute to stop (src/runtime.js); a
# stop that would not survive that with room to spare is a failure here.
STOP_BUDGET_SECONDS = 30


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="use this (empty) data directory, e.g. one with a space in its path",
    )
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    data = args.data_dir or Path(tempfile.mkdtemp(prefix="autogpt-smoke-"))
    bundle_before = snapshot(runtime)

    started = time.monotonic()
    process = subprocess.Popen(
        shell_command(runtime),
        cwd=runtime,
        env={**os.environ, "AUTOGPT_DESKTOP_DATA_DIR": str(data)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    url = wait_for_ready(process, args.timeout, started)
    failures = [] if url else ["the runtime never reported ready"]
    if url:
        failures += probe(url)

    assert process.stdin
    process.stdin.close()
    try:
        code = process.wait(STOP_BUDGET_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        failures.append(
            f"the runtime did not stop within {STOP_BUDGET_SECONDS}s of stdin closing"
        )
        code = None
    if code not in (0, None):
        failures.append(f"the runtime exited with code {code}")

    time.sleep(2)
    leftovers = processes_from(runtime)
    if leftovers:
        names = sorted({leftover.info["name"] for leftover in leftovers})
        failures.append(f"processes left running: {names}")
        for leftover in leftovers:  # do not leave the machine dirty
            with contextlib.suppress(psutil.Error):
                leftover.kill()
    failures += changed_files(bundle_before, snapshot(runtime))

    if failures:
        print("\nFAILED")
        for failure in failures:
            print(f"  - {failure}")
        print_logs(data)
        return 1
    print(f"\nOK in {time.monotonic() - started:.0f}s")
    return 0


def snapshot(runtime: Path) -> dict[str, tuple[int, int]]:
    """Size and modification time of every file in the bundle."""
    files = {}
    for path in runtime.rglob("*"):
        status = path.lstat()
        if stat.S_ISREG(status.st_mode):
            files[str(path.relative_to(runtime))] = (status.st_size, status.st_mtime_ns)
    return files


def changed_files(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> list[str]:
    """Running the app must leave its bundle exactly as it was: installed, the
    bundle may be read-only, and on macOS a changed file breaks the code
    signature."""
    changed = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )
    if not changed:
        return []
    listed = ", ".join(changed[:5]) + (" ..." if len(changed) > 5 else "")
    return [f"the run changed {len(changed)} file(s) in the bundle: {listed}"]


def shell_command(runtime: Path) -> list[str]:
    """What the shell runs: the bundle's manifest.json (src/paths.js)."""
    manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest.get(sys.platform, manifest["default"])
    return [str(runtime / entry["command"]), *entry["args"]]


def wait_for_ready(process: subprocess.Popen[str], timeout: int, started: float) -> str | None:
    result: list[str | None] = [None]

    def read() -> None:
        assert process.stdout
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            elapsed = time.monotonic() - started
            print(f"{elapsed:6.1f}s {event.get('event')}: {event.get('message') or event.get('url')}")
            if event.get("event") == "ready":
                result[0] = event["url"]
                return
            if event.get("event") == "error" and event.get("fatal"):
                return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    return result[0]


def probe(url: str) -> list[str]:
    checks = {
        "/healthz": 200,
        "/_agpt/health": 200,
        "/_agpt/docs": 404,
        "/login": 200,
        "/_agpt/api/store/agents?page_size=1": 200,
    }
    failures = []
    for path, expected in checks.items():
        status = fetch_status(url + path)
        print(f"  {path} -> {status}")
        if status != expected:
            failures.append(f"{path} returned {status}, expected {expected}")
    return failures


def fetch_status(url: str) -> int | str:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError) as exc:
        return str(exc)


def processes_from(runtime: Path) -> list[psutil.Process]:
    """Running processes whose executable lives in the bundle."""
    root = os.path.normcase(os.path.realpath(runtime))
    found = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        if process.info["pid"] == os.getpid():
            continue
        executable = process.info["exe"]
        if not executable:
            continue
        if os.path.normcase(os.path.realpath(executable)).startswith(root + os.sep):
            found.append(process)
    return found


def print_logs(data: Path) -> None:
    for log in sorted((data / "logs").glob("*.log")):
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        print(f"\n--- {log.name} (last {len(tail)} lines)")
        print("\n".join(tail))


if __name__ == "__main__":
    sys.exit(main())
