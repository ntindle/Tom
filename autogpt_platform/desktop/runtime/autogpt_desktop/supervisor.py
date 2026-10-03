"""Start the stack in dependency order, keep it running, stop it in reverse.

This is the appliance's entrypoint.sh + bootstrap.sh + supervisord, in one
process:

    config -> epmd -> postgres | valkey | rabbitmq -> migrations
           -> backend services | frontend -> proxy -> ready

Processes start in tiers; those in a tier do not depend on each other. Tiers
stop in reverse order, each one all at once. Like supervisord's two stop
groups, that takes the stateless services away first so the data stores get
a quiet, clean shutdown.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import NoReturn

from autogpt_desktop import (
    apps,
    bootstrap,
    events,
    ports,
    postgres,
    rabbitmq,
    settings,
    valkey,
)
from autogpt_desktop.layout import Bundle, DataDir
from autogpt_desktop.process import (
    ChildRegistry,
    ManagedProcess,
    adopt_kill_on_exit_job,
    base_env,
    stop_together,
    wait_until,
)
from autogpt_desktop.proxy import ProxyThread, Upstreams

logger = logging.getLogger("autogpt_desktop")

MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 300
APP_READY_TIMEOUT_SECONDS = 300
# What the shell allows a stop before it kills the runtime (src/runtime.js).
SHELL_STOP_GRACE_SECONDS = 60
PASSWORD_RESET_LOST = (
    "The owner password was not changed, because AutoGPT did not finish "
    "starting. Reset it again."
)


class StartupError(RuntimeError):
    pass


class Stack:
    def __init__(self, bundle: Bundle, data: DataDir) -> None:
        self.bundle = bundle
        self.data = data
        self.tiers: list[list[ManagedProcess]] = []
        self.proxy: ProxyThread | None = None
        self.registry = ChildRegistry(data.run / "children.json")
        self.restarts: dict[str, list[float]] = {}
        self.stop_requested = threading.Event()
        self.env: dict[str, str] = {}
        self.oneshot: ManagedProcess | None = None
        self.migrating = False
        self.password_reset: str | None = None

    @property
    def processes(self) -> list[ManagedProcess]:
        return [process for tier in self.tiers for process in tier]

    def run(self) -> int:
        try:
            return self.run_until_stopped()
        finally:  # whatever happened, nothing started here is left running
            self.stop()

    def run_until_stopped(self) -> int:
        try:
            url = self.start()
        except Exception as exc:
            cancelled = self.stop_requested.is_set()
            lost = self.forget_password_reset()
            if not cancelled:
                logger.exception("startup failed")
                events.error(f"{exc} {lost}".strip(), fatal=True)
            return 0 if cancelled else 1
        events.ready(url)
        self.publish_skills_catalog()
        return self.watch()

    def request_stop(self) -> None:
        """The shell asked for a stop. It kills a runtime that takes too long
        over one, so say so when this stop has a good reason to be slow."""
        self.stop_requested.set()
        if self.migrating:
            events.stopping(
                "Finishing a database update before closing",
                bootstrap.MIGRATION_TIMEOUT_SECONDS + SHELL_STOP_GRACE_SECONDS,
            )

    def forget_password_reset(self) -> str:
        """The password file is deleted as soon as it is read, so a start that
        ends before the reset is applied has lost it. Say so: the owner, who
        does not know the old password, would otherwise find the new one
        refused with no hint why."""
        if self.password_reset is None:
            return ""
        self.password_reset = None
        logger.warning(PASSWORD_RESET_LOST)
        return PASSWORD_RESET_LOST

    def raise_if_cancelled(self) -> None:
        if self.stop_requested.is_set():
            raise StartupError("startup was cancelled")

    def start(self) -> str:
        bundle, data = self.bundle, self.data
        # First, ahead of anything that can fail: whatever happens to this
        # start, the password does not stay on disk in the clear.
        self.password_reset = bootstrap.take_password_reset(
            data.config / bootstrap.RESET_PASSWORD_FILE
        )
        events.progress("config", "Preparing configuration")
        data.prepare()
        self.registry.reap_leftovers()
        secret = settings.ensure_secrets(bundle, data)
        port = ports.allocate(data.ports_file)
        self.env = settings.backend_environment(
            bundle, data, port, secret, settings.read_user_settings(data)
        )
        first_boot = not postgres.is_initialized(data)
        self.start_infrastructure(port, secret, first_boot)
        self.migrate(port, secret, first_boot)
        self.start_apps(port, secret)
        return self.env["AUTOGPT_PUBLIC_URL"]

    def start_infrastructure(
        self, port: dict[str, int], secret: dict[str, str], first_boot: bool
    ) -> None:
        """PostgreSQL, Valkey and RabbitMQ do not depend on each other, so
        they boot side by side; RabbitMQ is the slowest and sets the pace."""
        bundle, data = self.bundle, self.data
        events.progress(
            "infrastructure",
            "Setting up the database (first start only)"
            if first_boot
            else "Starting the database and message queue",
        )
        rabbit_user = secret["RABBITMQ_DEFAULT_USER"]
        rabbit_password = secret["RABBITMQ_DEFAULT_PASS"]
        postgres.check_compatible(bundle, data)
        postgres.initialize(bundle, data, secret["POSTGRES_PASSWORD"])
        valkey.write_config(
            data, port["valkey"], port["valkey_bus"], secret["REDIS_PASSWORD"]
        )
        rabbitmq.prepare(data, port["rabbitmq"], rabbit_user, rabbit_password)

        port_mapper = rabbitmq.epmd_process(bundle, data, port)
        self.launch([port_mapper])
        self.await_ready(
            port_mapper, lambda: rabbitmq.epmd_is_ready(port["epmd"]), timeout=30
        )

        database = postgres.process(bundle, data, port["postgres"])
        cache = valkey.process(bundle, data, port["valkey"], secret["REDIS_PASSWORD"])
        queue = rabbitmq.process(bundle, data, port)
        self.launch([database, cache, queue])

        self.await_ready(
            database,
            lambda: postgres.is_ready(port["postgres"], secret["POSTGRES_PASSWORD"]),
            timeout=120,
        )
        self.await_cache(cache, port, secret["REDIS_PASSWORD"])
        valkey.ensure_cluster(port["valkey"], secret["REDIS_PASSWORD"])
        self.await_ready(
            queue,
            lambda: rabbitmq.is_ready(port["rabbitmq"], rabbit_user, rabbit_password),
            timeout=240,
        )

    def await_cache(self, cache: ManagedProcess, port: dict[str, int], password: str) -> None:
        """Valkey exits at once when it cannot read its files (another build
        wrote them, or they are damaged). They are expendable; start empty."""

        def answers() -> bool:
            return valkey.is_ready(port["valkey"], password)

        try:
            self.await_ready(cache, answers, timeout=60)
        except StartupError:
            if cache.exit_code() is None or self.stop_requested.is_set():
                raise
            valkey.set_aside(self.data)
            valkey.write_config(self.data, port["valkey"], port["valkey_bus"], password)
            cache.start()
            self.record()
            self.await_ready(cache, answers, timeout=60)

    def migrate(
        self, port: dict[str, int], secret: dict[str, str], first_boot: bool
    ) -> None:
        events.progress(
            "migrate",
            "Creating the database tables (first start only)"
            if first_boot
            else "Checking for database updates",
        )
        connect = self.database_connector(port["postgres"], secret["POSTGRES_PASSWORD"])
        bootstrap.create_schemas(self.bundle, connect)
        bootstrap.refuse_interrupted_migration(connect)
        # A migration that is cut short leaves a database that needs repair by
        # hand. Once it starts it runs to the end, and a stop request waits.
        self.migrating = True
        try:
            self.raise_if_cancelled()
            bootstrap.remove_owner_trigger(connect)
            bootstrap.apply_migrations(self.bundle, self.env)
        finally:
            self.migrating = False
        bootstrap.configure_frontend_role(
            self.bundle, connect, secret["AUTOGPT_FRONTEND_DB_PASSWORD"]
        )
        self.secure_owner(connect)
        postgres.first_run_completed(self.data)

    def secure_owner(self, connect) -> None:
        """The first account is the owner; once it exists, registration is
        closed unless settings.env reopens it. `start_apps` hands the frontend
        its environment after this, so the gate can still be set here."""
        configured = self.env.get("AUTH_ALLOW_NEW_ACCOUNTS")
        identities = bootstrap.ensure_owner(
            connect, settings.closes_registration(configured)
        )
        self.env["AUTH_ALLOW_NEW_ACCOUNTS"] = settings.registration_gate(
            configured, identities
        )
        password, self.password_reset = self.password_reset, None
        if password:
            bootstrap.reset_owner_password(connect, password)

    def start_apps(self, port: dict[str, int], secret: dict[str, str]) -> None:
        bundle, data = self.bundle, self.data
        self.raise_if_cancelled()
        events.progress("services", "Starting AutoGPT")
        frontend_env = settings.frontend_environment(self.env, port, secret, data)
        self.launch(
            [
                *apps.backend_processes(bundle, data, self.env),
                apps.frontend_process(bundle, data, frontend_env),
            ]
        )

        self.proxy = ProxyThread(
            Upstreams(
                public_url=self.env["AUTOGPT_PUBLIC_URL"],
                rest=f"http://127.0.0.1:{port['agent_api']}",
                websocket=f"http://127.0.0.1:{port['websocket']}",
                frontend=f"http://127.0.0.1:{port['frontend']}",
            ),
            port["public"],
        )
        self.proxy.start()
        self.wait_for_apps(port)
        self.record()

    def launch(self, tier: list[ManagedProcess]) -> None:
        started: list[ManagedProcess] = []
        self.tiers.append(started)
        for process in tier:
            process.start()
            started.append(process)
            self.record()

    def record(self) -> None:
        oneshot = [self.oneshot] if self.oneshot else []
        self.registry.record([*self.processes, *oneshot])

    def await_ready(
        self, process: ManagedProcess, probe: Callable[[], bool], timeout: float
    ) -> None:
        """Wait for a service to answer, but not past the point of knowing it
        never will: a process that has exited, or a stop request, ends the
        wait at once instead of running out the timeout."""

        def settled() -> bool:
            if self.stop_requested.is_set() or process.exit_code() is not None:
                return True
            return probe()

        wait_until(settled, timeout)
        self.raise_if_cancelled()
        log = self.data.logs / f"{process.name}.log"
        if process.exit_code() is not None:
            raise StartupError(f"{process.name} exited while starting. See {log} for details.")
        if not probe():
            raise StartupError(f"{process.name} did not start. See {log} for details.")

    def wait_for_apps(self, port: dict[str, int]) -> None:
        probes = {
            "rest": f"http://127.0.0.1:{port['agent_api']}/health",
            "websocket": f"http://127.0.0.1:{port['websocket']}/health",
            "frontend": f"http://127.0.0.1:{port['frontend']}/",
        }
        by_name = {process.name: process for process in self.processes}

        def all_healthy() -> bool:
            if self.stop_requested.is_set():
                return True
            for name in probes:
                if by_name[name].exit_code() is not None:
                    raise StartupError(
                        f"{name} exited while starting. "
                        f"See {self.data.logs / (name + '.log')} for details."
                    )
            return all(_http_ok(url) for url in probes.values())

        if not wait_until(all_healthy, APP_READY_TIMEOUT_SECONDS, interval=1):
            raise StartupError("AutoGPT did not become ready in time")
        self.raise_if_cancelled()

    def database_connector(self, port: int, password: str):
        import psycopg2

        def connect():
            return psycopg2.connect(
                host="127.0.0.1",
                port=port,
                user="postgres",
                password=password,
                dbname="postgres",
                connect_timeout=10,
            )

        return connect

    def publish_skills_catalog(self) -> None:
        """Best-effort, in the background: the appliance blocks boot on this
        for up to ten minutes, which a desktop user would read as a hang."""
        process = ManagedProcess(
            name="skills-catalog",
            argv=apps.entry_point_argv(
                self.bundle, apps.SKILLS_CATALOG_ENTRY, "--skip-missing-preloads"
            ),
            env={**base_env(), **self.env},
            cwd=self.bundle.backend_dir,
            log_dir=self.data.logs,
        )
        try:
            process.start()
        except OSError as exc:
            logger.warning(f"could not start the skills catalog publish: {exc}")
            return
        self.oneshot = process
        self.record()

    def watch(self) -> int:
        while not self.stop_requested.wait(2):
            for process in self.processes:
                code = process.exit_code()
                if code is None:
                    continue
                if not self.may_restart(process.name):
                    events.error(
                        f"{process.name} keeps stopping (exit code {code}). "
                        f"See {self.data.logs / (process.name + '.log')}.",
                        fatal=True,
                    )
                    return 1
                logger.warning(f"{process.name} exited with code {code}; restarting it")
                process.start()
                self.record()
        return 0

    def may_restart(self, name: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self.restarts.get(name, []) if now - t < RESTART_WINDOW_SECONDS]
        recent.append(now)
        self.restarts[name] = recent
        return len(recent) <= MAX_RESTARTS

    def stop(self) -> None:
        self.stop_requested.set()
        if self.proxy:
            try:
                self.proxy.stop()
            except Exception as exc:
                logger.warning(f"proxy did not stop cleanly: {exc}")
        oneshot = [self.oneshot] if self.oneshot else []
        for tier in reversed([*self.tiers, oneshot]):
            stop_together(tier)
        self.registry.path.unlink(missing_ok=True)


def serve() -> NoReturn:
    events.configure_logging()
    adopt_kill_on_exit_job()
    stack = Stack(Bundle.locate(), DataDir.locate())
    _stop_on_signals(stack)
    _stop_when_stdin_closes(stack)
    code = stack.run()
    # Skip interpreter finalization: every service is already stopped, and
    # library threads (aiohttp, pika, the stdin watcher) must not be able to
    # turn a clean shutdown into a hang or a crash report.
    with contextlib.suppress(OSError, ValueError):  # the shell may be gone
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(code)


def _stop_on_signals(stack: Stack) -> None:
    def handler(signum, frame):
        stack.stop_requested.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _stop_when_stdin_closes(stack: Stack) -> None:
    """The shell closes our stdin to ask for shutdown, and the OS closes it
    for us if the shell dies. A terminal run (stdin is a TTY) uses Ctrl+C."""
    if sys.stdin is None or sys.stdin.isatty():
        return

    descriptor = sys.stdin.fileno()

    def wait_for_eof() -> None:
        # Read the raw descriptor, not sys.stdin: a thread parked inside the
        # buffered reader holds its lock, and CPython aborts at exit when it
        # cannot take that lock back from a daemon thread.
        try:
            while os.read(descriptor, 4096):
                pass
        except OSError:
            pass
        stack.request_stop()

    threading.Thread(target=wait_for_eof, name="stdin-watch", daemon=True).start()


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError):
        return False
