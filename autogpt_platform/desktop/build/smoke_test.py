"""Boot an assembled runtime, check that it serves, stop it, check it is gone.

    <runtime>/python/python smoke_test.py <runtime> [--timeout 600]

Run it with the bundle's own interpreter (it uses psutil from the bundle).
This is the shell's contract exercised without the shell: start the runtime,
read JSON events from stdout until `ready`, close stdin, expect exit code 0
and no process from the bundle left running.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil


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

    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-m", "autogpt_desktop", "serve"],
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
        code = process.wait(90)
    except subprocess.TimeoutExpired:
        process.kill()
        failures.append("the runtime did not stop within 90s of stdin closing")
        code = None
    if code not in (0, None):
        failures.append(f"the runtime exited with code {code}")

    time.sleep(2)
    leftovers = processes_from(runtime)
    if leftovers:
        names = sorted({leftover.info["name"] for leftover in leftovers})
        failures.append(f"processes left running: {names}")
        for leftover in leftovers:  # do not leave the machine dirty
            try:
                leftover.kill()
            except psutil.Error:
                pass

    if failures:
        print("\nFAILED")
        for failure in failures:
            print(f"  - {failure}")
        print_logs(data)
        return 1
    print(f"\nOK in {time.monotonic() - started:.0f}s")
    return 0


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
