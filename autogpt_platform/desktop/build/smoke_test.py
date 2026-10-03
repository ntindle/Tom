"""Boot an assembled runtime, check that it serves, stop it, check it is gone.

    <runtime>/python/python smoke_test.py <runtime> [--timeout 600] [--quick]
                                          [--profile compact|balanced|isolated]

Run it with the bundle's own interpreter (it uses psutil, psycopg2 and redis
from the bundle). This is the shell's contract exercised without the shell:
start the runtime as the bundle's manifest says to, read JSON events from
stdout until `ready`, close stdin, expect exit code 0, no process from the
bundle left running, and no file in the bundle changed.

Before anything is started, the backend in the bundle is checked against
what the service host depends on (runtime/tests/backend_contract.py).

While the first start is up, the stack itself is put to work: every service
answers, an agent runs to the right result, memory is inside the profile's
budget, the process hosting the executor is killed with a run in flight and
comes back to finish that run and take another. After every stop, each service host's log must show that
it was asked, cleaned up, and left by itself.

It starts the runtime three times on one data directory, because the owner
account's promises are about restarts:

  1. a fresh install: the first account to sign up is an admin at once, and
     a second sign-up is refused;
  2. a restart: the owner is still an admin, and the frontend now refuses
     sign-ups with its own "not allowed" message; runs left "running" by the
     first start are settled and its stale lock is gone;
  3. a restart with a password-reset file waiting, and nobody an admin: the
     old password stops working, the new one works, the owner is admin again.

Every start must come up on the address the first one had: sessions and OAuth
redirect URLs hang off it.

`--quick` stops after the first.
"""

from __future__ import annotations

import argparse
import contextlib
import http.cookiejar
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import psutil
import psycopg2

# The shell kills a runtime that takes a minute to stop (src/runtime.js); a
# stop that would not survive that with room to spare is a failure here.
STOP_BUDGET_SECONDS = 30

OWNER_EMAIL = "owner@smoke.test"
OWNER_PASSWORD = "first-password-of-the-owner"
NEW_OWNER_PASSWORD = "second-password-of-the-owner"
# Cheap, read-only, and behind `requires_admin_user`
# (backend/api/features/admin/store_admin_routes.py).
ADMIN_ROUTE = "/_agpt/api/store/admin/listings?page_size=1"
# CalculatorBlock (backend/blocks/maths.py): operation, a, b -> result.
CALCULATOR_BLOCK = "b1ab9b19-67a6-406d-abf5-2dba76d00c79"
# CountdownTimerBlock (backend/blocks/time_blocks.py): seconds, input_message
# -> output_message. Long enough to still be running when its executor dies.
COUNTDOWN_BLOCK = "d67a9c52-5e4e-11e2-bcfd-0800200c9a71"
IN_FLIGHT_SECONDS = 20
AGENT_RUN_SECONDS = 180
RESTART_SECONDS = 240
# Memory is read this long after ready, once start-up work has drained. On a
# fast machine that is before the skills catalog publish begins
# (supervisor.py: a minute); on a slow one the publisher may be running, so
# it is listed and left out of the total.
SETTLE_SECONDS = 45
CATALOG = "skills-catalog"
CATALOG_NOT_BEFORE_SECONDS = 55
CATALOG_SECONDS = 420
STALE_LOCK = "exec_lock:left-by-the-smoke-test"

# The whole stack, idle: memory unique to its processes (USS; on macOS the
# physical footprint), in MB. Measured on Windows x64, 2026-10-03: balanced
# and compact (three service hosts) 2,900-3,000; isolated (eight) 5,900. The
# budget is that plus room for an OS that shares less, and for upstream
# growing a little between syncs; it is there to catch a fourth interpreter
# or a doubled import, not a few megabytes.
MEMORY_BUDGET_MB = {"compact": 3800, "balanced": 3800, "isolated": 7200}

CONTRACT_SCRIPT = Path(__file__).resolve().parents[1] / "runtime" / "tests" / "backend_contract.py"


