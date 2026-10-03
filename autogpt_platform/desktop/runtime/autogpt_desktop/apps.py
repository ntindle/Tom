"""AutoGPT's own processes: the backend services and the Next server.

The same set the appliance's supervisord runs (supervisor/supervisord.conf)
minus the opt-in chat-bot services. Each backend service is started by
calling its entry point through the bundled interpreter rather than the
console scripts pip generates, because those carry absolute shebangs that
break as soon as the app bundle is moved.
"""

from __future__ import annotations

from autogpt_desktop.layout import Bundle, DataDir
from autogpt_desktop.process import ManagedProcess, base_env

# (process name, entry point) in the appliance's start order.
BACKEND_SERVICES = (
    ("database-manager", "backend.db:main"),
    ("scheduler", "backend.scheduler:main"),
    ("batch-executor", "backend.batch_executor:main"),
    ("notification", "backend.notification:main"),
    ("executor", "backend.exec:main"),
    ("copilot-executor", "backend.copilot.executor.__main__:main"),
    ("websocket", "backend.ws:main"),
    ("rest", "backend.rest:main"),
)

SKILLS_CATALOG_ENTRY = "backend.cli.publish_skills_catalog:main"

# How long a service gets between being asked to stop and being killed. The
# appliance measured this (single-container/supervisor/supervisord.conf): a
# service finishes its own cleanup well inside a second, then sits in
# third-party telemetry teardown for several more, so waiting longer buys
# nothing. On Windows the question does not arise; there the ask is the kill.
STOP_TIMEOUT_SECONDS = 3


def backend_processes(
    bundle: Bundle, data: DataDir, env: dict[str, str]
) -> list[ManagedProcess]:
    return [
        ManagedProcess(
            name=name,
            argv=entry_point_argv(bundle, entry),
            env={**base_env(), **env},
            cwd=bundle.backend_dir,
            log_dir=data.logs,
            stop_timeout=STOP_TIMEOUT_SECONDS,
        )
        for name, entry in BACKEND_SERVICES
    ]


def frontend_process(bundle: Bundle, data: DataDir, env: dict[str, str]) -> ManagedProcess:
    return ManagedProcess(
        name="frontend",
        argv=[*bundle.node_command(), str(bundle.frontend_server)],
        env={**base_env(), **env, "ELECTRON_RUN_AS_NODE": "1"},
        cwd=bundle.frontend_server.parent,
        log_dir=data.logs,
        stop_timeout=STOP_TIMEOUT_SECONDS,
    )


def entry_point_argv(bundle: Bundle, entry: str, *args: str) -> list[str]:
    module, _, function = entry.partition(":")
    code = f"import sys; from {module} import {function} as m; sys.exit(m())"
    return [str(bundle.python), "-c", code, *args]
