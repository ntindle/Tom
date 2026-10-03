"""AutoGPT's own processes: the backend services and the Next server.

The same set the appliance's supervisord runs (supervisor/supervisord.conf)
minus the opt-in chat-bot services. Each backend service is started by
calling its entry point through the bundled interpreter rather than the
console scripts pip generates, because those carry absolute shebangs that
break as soon as the app bundle is moved.
"""

from __future__ import annotations

import os

from autogpt_desktop.layout import Bundle, DataDir
from autogpt_desktop.process import ManagedProcess

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
            stop_timeout=5,
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
        stop_timeout=5,
    )


def entry_point_argv(bundle: Bundle, entry: str, *args: str) -> list[str]:
    module, _, function = entry.partition(":")
    code = f"import sys; from {module} import {function} as m; sys.exit(m())"
    return [str(bundle.python), "-c", code, *args]


def base_env() -> dict[str, str]:
    """What a child needs from the user's environment to function at all
    (system paths, temp dirs, locale on Windows), without inheriting
    AutoGPT settings the user may have exported for some other checkout."""
    keep = (
        "PATH",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "USERNAME",
        "USER",
        "LOGNAME",
        "LANG",
        "SSL_CERT_FILE",
        "AUTOGPT_DESKTOP_NODE",
    )
    return {name: os.environ[name] for name in keep if name in os.environ}