class Install:
    """What one start leaves for the next ones to check against."""

    def __init__(self, quick: bool) -> None:
        self.quick = quick
        self.address: str | None = None
        self.owner: Browser | None = None
        self.profile: str | None = None
        self.runtime: psutil.Process | None = None
        self.ready_at = 0.0
        self.ready_wall = 0.0
        # Agent runs of the first start, by what the next start must have
        # made of them.
        self.left_running: dict[str, str] = {}
        # A run still going when the first start was stopped.
        self.in_flight: str | None = None
        self.quit_mid_run = False


Checks = Callable[[str, Path, Install], list[str]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--timeout", type=int, default=600, help="seconds, per start")
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="use this (empty) data directory, e.g. one with a space in its path",
    )
    parser.add_argument("--quick", action="store_true", help="one start, no restarts")
    parser.add_argument(
        "--profile",
        default=os.environ.get("AUTOGPT_DESKTOP_PROFILE"),
        help="set AUTOGPT_DESKTOP_PROFILE in the data directory's settings.env",
    )
    args = parser.parse_args()
    # Service logs carry emoji, which a Windows console or pipe cannot print.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")
    runtime = args.runtime.resolve()
    sys.path.append(str(runtime))  # for autogpt_desktop.apps, as the bundle has it
    data = args.data_dir or Path(tempfile.mkdtemp(prefix="autogpt-smoke-"))
    if args.profile:
        choose_profile(data, args.profile)
    bundle_before = snapshot(runtime)
    starts: list[Checks] = [fresh_install]
    if not args.quick:
        starts += [restart, restart_with_a_password_reset]

    started = time.monotonic()
    failures = backend_contract(runtime)
    install = Install(args.quick)
    for number, checks in enumerate(starts, start=1):
        if failures:
            break
        print(f"\n== start {number} of {len(starts)}: {checks.__name__.replace('_', ' ')}")
        failures += run(runtime, data, args.timeout, checks, install)
    failures += changed_files(bundle_before, snapshot(runtime))

    if failures:
        print("\nFAILED")
        for failure in failures:
            print(f"  - {failure}")
        print_logs(data)
        return 1
    print(f"\nOK in {time.monotonic() - started:.0f}s")
    return 0


def run(runtime: Path, data: Path, timeout: int, checks: Checks, install: Install) -> list[str]:
    """One start and stop of the runtime, with `checks` made while it is up."""
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
    install.runtime = psutil.Process(process.pid)
    url = wait_for_ready(process, timeout, started, install)
    install.ready_at, install.ready_wall = time.monotonic(), time.time()
    failures = [] if url else ["the runtime never reported ready"]
    install.quit_mid_run = False
    if url:
        failures += checked(checks, url, data, install)
    failures += stop(process)
    if url:
        failures += stopped_gracefully(data, install)

    time.sleep(2)
    leftovers = processes_from(runtime)
    if leftovers:
        names = sorted({leftover.info["name"] for leftover in leftovers})
        failures.append(f"processes left running: {names}")
        for leftover in leftovers:  # do not leave the machine dirty
            with contextlib.suppress(psutil.Error):
                leftover.kill()
    return failures


def checked(checks: Checks, url: str, data: Path, install: Install) -> list[str]:
    """A check that blows up is a failure, and the runtime is still stopped."""
    try:
        return checks(url, data, install)
    except Exception as exc:
        return [f"{checks.__name__} raised {type(exc).__name__}: {exc}"]


