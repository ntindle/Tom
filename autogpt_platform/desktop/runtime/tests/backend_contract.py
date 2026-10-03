"""What the service host depends on in the backend, checked against a real one.

    <runtime>/python/python -B backend_contract.py [<runtime dir>]

Run with an interpreter that has the backend's dependencies: the bundle's.
It is a script and not a test because importing the backend configures
logging, Sentry and the multiprocessing start method for the whole
interpreter; it gets one of its own, with the environment the runtime gives
a service. test_backend_contract.py runs it when a bundle is assembled, and
build/smoke_test.py runs it on every OS before it starts anything.

Nothing is started. Each line of output is one dependency: `ok`, or `BROKEN`
with what changed upstream and what to update here. Exit code 1 if any is
broken.
"""

from __future__ import annotations

import ast
import fnmatch
import inspect
import os
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

RUNTIME_PACKAGE_PARENT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = RUNTIME_PACKAGE_PARENT.parent / "build" / "runtime"
# In backend.app.main's list and deliberately not run by the desktop: the
# chat-bot bridge services, as in the appliance.
NOT_RUN = {"PlatformLinkingManager", "CoPilotChatBridge"}
HOST = "autogpt_desktop/servicehost.py"
APPS = "autogpt_desktop/apps.py"
# The backend setting that holds the port of each service that does not serve
# through AppService (those are asked with get_port).
PORT_SETTINGS = {
    "executor": "execution_manager_port",
    "copilot-executor": "copilot_executor_port",
    "websocket": "websocket_server_port",
    "rest": "agent_api_port",
}

findings: list[tuple[bool, str]] = []


def main() -> int:
    bundle_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_BUNDLE
    scratch = tempfile.TemporaryDirectory(prefix="autogpt-contract-", ignore_cleanup_errors=True)
    with scratch as directory:
        services = collect(bundle_root, Path(directory))
        for check in CHECKS if services else ():
            try:
                check(services, bundle_root / "backend" / "backend")
            except Exception as exc:
                what = check.__name__.replace("_", " ")
                expect(False, what, f"The check itself failed: {type(exc).__name__}: {exc}")
    for ok, message in findings:
        print(f"{'ok' if ok else 'BROKEN'}: {message}")
    broken = sum(not ok for ok, _ in findings)
    print(f"{len(findings) - broken} of {len(findings)} dependencies hold")
    return 1 if broken else 0


def expect(ok: bool, holds: str, broken: str) -> None:
    findings.append((bool(ok), holds if ok else f"{holds}. {broken}"))


def collect(bundle_root: Path, scratch: Path) -> dict[str, Any]:
    """The eight services, taken from upstream the way the host takes them:
    with its own collector, in one interpreter."""
    as_a_service_sees_it(bundle_root, scratch)
    from autogpt_desktop import apps, servicehost

    host = servicehost.Host(
        name="contract", entries=[(service.name, service.entry) for service in apps.SERVICES]
    )
    try:
        host.collect()
    except servicehost.ContractError as exc:
        expect(False, "every entry point hands one service to backend.app.run_processes", str(exc))
        return {}
    expect(
        True,
        "every entry point hands one service, with start(background=...) and cleanup(), "
        "to backend.app.run_processes",
        "",
    )
    return {hosted.name: hosted.instance for hosted in host.services}


def as_a_service_sees_it(bundle_root: Path, scratch: Path) -> None:
    """cwd and path as apps.host_argv gives them, environment as
    settings.backend_environment does, over made-up ports and secrets."""
    sys.path.append(str(RUNTIME_PACKAGE_PARENT))
    from autogpt_desktop import ports, resources, settings
    from autogpt_desktop.layout import Bundle, DataDir

    data = DataDir(scratch)
    data.prepare()
    port = {name: 20000 + index for index, name in enumerate(ports.PORT_NAMES)}
    bundle = Bundle(bundle_root)
    secret = settings.ensure_secrets(bundle, data)
    profile = resources.choose({}, ram_bytes=16 * 1024**3, cores=6)
    os.environ.update(
        settings.backend_environment(bundle, data, port, secret, {}, profile.backend_env)
    )
    os.chdir(bundle.backend_dir)
    sys.path.insert(0, "")
    sys.argv = sys.argv[:1]


