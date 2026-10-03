"""Boot an assembled runtime, check that it serves, stop it, check it is gone.

    <runtime>/python/python smoke_test.py <runtime> [--timeout 600] [--quick]

Run it with the bundle's own interpreter (it uses psutil and psycopg2 from
the bundle). This is the shell's contract exercised without the shell: start
the runtime as the bundle's manifest says to, read JSON events from stdout
until `ready`, close stdin, expect exit code 0, no process from the bundle
left running, and no file in the bundle changed.

It starts the runtime three times on one data directory, because the owner
account's promises are about restarts:

  1. a fresh install: the first account to sign up is an admin at once, and
     a second sign-up is refused;
  2. a restart: the owner is still an admin, and the frontend now refuses
     sign-ups with its own "not allowed" message;
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


class Install:
    """What one start leaves for the next ones to check against."""

    def __init__(self) -> None:
        self.address: str | None = None
        self.owner: Browser | None = None


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
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    data = args.data_dir or Path(tempfile.mkdtemp(prefix="autogpt-smoke-"))
    bundle_before = snapshot(runtime)
    starts: list[Checks] = [fresh_install]
    if not args.quick:
        starts += [restart, restart_with_a_password_reset]

    started = time.monotonic()
    failures: list[str] = []
    install = Install()
    for number, checks in enumerate(starts, start=1):
        print(f"\n== start {number} of {len(starts)}: {checks.__name__.replace('_', ' ')}")
        failures += run(runtime, data, args.timeout, checks, install)
        if failures:
            break
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
    url = wait_for_ready(process, timeout, started)
    failures = [] if url else ["the runtime never reported ready"]
    if url:
        failures += checked(checks, url, data, install)
    failures += stop(process)

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
    process.stdin.close()
    try:
        code = process.wait(STOP_BUDGET_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        return [f"the runtime did not stop within {STOP_BUDGET_SECONDS}s of stdin closing"]
    return [f"the runtime exited with code {code}"] if code != 0 else []


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
    return report.failures


def restart(url: str, data: Path, install: Install) -> list[str]:
    report = Report()
    report.expect("the app's address after a restart", url, install.address)
    owner = install.owner = Browser(url)
    report.expect("the owner signs in", owner.sign_in(OWNER_EMAIL, OWNER_PASSWORD)[0], 200)
    report.expect("the owner's role after a restart", owner.role(), "admin")
    report.expect("an admin route with the owner's token", owner.admin_route(), 200)

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


def database(data: Path):
    """A connection as postgres, the way the runtime's own bootstrap makes
    one: from the port and the generated password in the data directory."""
    config = data / "config"
    secrets = dict(
        line.split("=", 1)
        for line in (config / "runtime.env").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )
    connection = psycopg2.connect(
        host="127.0.0.1",
        port=json.loads((config / "ports.json").read_text(encoding="utf-8"))["postgres"],
        user="postgres",
        password=secrets["POSTGRES_PASSWORD"],
        dbname="postgres",
        connect_timeout=10,
    )
    connection.autocommit = True
    return connection


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
