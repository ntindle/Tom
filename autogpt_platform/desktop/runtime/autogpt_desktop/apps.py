"""AutoGPT's own processes: the backend services and the Next server.

The same services the appliance's supervisord runs
(supervisor/supervisord.conf) minus the opt-in chat-bot ones. Each is
upstream's own entry point, called through the bundled interpreter rather
than the console scripts pip generates: those carry absolute shebangs that
break as soon as the app bundle is moved.

Every service process is a service host (servicehost.py), whether it hosts
one service or several. By default they are grouped, because each
interpreter that imports the backend costs about 600 MB:

    database-manager   alone: it is the others' way to the database
    workers            the five services that never connect to the database
    api                the two API servers, on one event loop

`isolated()` is one host per service: what the `isolated` profile runs, and
what a group is replaced with when upstream stops fitting it.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from autogpt_desktop import servicehost
from autogpt_desktop.layout import Bundle, DataDir
from autogpt_desktop.process import ManagedProcess, StopEvent, base_env
from autogpt_desktop.runs import Cache


@dataclass(frozen=True)
class Service:
    name: str
    entry: str
    # Where it answers once it is up: its name in ports.PORT_NAMES, and a
    # path. Services built on the backend's AppService have /health_check;
    # the two executors serve only their metrics.
    port: str
    health: str
    # Connects to the database itself (Prisma) instead of through
    # database-manager. The backend decides per process which of the two a
    # query takes, so such a service cannot share one with the others.
    database: bool = False


SERVICES = (
    Service("database-manager", "backend.db:main", "database_api", "/health_check", database=True),
    Service("scheduler", "backend.scheduler:main", "execution_scheduler", "/health_check"),
    Service("batch-executor", "backend.batch_executor:main", "batch_executor", "/health_check"),
    Service("notification", "backend.notification:main", "notification", "/health_check"),
    Service("executor", "backend.exec:main", "execution_manager", "/metrics"),
    Service(
        "copilot-executor",
        "backend.copilot.executor.__main__:main",
        "copilot_executor",
        "/metrics",
    ),
    Service("websocket", "backend.ws:main", "websocket", "/health", database=True),
    Service("rest", "backend.rest:main", "agent_api", "/health", database=True),
)
DATABASE_MANAGER = "database-manager"
FRONTEND = "frontend"
# The two that call `uvicorn.run` themselves (the others serve through
# AppService, each on a loop of its own).
API_SERVERS = ("websocket", "rest")

# What the window talks to, and their way to the database. The app is ready
# when these answer; the rest are waited for only so long (supervisor.py).
NEEDED_TO_OPEN = frozenset({DATABASE_MANAGER, *API_SERVERS, FRONTEND})

SKILLS_CATALOG_ENTRY = "backend.cli.publish_skills_catalog:main"

# How long a service gets between being asked to stop and being killed. The
# appliance measured this (single-container/supervisor/supervisord.conf): a
# service finishes its own cleanup well inside a second, then sits in
# third-party telemetry teardown for several more, so waiting longer buys
# nothing. The host exits by itself once cleanup is done, just inside it.
STOP_TIMEOUT_SECONDS = 3
HOST_STOP_BUDGET_SECONDS = STOP_TIMEOUT_SECONDS - 0.5

# The package is not on a service's path (its cwd is the backend), and it is
# appended, never put first: its parent directory in the bundle also holds
# directories named prisma, site and backend.
HOST_CODE = (
    "import sys; sys.path.append(sys.argv.pop(1)); "
    "from autogpt_desktop.servicehost import main; main()"
)
PACKAGE_PARENT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Group:
    """Services that share a process, named as the supervisor and the logs
    name it."""

    name: str
    services: tuple[str, ...]

    @property
    def merged(self) -> bool:
        return len(self.services) > 1


# The executor first: it loads every block module as it starts, and the
# host starts the next service once that has finished.
MERGED = (
    Group("database-manager", ("database-manager",)),
    Group(
        "workers",
        ("executor", "copilot-executor", "scheduler", "batch-executor", "notification"),
    ),
    Group("api", API_SERVERS),
)


def layout(merged: bool) -> tuple[Group, ...]:
    return MERGED if merged else isolated(MERGED)


def isolated(groups: tuple[Group, ...]) -> tuple[Group, ...]:
    """A host for each service of `groups`, in the appliance's start order."""
    members = {name for group in groups for name in group.services}
    return tuple(Group(s.name, (s.name,)) for s in SERVICES if s.name in members)