def service_names_and_classes(services: dict[str, Any], backend: Path) -> None:
    for name, service in services.items():
        label = getattr(service, "service_name", None)
        expect(
            isinstance(label, str) and bool(label),
            f"{name}: service_name is a string ({label!r})",
            f"The host's log names services with it; see Hosted.label in {HOST}.",
        )
    tree = ast.parse((backend / "app.py").read_text(encoding="utf-8"))
    upstream = {
        node.func.id
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "run_processes"
        for node in ast.walk(call)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } - {"run_processes"}
    ours = {type(service).__name__ for service in services.values()}
    expect(
        upstream - NOT_RUN == ours,
        f"backend.app.main runs the services the desktop runs, and {sorted(NOT_RUN)}",
        f"Upstream now runs {sorted(upstream)}; the desktop runs {sorted(ours)}. A new "
        f"service needs an entry in SERVICES and a group in {APPS} (and a port in ports.py); "
        "a removed one has to go from there.",
    )


def nobody_is_connected(services: dict[str, Any], backend: Path) -> None:
    """Building the services must not connect to the database, and only the
    ones marked `database` may ever do so themselves."""
    from autogpt_desktop import apps

    try:
        from backend.data import db

        connected = db.is_connected()
    except Exception as exc:
        expect(False, "backend.data.db.is_connected() exists", f"{exc}. The watchdog in {HOST} uses it.")
        return
    expect(
        connected is False,
        "backend.data.db.is_connected() is False before any service starts",
        f"The watchdog in {HOST} would end the workers host at once.",
    )
    connects = re.compile(r"\b(db|database|prisma)\.connect\(\)")
    for name, service in services.items():
        source = Path(inspect.getsourcefile(type(service)) or "").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        marked = apps.service(name).database
        expect(
            bool(connects.search(code)) == marked,
            f"{name} {'connects' if marked else 'does not connect'} to the database itself",
            f"Its module now says otherwise. Set `database` on its entry in SERVICES in {APPS} "
            "to match: only services that never connect may share the workers host.",
        )


def api_servers_call_uvicorn_run(services: dict[str, Any], backend: Path) -> None:
    from autogpt_desktop import apps

    for name in apps.API_SERVERS:
        source = inspect.getsource(type(services[name]).run)
        expect(
            "uvicorn.run(" in source,
            f"{name} serves by calling uvicorn.run",
            f"That is the call {HOST} replaces to put the API servers on one event loop "
            "(serve_on_one_loop). Intercept what it calls now, or take it out of API_SERVERS "
            f"and the api group in {APPS}.",
        )


def services_answer_where_the_health_table_says(services: dict[str, Any], backend: Path) -> None:
    from autogpt_desktop import apps, ports

    port = {name: 20000 + index for index, name in enumerate(ports.PORT_NAMES)}
    for service in apps.SERVICES:
        instance = services[service.name]
        get_port: Callable[[], int] | None = getattr(type(instance), "get_port", None)
        if service.health == "/health_check":
            expect(
                get_port is not None and get_port() == port[service.port],
                f"{service.name} listens on the {service.port} port and has /health_check",
                f"Update its port or path in SERVICES in {APPS} (and the *_PORT names in "
                "settings._service_addresses).",
            )
    source = (backend / "util" / "service.py").read_text(encoding="utf-8")
    expect('"/health_check"' in source, "AppService serves /health_check", f"Update SERVICES in {APPS}.")
    for name in ("executor", "copilot-executor"):
        run = inspect.getsource(type(services[name]).run)
        expect(
            "start_http_server(" in run,
            f"{name} serves its metrics over HTTP",
            f"/metrics is how the supervisor knows it is up; update SERVICES in {APPS}.",
        )


def the_other_services_listen_on_the_ports_they_are_given(
    services: dict[str, Any], backend: Path
) -> None:
    """The executors' metrics servers and the two API servers: the runtime
    probes each on the port it handed out through settings._service_addresses."""
    from autogpt_desktop import apps, ports

    from backend.util.settings import Config

    config = Config()
    port = {name: 20000 + index for index, name in enumerate(ports.PORT_NAMES)}
    for name, setting in PORT_SETTINGS.items():
        given = port[apps.service(name).port]
        run = inspect.getsource(type(services[name]).run)
        expect(
            getattr(config, setting, None) == given and setting in run,
            f"{name} listens on the port given as {setting.upper()}",
            f"The backend reads {getattr(config, setting, None)!r} for {setting} and its run() "
            f"{'uses' if setting in run else 'no longer uses'} it. The supervisor waits for "
            f"{name} there: update the *_PORT names in settings._service_addresses and its "
            f"entry in SERVICES in {APPS}.",
        )


