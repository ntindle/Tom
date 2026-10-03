"""Start the stack in dependency order, keep it running, stop it in reverse.

This is the appliance's entrypoint.sh + bootstrap.sh + supervisord, in one
process:

    config -> postgres -> valkey -> rabbitmq -> migrations -> backend services
           -> frontend -> proxy -> ready

Stop order is the reverse, and like supervisord's two tiers the stateless
services go first so the data stores get a quiet, clean shutdown.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
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
    wait_until,
)
from autogpt_desktop.proxy import ProxyThread, Upstreams

logger = logging.getLogger("autogpt_desktop")

MAX_RESTARTS = 3
RESTART_WINDOW_SECONDS = 300
APP_READY_TIMEOUT_SECONDS = 300


class StartupError(RuntimeError):
    pass


class Stack:
    def __init__(self, bundle: Bundle, data: DataDir) -> None:
        self.bundle = bundle
        self.data = data
        self.processes: list[ManagedProcess] = []
        self.proxy: ProxyThread | None = None
        self.registry = ChildRegistry(data.run / "children.json")
        self.restarts: dict[str, list[float]] = {}
        self.stop_requested = threading.Event()
        self.env: dict[str, str] = {}
        self.oneshot: ManagedProcess | None = None

    def run(self) -> int:
        try:
            url = self.start()
        except Exception as exc:
            cancelled = self.stop_requested.is_set()
            if not cancelled:
                logger.exception("startup failed")
                events.error(str(exc), fatal=True)
            self.stop()
            return 0 if cancelled else 1
        events.ready(url)
        self.publish_skills_catalog()
        code = self.watch()
        self.stop()
        return code

    def start(self) -> str:
        bundle, data = self.bundle, self.data
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
        postgres.initialize(bundle, data, secret["POSTGRES_PASSWORD"])
        valkey.write_config(
            data, port["valkey"], port["valkey_bus"], secret["REDIS_PASSWORD"]
        )
        rabbitmq.prepare(data, port["rabbitmq"], rabbit_user, rabbit_password)

        database = postgres.process(bundle, data, port["postgres"])
        cache = valkey.process(bundle, data, port["valkey"], secret["REDIS_PASSWORD"])
        queue = rabbitmq.process(bundle, data, port)
        for process in (database, cache, queue):
            self.launch(process)

        self.require(
            postgres.wait_ready(port["postgres"], secret["POSTGRES_PASSWORD"]), database
        )
        self.require(valkey.wait_ready(port["valkey"], secret["REDIS_PASSWORD"]), cache)
        valkey.ensure_cluster(port["valkey"], secret["REDIS_PASSWORD"])
        self.require(
            rabbitmq.wait_ready(port["rabbitmq"], rabbit_user, rabbit_password), queue
        )

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
        bootstrap.apply_migrations(self.bundle, self.env)
        bootstrap.configure_frontend_role(
            self.bundle, connect, secret["AUTOGPT_FRONTEND_DB_PASSWORD"]
        )

    def start_apps(self, port: dict[str, int], secret: dict[str, str]) -> None:
        bundle, data = self.bundle, self.data
        events.progress("services", "Starting AutoGPT")
        for service in apps.backend_processes(bundle, data, self.env):
            self.launch(service)
        frontend_env = settings.frontend_environment(self.env, port, secret, data)
        self.launch(apps.frontend_process(bundle, data, frontend_env))

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
        self.registry.record(self.processes)

    def launch(self, process: ManagedProcess) -> None:
        process.start()
        self.processes.append(process)
        self.registry.record(self.processes)

    def require(self, became_ready: bool, process: ManagedProcess) -> None:
        if self.stop_requested.is_set():
            raise StartupError("startup was cancelled")
        if became_ready:
            return
        log = self.data.logs / f"{process.name}.log"
        raise StartupError(
            f"{process.name} did not start. See {log} for details."
        )

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
        if self.stop_requested.is_set():
            raise StartupError("startup was cancelled")

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
            env={**apps.base_env(), **self.env},
            cwd=self.bundle.backend_dir,
            log_dir=self.data.logs,
        )
        try:
            process.start()
        except OSError as exc:
            logger.warning(f"could not start the skills catalog publish: {exc}")
            return
        self.oneshot = process

    def watch(self) -> int:
        while not self.stop_requested.wait(2):
            for index, process in enumerate(self.processes):
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
                self.processes[index] = process
                self.registry.record(self.processes)
        return 0

    def may_restart(self, name: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self.restarts.get(name, []) if now - t < RESTART_WINDOW_SECONDS]
        recent.append(now)
        self.restarts[name] = recent
        return len(recent) <= MAX_RESTARTS

    def stop(self) -> None:
        self.stop_requested.set()
        if self.oneshot:
            self.oneshot.stop()
        if self.proxy:
            try:
                self.proxy.stop()
            except Exception as exc:
                logger.warning(f"proxy did not stop cleanly: {exc}")
        for process in reversed(self.processes):
            process.stop()
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
        stack.stop_requested.set()

    threading.Thread(target=wait_for_eof, name="stdin-watch", daemon=True).start()


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError):
        return False