def service(name: str) -> Service:
    return next(service for service in SERVICES if service.name == name)


def health_urls(port: dict[str, int]) -> dict[str, str]:
    """What answers once the app is up, by service."""
    urls = {s.name: f"http://127.0.0.1:{port[s.port]}{s.health}" for s in SERVICES}
    urls[FRONTEND] = f"http://127.0.0.1:{port[FRONTEND]}/"
    return urls


def health_port(name: str) -> str:
    """The name in ports.PORT_NAMES of the port `name` answers on."""
    return FRONTEND if name == FRONTEND else service(name).port


def backend_processes(
    bundle: Bundle,
    data: DataDir,
    env: dict[str, str],
    groups: tuple[Group, ...],
    cache: Cache,
) -> list[ManagedProcess]:
    return [host_process(bundle, data, env, group, cache) for group in groups]


def host_process(
    bundle: Bundle, data: DataDir, env: dict[str, str], group: Group, cache: Cache
) -> ManagedProcess:
    stop_event = StopEvent.create(group.name)
    env = {**base_env(), **env}
    if stop_event:
        env[servicehost.STOP_EVENT_ENV] = stop_event.name
    return ManagedProcess(
        name=group.name,
        argv=host_argv(bundle, group),
        env=env,
        cwd=bundle.backend_dir,
        log_dir=data.logs,
        # Elsewhere the default, SIGTERM to the host alone, is the request.
        graceful_stop=(lambda _: stop_event.set()) if stop_event else None,
        stop_timeout=STOP_TIMEOUT_SECONDS,
        before_start=_before_start(group, cache, stop_event),
    )


def host_argv(bundle: Bundle, group: Group) -> list[str]:
    members = [service(name) for name in group.services]
    flags = ["--name", group.name, "--stop-budget", f"{HOST_STOP_BUDGET_SECONDS:g}"]
    if any(member.name in API_SERVERS for member in members):
        flags.append("--shared-loop")
    if group.merged and not any(member.database for member in members):
        flags.append("--no-database")
    entries = [f"{member.name}={member.entry}" for member in members]
    return [str(bundle.python), "-c", HOST_CODE, str(PACKAGE_PARENT), *flags, *entries]


def _before_start(group: Group, cache: Cache, stop_event: StopEvent | None) -> Callable[[], None]:
    def prepare() -> None:
        if stop_event:
            stop_event.reset()  # a restart must not find the last stop's request
        cache.clear_stale_locks(group.services)

    return prepare


def frontend_process(bundle: Bundle, data: DataDir, env: dict[str, str]) -> ManagedProcess:
    return ManagedProcess(
        name=FRONTEND,
        argv=[*bundle.node_command(), str(bundle.frontend_server)],
        env={**base_env(), **env, "ELECTRON_RUN_AS_NODE": "1"},
        cwd=bundle.frontend_server.parent,
        log_dir=data.logs,
        stop_timeout=STOP_TIMEOUT_SECONDS,
    )


def entry_point_argv(bundle: Bundle, entry: str, *args: str) -> list[str]:
    """A one-shot tool of the backend's (not a service: no host)."""
    module, _, function = entry.partition(":")
    code = f"import sys; from {module} import {function} as m; sys.exit(m())"
    return [str(bundle.python), "-c", code, *args]


def bundle_version(bundle: Bundle) -> str:
    """Changes whenever the bundled backend does: the bundle carries no
    version number of its own. From names and sizes, not contents: reading
    two thousand files takes ten seconds under a virus scanner."""
    import hashlib

    digest = hashlib.sha256(sys.version.encode())
    backend = bundle.backend_dir
    for path in sorted(backend.rglob("*")):
        if path.suffix in (".py", ".prisma", ".sql"):
            digest.update(f"{path.relative_to(backend).as_posix()}:{path.stat().st_size};".encode())
    return digest.hexdigest()