def uvicorn_can_serve_on_the_hosts_loop(services: dict[str, Any], backend: Path) -> None:
    import uvicorn

    update = f"Update serve_on_one_loop in {HOST}: it builds the server and awaits it itself."

    async def app(scope: Any, receive: Any, send: Any) -> None:
        pass

    server: Any = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None))
    serve = getattr(server, "serve", None)
    needed = [
        parameter.name
        for parameter in inspect.signature(serve).parameters.values()
        if parameter.default is parameter.empty
        and parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
    ]
    expect(
        inspect.iscoroutinefunction(serve) and not needed,
        "uvicorn.Server(uvicorn.Config(app, **options)).serve() is a coroutine needing no arguments",
        f"It now needs {needed}. {update}",
    )
    expect(
        getattr(server, "started", None) is False and getattr(server, "should_exit", None) is False,
        "a uvicorn server has `started` and `should_exit`, both False before it serves",
        f"The host reads the first to tell a failed start and sets the second to stop. {update}",
    )


def the_scheduler_holds_the_connections_the_limit_counts(
    services: dict[str, Any], backend: Path
) -> None:
    from autogpt_desktop import resources

    source = (backend / "executor" / "scheduler.py").read_text(encoding="utf-8")
    pools = source.count("create_engine(")
    bounded = source.count("pool_size=self.db_pool_size()") == pools == source.count(
        "max_overflow=0"
    )
    expect(
        pools == resources.SCHEDULER_POOLS and bounded and "config.scheduler_db_pool_size" in source,
        f"the scheduler keeps {resources.SCHEDULER_POOLS} connection pools of "
        "scheduler_db_pool_size, without overflow",
        f"It now creates {pools} engines, or sizes them differently. Count its connections "
        "again and update SCHEDULER_POOLS and POSTGRES_LIMITS in autogpt_desktop/resources.py.",
    )


def pool_sizes_come_from_the_environment(services: dict[str, Any], backend: Path) -> None:
    from autogpt_desktop import resources

    from backend.util.settings import Config

    config = Config()
    wanted = resources.choose({}, ram_bytes=16 * 1024**3, cores=6).backend_env
    for name, value in wanted.items():
        expect(
            str(getattr(config, name.lower(), None)) == value,
            f"{name} sets the backend's {name.lower()}",
            "Update the names in autogpt_desktop/resources.py.",
        )


def lock_keys_are_the_ones_cleared(services: dict[str, Any], backend: Path) -> None:
    from autogpt_desktop import runs

    from backend.copilot.executor.utils import get_session_lock_key

    update = "Update LOCK_PATTERNS in autogpt_desktop/runs.py."
    manager = (backend / "executor" / "manager.py").read_text(encoding="utf-8")
    key = re.search(r'key=f"(exec_lock:)\{', manager)
    expect(
        bool(key) and fnmatch.fnmatch("exec_lock:some-run", runs.LOCK_PATTERNS["executor"]),
        "the executor locks a run under exec_lock:<id>",
        update,
    )
    expect(
        fnmatch.fnmatch(get_session_lock_key("some-session"), runs.LOCK_PATTERNS["copilot-executor"]),
        f"the copilot executor locks a session under {get_session_lock_key('<id>')}",
        update,
    )


CHECKS = (
    service_names_and_classes,
    nobody_is_connected,
    api_servers_call_uvicorn_run,
    services_answer_where_the_health_table_says,
    the_other_services_listen_on_the_ports_they_are_given,
    uvicorn_can_serve_on_the_hosts_loop,
    pool_sizes_come_from_the_environment,
    the_scheduler_holds_the_connections_the_limit_counts,
    lock_keys_are_the_ones_cleared,
)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)  # the backend's telemetry threads must not hold this open