def stop(process: subprocess.Popen[str]) -> list[str]:
    assert process.stdin
    asked = time.monotonic()
    process.stdin.close()
    try:
        code = process.wait(STOP_BUDGET_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        return [f"the runtime did not stop within {STOP_BUDGET_SECONDS}s of stdin closing"]
    print(f"  stopped in {time.monotonic() - asked:.1f}s")
    return [f"the runtime exited with code {code}"] if code != 0 else []


def choose_profile(data: Path, profile: str) -> None:
    settings = data / "config" / "settings.env"
    settings.parent.mkdir(parents=True, exist_ok=True)
    with open(settings, "a", encoding="utf-8") as stream:
        stream.write(f"AUTOGPT_DESKTOP_PROFILE={profile}\n")


def backend_contract(runtime: Path) -> list[str]:
    """The bundle's backend still has the shape the service host depends on.
    Checked in an interpreter of its own, as a service would import it."""
    print("== the backend against the service host's contract")
    result = subprocess.run(
        [shell_command(runtime)[0], "-B", str(CONTRACT_SCRIPT), str(runtime)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )
    lines = result.stdout.splitlines()  # among the backend's own logging
    broken = [line for line in lines if line.startswith("BROKEN:")]
    print("  " + "\n  ".join([*broken, *lines[-1:]]))
    if result.returncode == 0:
        return []
    report = "\n".join(broken) or (result.stdout + result.stderr).strip()[-2000:]
    return [f"the backend broke the service host's contract:\n{report}"]


def stopped_gracefully(data: Path, install: Install) -> list[str]:
    """Each service host was asked, ran its services' cleanup and left by
    itself. On Windows that is the named event doing what a signal cannot;
    before the host, a Windows service was simply ended.

    Two cleanups are allowed not to finish inside the stop: the executor's
    while a run is in flight (it waits for the run, and a quit does not),
    and the scheduler's while one of its jobs is. The services' own last
    words ("Cleanup done", "Terminated") are upstream's, logged when run()
    returns after cleanup; they are required of the services that do."""
    from autogpt_desktop import apps

    report = Report()
    may_overrun = {"scheduler"} | ({"executor"} if install.quit_mid_run else set())
    last_words = {"database-manager", "batch-executor", "notification", *apps.API_SERVERS}
    for group in apps.layout(install.profile != "isolated"):
        log = (data / "logs" / f"{group.name}.log").read_text(encoding="utf-8", errors="replace")
        _, asked, after = log.rpartition(f"[host {group.name}] stopping: asked to stop")
        report.expect(f"{group.name} was asked to stop", bool(asked), True)
        left = f"[host {group.name}] stopped with code 0" in after
        report.expect(f"{group.name} left by itself", left, True)
        cleaned = [
            name
            for name in group.services
            if re.search(rf"\] {re.escape(name)} \([^)]*\) cleaned up in", after)
        ]
        required = [name for name in group.services if name not in may_overrun]
        finished = [name for name in cleaned if name in required]
        report.expect(f"{group.name}: cleanup finished for", finished, required, shown=cleaned)
        if last_words & set(group.services):
            own_words = "Cleanup done" in after and "Terminated" in after
            report.expect(f'{group.name}: "Cleanup done" and "Terminated" logged', own_words, True)
    return report.failures


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


def wait_for_ready(
    process: subprocess.Popen[str], timeout: int, started: float, install: Install
) -> str | None:
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
            if event.get("step") == "profile":
                named = re.search(r"Using the (\w+) profile", str(event.get("message")))
                install.profile = named.group(1) if named else None
            if event.get("event") == "ready":
                result[0] = event["url"]
                return
            if event.get("event") == "error" and event.get("fatal"):
                return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    return result[0]


def fresh_install(url: str, data: Path, install: Install) -> list[str]:
    report = Report()
    install.address = url
    report.failures += probe(url)
    # The app's address is predictable, so a page open in the user's browser
    # must not be able to claim the owner account with a blind request.
    status, _ = Browser(url, origin="https://example.com").sign_up(
        "page@smoke.test", "password-of-a-web-page"
    )
    report.expect("a sign-up sent by a page on another site is refused", status, 403)
    with contextlib.closing(database(data)) as connection:
        report.expect("accounts before the first sign-up", count_accounts(connection), 0)

    owner = Browser(url)
    status, _ = owner.sign_up(OWNER_EMAIL, OWNER_PASSWORD)
    report.expect("the first sign-up", status, 200)
    report.expect("the first account's role, in its first session", owner.role(), "admin")
    report.expect("an admin route without a token", Browser(url).get(ADMIN_ROUTE)[0], 401)
    report.expect("an admin route with the owner's token", owner.admin_route(), 200)

    status, _ = Browser(url).sign_up("second@smoke.test", "password-of-a-second-person")
    report.expect("a second sign-up is refused", status in (403, 422), True, shown=status)
    with contextlib.closing(database(data)) as connection:
        report.expect("accounts after the refused sign-up", count_accounts(connection), 1)
        report.expect(
            "role of an account inserted as postgres (as a migration would)",
            insert_as_postgres(connection),
            None,
        )
    work_the_stack(data, install, owner, report)
    return report.failures


def work_the_stack(data: Path, install: Install, owner: Browser, report: Report) -> None:
    """The service hosts under real use: see the module docstring."""
    from autogpt_desktop import apps

    assert install.runtime
    ports = json.loads((data / "config" / "ports.json").read_text(encoding="utf-8"))
    groups = apps.layout(install.profile != "isolated")
    expected = sorted(group.name for group in groups)
    report.expect(f"service hosts of the {install.profile} profile", sorted(hosts(install)), expected)
    health = apps.health_urls(ports)
    for name, address in health.items():
        report.expect(f"{name} answers at {address.split('/', 3)[3] or '/'}", fetch_status(address), 200)
    with contextlib.closing(database(data)) as connection:
        report.expect("PostgreSQL's connection limit", setting(connection, "max_connections"), "50")
        report.note("PostgreSQL connections in use", connections(connection))

    first = run_agent(owner, report, "an agent run", 6, 7)

    settle = install.ready_at + SETTLE_SECONDS - time.monotonic()
    time.sleep(max(0.0, settle))
    catalog_log = data / "logs" / "skills-catalog.log"
    if time.monotonic() - install.ready_at < CATALOG_NOT_BEFORE_SECONDS:
        report.expect("the skills catalog publish has not started yet", catalog_log.exists(), False)
    used = memory_table(install.runtime)
    budget = MEMORY_BUDGET_MB.get(str(install.profile), 0)
    report.expect(f"memory within the {install.profile} budget of {budget} MB", used <= budget, True, shown=f"{used:.0f} MB")
    open_files(install, report)

    # The executor dies with a run in flight. Its lock on the run outlives
    # it; unless that is cleared before the new executor starts, the run's
    # redelivered message is dropped and the run stays "running" for ever.
    executor_host = next(group for group in groups if "executor" in group.services)
    cache = valkey(data, ports)
    cache.set(STALE_LOCK, "an executor that is gone", ex=300)
    in_flight = start_run(owner, report, "a run in flight", countdown(IN_FLIGHT_SECONDS))
    if in_flight:
        running = wait_for(lambda: run_status(owner, in_flight) == "RUNNING", AGENT_RUN_SECONDS)
        report.expect("a run is in flight when its executor is killed", running, True)
    restart_host(install, executor_host.name, [health[name] for name in executor_host.services], report)
    report.expect("the dead executor's lock was cleared before the restart", cache.exists(STALE_LOCK), 0)
    if in_flight:
        what = "the run that was in flight, picked up by the new executor,"
        finish_run(owner, report, what, in_flight, "output_message", "the timer ran out")
    second = run_agent(owner, report, "an agent run after the restart", 5, 9)

    if not install.quick:
        skills_catalog(data, install, report)
        leave_runs_running(data, install, first, second)
        cache.set(STALE_LOCK, "an executor that is gone", ex=300)
        # And a real one: the app is about to quit with this run in flight.
        install.in_flight = start_run(owner, report, "a run in flight at quit", countdown(IN_FLIGHT_SECONDS))
        if install.in_flight:
            running = wait_for(lambda: run_status(owner, install.in_flight or "") == "RUNNING", AGENT_RUN_SECONDS)
            report.expect("a run is in flight when the app quits", running, True)
            install.quit_mid_run = running


def open_files(install: Install, report: Report) -> None:
    """What each service host holds open, for the record: a merged host has
    several services' files and sockets under one per-process limit (the
    runtime raises the soft limit on POSIX; process.raise_file_limit)."""
    for name, host in sorted(hosts(install).items()):
        with contextlib.suppress(psutil.Error):
            if sys.platform == "win32":
                report.note(f"{name}: open handles", host.num_handles())
            else:
                report.note(f"{name}: open file descriptors", host.num_fds())
    if sys.platform != "win32":
        import resource

        report.note("the smoke test's own open-file limit", resource.getrlimit(resource.RLIMIT_NOFILE))


def hosts(install: Install) -> dict[str, psutil.Process]:
    """The runtime's service hosts, by the name the supervisor gave each."""
    assert install.runtime
    found = {}
    for child in install.runtime.children():
        with contextlib.suppress(psutil.Error):
            command = child.cmdline()
            if "--name" in command and "servicehost" in " ".join(command):
                found[command[command.index("--name") + 1]] = child
    return found


def calculator(a: int, b: int) -> dict:
    return {"block_id": CALCULATOR_BLOCK, "input_default": {"operation": "Multiply", "a": a, "b": b}}


def countdown(seconds: int) -> dict:
    return {
        "block_id": COUNTDOWN_BLOCK,
        "input_default": {"seconds": seconds, "input_message": "the timer ran out"},
    }


def run_agent(owner: Browser, report: Report, what: str, a: int, b: int) -> str | None:
    """A graph of one calculator block, run through the REST API
    (rest -> RabbitMQ -> executor -> database-manager), must give a * b.
    Returns the run's id."""
    run = start_run(owner, report, what, calculator(a, b))
    if run:
        finish_run(owner, report, what, run, "result", float(a * b))
    return run.rpartition("/")[2] if run else None


def start_run(owner: Browser, report: Report, what: str, node: dict) -> str | None:
    """Create a graph of one block and run it. Returns the path the run is
    read back from."""
    node = {**node, "metadata": {"position": {"x": 0, "y": 0}}}
    graph = {"name": f"Smoke: {what}", "description": "One block.", "nodes": [node], "links": []}
    status, created = owner.api("/graphs", {"graph": graph})
    if status != 200:
        report.expect(f"{what}: the graph is created", status, 200, shown=f"{status} {created}")
        return None
    path = f"/graphs/{created['id']}"
    start = {"inputs": {}, "credentials_inputs": {}}
    status, run = owner.api(f"{path}/execute/{created['version']}", start)
    if status != 200:
        report.expect(f"{what}: the run is accepted", status, 200, shown=f"{status} {run}")
        return None
    return f"{path}/executions/{run['id']}"


def run_status(owner: Browser, run: str) -> object:
    return owner.api(run)[1].get("status")


def finish_run(
    owner: Browser, report: Report, what: str, run: str, output: str, expected: object
) -> None:
    started = time.monotonic()
    wait_for(
        lambda: run_status(owner, run) not in ("QUEUED", "RUNNING", "INCOMPLETE", None),
        AGENT_RUN_SECONDS,
    )
    _, execution = owner.api(run)
    took = f"{execution.get('status')} after {time.monotonic() - started:.0f}s"
    report.expect(f"{what} ends", execution.get("status"), "COMPLETED", shown=took)
    results = [
        node.get("output_data", {}).get(output) for node in execution.get("node_executions", [])
    ]
    report.expect(f"{what}: its {output}", results, [[expected]])


def restart_host(install: Install, name: str, health: list[str], report: Report) -> None:
    """Kill a service host; the supervisor must bring it back, serving."""
    victim = hosts(install)[name]
    killed = time.monotonic()
    victim.kill()

    def back() -> bool:
        replacement = hosts(install).get(name)
        return bool(replacement and replacement.pid != victim.pid)

    def serving() -> bool:
        return all(fetch_status(address) == 200 for address in health)

    restarted = wait_for(back, RESTART_SECONDS) and wait_for(serving, RESTART_SECONDS)
    took = f"{time.monotonic() - killed:.0f}s"
    report.expect(f"{name}, killed, is restarted and serving", restarted, True, shown=took)


def skills_catalog(data: Path, install: Install, report: Report) -> None:
    """One more backend interpreter, 700 MB while it runs: not during the
    start, and not at all once this bundle's catalog is in."""
    assert install.runtime
    log = data / "logs" / "skills-catalog.log"
    marker = data / "config" / "skills-catalog.published"
    report.expect("the skills catalog publish starts", wait_for(log.exists, CATALOG_SECONDS), True)
    for child in install.runtime.children():
        with contextlib.suppress(psutil.Error):
            if "publish_skills_catalog" in " ".join(child.cmdline()):
                after = child.create_time() - install.ready_wall
                on_time = after >= CATALOG_NOT_BEFORE_SECONDS
                report.expect("...once the app has been up a while", on_time, True, shown=f"{after:.0f}s")
    published = wait_for(marker.exists, CATALOG_SECONDS)
    # It downloads the catalog from GitHub; a machine without a network has
    # no catalog and loses nothing else.
    report.note("the skills catalog was published", published)


def leave_runs_running(data: Path, install: Install, first: str | None, second: str | None) -> None:
    """What a crash leaves: runs marked running that nothing is working on.
    The next start fails the one that is days old and ends the one the user
    stopped; it must not resume either."""
    if not (first and second):
        return
    with contextlib.closing(database(data)) as connection:
        cursor = connection.cursor()
        cursor.execute(
            'UPDATE platform."AgentGraphExecution" SET "executionStatus" = \'RUNNING\', '
            '"createdAt" = "createdAt" - interval \'2 days\' WHERE id = %s',
            (first,),
        )
        cursor.execute(
            'UPDATE platform."AgentNodeExecution" SET "executionStatus" = \'RUNNING\' '
            'WHERE "agentGraphExecutionId" = %s',
            (first,),
        )
        cursor.execute(
            'UPDATE platform."AgentGraphExecution" SET "executionStatus" = \'RUNNING\' WHERE id = %s',
            (second,),
        )
    (data / "run" / "stopped-runs.json").write_text(json.dumps([second]), encoding="utf-8")
    install.left_running = {first: "FAILED", second: "TERMINATED"}


def settled_runs(data: Path, install: Install, report: Report, owner: Browser) -> None:
    """What the restart made of the runs the first start left behind."""
    if install.in_flight:
        what = "the run in flight when the app quit, picked up after the restart,"
        finish_run(owner, report, what, install.in_flight, "output_message", "the timer ran out")
    ports = json.loads((data / "config" / "ports.json").read_text(encoding="utf-8"))
    with contextlib.closing(database(data)) as connection:
        cursor = connection.cursor()
        for run, expected in install.left_running.items():
            cursor.execute(
                'SELECT "executionStatus"::text FROM platform."AgentGraphExecution" WHERE id = %s',
                (run,),
            )
            report.expect(f"a run left running is {expected} after a restart", cursor.fetchone()[0], expected)
        cursor.execute(
            'SELECT count(*) FROM platform."AgentNodeExecution" WHERE "executionStatus" = \'RUNNING\''
        )
        report.expect("node executions still marked running", cursor.fetchone()[0], 0)
    report.expect(
        "the record of stopped runs is used up",
        (data / "run" / "stopped-runs.json").exists(),
        False,
    )
    report.expect("a lock from before the restart", valkey(data, ports).exists(STALE_LOCK), 0)


def memory_table(runtime: psutil.Process) -> float:
    """Print what each supervised process and its children hold; return the
    total in MB, without the skills-catalog publisher (a one-shot that is not
    part of the idle app). USS is memory unique to a process, the honest number on
    Windows and Linux; on macOS it under-reports, and the physical footprint
    (what Activity Monitor shows) is read from `top` instead. "Committed" is
    what the process has asked for, which paging cannot hide."""
    footprints = macos_footprints()
    megabyte = 1024 * 1024
    rows: dict[str, list[float]] = {}
    for child in [runtime, *runtime.children()]:
        members = [child] if child == runtime else [child, *child.children(recursive=True)]
        row = rows.setdefault(label(child, runtime), [0, 0, 0, 0])
        for member in members:
            rss, own, committed = usage(member, footprints)
            row[0] += 1
            row[1] += rss / megabyte
            row[2] += own / megabyte
            row[3] += committed / megabyte
    print(f"  {'process':18} {'count':>5} {'RSS MB':>8} {'own MB':>8} {'committed MB':>13}")
    total = [0.0, 0.0, 0.0, 0.0]
    for name, row in sorted(rows.items(), key=lambda item: -item[1][2]):
        counted = name != CATALOG
        note = "" if counted else "  (running now; not in the total)"
        print(f"  {name:18} {row[0]:5.0f} {row[1]:8.0f} {row[2]:8.0f} {row[3]:13.0f}{note}")
        if counted:
            total = [sum(pair) for pair in zip(total, row, strict=True)]
    print(f"  {'TOTAL':18} {total[0]:5.0f} {total[1]:8.0f} {total[2]:8.0f} {total[3]:13.0f}")
    machine = psutil.virtual_memory()
    print(
        f"  machine: {machine.total / megabyte / 1024:.0f} GB, "
        f"{machine.available / megabyte / 1024:.1f} GB available, {psutil.cpu_count()} cores"
    )
    return total[2]


def label(process: psutil.Process, runtime: psutil.Process) -> str:
    if process == runtime:
        return "(runtime)"
    try:
        command = process.cmdline()
        name = process.name().lower()
    except psutil.Error:
        return "?"
    if "--name" in command:
        return command[command.index("--name") + 1]
    if "publish_skills_catalog" in " ".join(command):
        return CATALOG
    if any(part.endswith("server.js") for part in command):
        return "frontend"
    for marker in ("postgres", "valkey", "epmd", "erl", "beam", "cmd", "sh"):
        if name.startswith(marker):
            return "rabbitmq" if marker in ("erl", "beam", "cmd", "sh") else marker
    return name


def usage(process: psutil.Process, footprints: dict[int, int]) -> tuple[int, int, int]:
    """(RSS, memory of its own, committed) in bytes."""
    try:
        info = process.memory_info()
        committed = getattr(info, "private", getattr(info, "vms", 0))
        if process.pid in footprints:
            return info.rss, footprints[process.pid], committed
        return info.rss, getattr(process.memory_full_info(), "uss", info.rss), committed
    except psutil.Error:
        return 0, 0, 0


def macos_footprints() -> dict[int, int]:
    if sys.platform != "darwin":
        return {}
    output = subprocess.run(
        ["top", "-l", "1", "-stats", "pid,mem"], capture_output=True, text=True, check=False
    ).stdout
    units = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3}
    footprints = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            value = parts[1].rstrip("+-")
            if value and value[-1] in units:
                footprints[int(parts[0])] = int(float(value[:-1]) * units[value[-1]])
    return footprints


def wait_for(done: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(1)
    return done()


def restart(url: str, data: Path, install: Install) -> list[str]:
    report = Report()
    report.expect("the app's address after a restart", url, install.address)
    owner = install.owner = Browser(url)
    report.expect("the owner signs in", owner.sign_in(OWNER_EMAIL, OWNER_PASSWORD)[0], 200)
    report.expect("the owner's role after a restart", owner.role(), "admin")
    report.expect("an admin route with the owner's token", owner.admin_route(), 200)
    settled_runs(data, install, report, owner)

    status, body = Browser(url).sign_up("second@smoke.test", "password-of-a-second-person")
    message = str(body.get("message"))
    report.expect("a sign-up is refused by the frontend's own gate", status, 403)
    # What the sign-up page recognises (frontend/src/app/api/auth/utils.ts).
    report.expect("...with its message", "not allowed" in message.lower(), True, shown=message)
    with contextlib.closing(database(data)) as connection:
        report.expect("accounts after the refused sign-up", count_accounts(connection), 1)
        # For the next start: an install from before the owner step has an
        # account and no admin, and the owner has forgotten the password.
        connection.cursor().execute('UPDATE platform."UserAuthIdentity" SET role = NULL')
    write_password_reset(data, NEW_OWNER_PASSWORD)
    return report.failures


def restart_with_a_password_reset(url: str, data: Path, install: Install) -> list[str]:
    report = Report()
    report.expect("the app's address after a second restart", url, install.address)
    report.expect(
        "the password file is gone", (data / "config" / "reset-password").exists(), False
    )
    if install.owner:
        # The reset deletes the owner's sessions. A browser that holds one
        # keeps working from its cookie until the frontend's five-minute
        # cache of it runs out (cookieCache in auth.ts); the README says so.
        stale = install.owner
        report.note("a session from before the reset, from its cookie alone", stale.role())
        report.expect("...is gone when looked up in the database", stale.role(cached=False), None)
    stale = Browser(url).sign_in(OWNER_EMAIL, OWNER_PASSWORD)[0]
    report.expect("the old password is refused", stale, 401)
    owner = Browser(url)
    report.expect("the new password works", owner.sign_in(OWNER_EMAIL, NEW_OWNER_PASSWORD)[0], 200)
    report.expect("the oldest account was made admin again", owner.role(), "admin")
    report.expect("an admin route with the owner's token", owner.admin_route(), 200)
    return report.failures


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def expect(self, what: str, actual: object, expected: object, shown: object = None) -> None:
        print(f"  {what} -> {actual if shown is None else shown}")
        if actual != expected:
            self.failures.append(f"{what}: got {actual!r}, expected {expected!r}")

    def note(self, what: str, observed: object) -> None:
        """Printed for the record; not a pass or a fail."""
        print(f"  {what} -> {observed} (not checked)")


class Browser:
    """What a signed-in window is to the app: a cookie jar, and requests that
    name the app as their origin (Better Auth refuses cookie-bearing POSTs
    from anywhere else)."""

    def __init__(self, url: str, origin: str | None = None) -> None:
        self.url = url
        self.origin = origin or url
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def sign_up(self, email: str, password: str) -> tuple[int, dict]:
        account = {"name": "Smoke Test", "email": email, "password": password}
        return self.post("/api/auth/sign-up/email", account)

    def sign_in(self, email: str, password: str) -> tuple[int, dict]:
        return self.post("/api/auth/sign-in/email", {"email": email, "password": password})

    def role(self, cached: bool = True) -> object:
        query = "" if cached else "?disableCookieCache=true"
        _, session = self.get(f"/api/auth/get-session{query}")
        user = session.get("user")
        return user.get("role") if isinstance(user, dict) else None

    def admin_route(self) -> int:
        """The backend only believes the token the frontend mints."""
        _, minted = self.get("/api/auth/token")
        return self.get(ADMIN_ROUTE, token=str(minted.get("token")))[0]

    def api(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        """The platform's REST API, as the signed-in window calls it."""
        _, minted = self.get("/api/auth/token")
        return self.send(f"/_agpt/api{path}", body, str(minted.get("token")))

    def get(self, path: str, token: str | None = None) -> tuple[int, dict]:
        return self.send(path, None, token)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        return self.send(path, body, None)

    def send(self, path: str, body: dict | None, token: str | None) -> tuple[int, dict]:
        headers = {"Origin": self.origin, "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=payload, headers=headers)
        try:
            with self.opener.open(request, timeout=60) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            answer = json.loads(raw)
        except ValueError:
            answer = None
        return status, answer if isinstance(answer, dict) else {}


def secrets(data: Path) -> dict[str, str]:
    """What the runtime generated for this data directory."""
    lines = (data / "config" / "runtime.env").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def database(data: Path):
    """A connection as postgres, the way the runtime's own bootstrap makes
    one: from the port and the generated password in the data directory."""
    connection = psycopg2.connect(
        host="127.0.0.1",
        port=json.loads((data / "config" / "ports.json").read_text(encoding="utf-8"))["postgres"],
        user="postgres",
        password=secrets(data)["POSTGRES_PASSWORD"],
        dbname="postgres",
        connect_timeout=10,
    )
    connection.autocommit = True
    return connection


def valkey(data: Path, ports: dict[str, int]):
    import redis

    return redis.Redis(
        host="127.0.0.1",
        port=ports["valkey"],
        password=secrets(data)["REDIS_PASSWORD"],
        socket_timeout=5,
        socket_connect_timeout=3,
    )


def setting(connection, name: str) -> str:
    cursor = connection.cursor()
    cursor.execute("SELECT current_setting(%s)", (name,))
    return cursor.fetchone()[0]


def connections(connection) -> int:
    cursor = connection.cursor()
    cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'")
    return cursor.fetchone()[0]


def count_accounts(connection) -> int:
    cursor = connection.cursor()
    cursor.execute('SELECT count(*) FROM platform."UserAuthIdentity"')
    return cursor.fetchone()[0]


def insert_as_postgres(connection) -> object:
    """Upstream migrations insert accounts as postgres. The owner trigger
    must let them through, as ordinary users; one that raised would leave the
    migration unfinished and the app unable to start. Rolled back."""
    cursor = connection.cursor()
    cursor.execute("BEGIN")
    try:
        cursor.execute(
            'INSERT INTO platform."UserAuthIdentity" '
            '(id, name, email, "emailVerified", "createdAt", "updatedAt") '
            "VALUES ('smoke-migrated', 'Migrated', 'migrated@smoke.test', true, now(), now()) "
            "RETURNING role"
        )
        return cursor.fetchone()[0]
    except psycopg2.Error as exc:
        return f"refused: {exc}"
    finally:
        cursor.execute("ROLLBACK")


def write_password_reset(data: Path, password: str) -> None:
    """What the shell's "Reset owner password" does (src/owner.js)."""
    path = data / "config" / "reset-password"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(password + "\n")


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
