"""RabbitMQ on a bundled Erlang runtime.

The appliance copies /opt/erlang and /opt/rabbitmq out of the official image.
Here the same two trees sit under the bundle root, and everything RabbitMQ
would otherwise derive from the user's profile (data dirs, the Erlang cookie,
the epmd and distribution ports) is pinned so it cannot collide with another
Erlang installation on the machine.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

from autogpt_desktop.layout import EXE, Bundle, DataDir
from autogpt_desktop.process import ManagedProcess, wait_until

NODE_NAME = "rabbit@localhost"
WINDOWS = sys.platform == "win32"


def prepare(data: DataDir, port: int, user: str, password: str) -> None:
    base = data.rabbitmq
    (base / "mnesia").mkdir(parents=True, exist_ok=True)
    (base / "rabbitmq.conf").write_text(
        "\n".join(
            [
                f"listeners.tcp.default = 127.0.0.1:{port}",
                "distribution.listener.interface = 127.0.0.1",
                "loopback_users.guest = true",
                "log.console = true",
                "log.console.level = info",
                "log.file = false",
                f"default_user = {user}",
                f"default_pass = {password}",
                "default_vhost = /",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (base / "enabled_plugins").write_text("[].\n", encoding="utf-8")
    cookie = base / ".erlang.cookie"
    if not cookie.exists():
        descriptor = os.open(cookie, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(secrets.token_hex(20).upper())


def environment(bundle: Bundle, data: DataDir, ports: dict[str, int]) -> dict[str, str]:
    base = _short(data.rabbitmq)
    erlang = _short(bundle.erlang_home)
    env = {
        **os.environ,
        "PATH": os.pathsep.join([str(Path(erlang) / "bin"), os.environ.get("PATH", "")]),
        "ERLANG_HOME": erlang,
        "RABBITMQ_HOME": _short(bundle.rabbitmq_home),
        "RABBITMQ_BASE": base,
        "RABBITMQ_MNESIA_BASE": str(Path(base) / "mnesia"),
        "RABBITMQ_CONFIG_FILE": str(Path(base) / "rabbitmq.conf"),
        "RABBITMQ_ENABLED_PLUGINS_FILE": str(Path(base) / "enabled_plugins"),
        "RABBITMQ_LOGS": "-",
        "RABBITMQ_NODENAME": NODE_NAME,
        "RABBITMQ_DIST_PORT": str(ports["rabbitmq_dist"]),
        "ERL_EPMD_PORT": str(ports["epmd"]),
        "ERL_EPMD_ADDRESS": "127.0.0.1",
        "ERL_CRASH_DUMP": str(Path(base) / "erl_crash.dump"),
        # Erlang finds its cookie in the home directory; point every flavour
        # of "home" at the data dir so the server and rabbitmqctl agree.
        "HOME": base,
        "USERPROFILE": base,
        "RUNNING_UNDER_SYSTEMD": "true",
    }
    if WINDOWS:
        drive, rest = os.path.splitdrive(base)
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = rest or "\\"
    return env


def process(bundle: Bundle, data: DataDir, ports: dict[str, int]) -> ManagedProcess:
    env = environment(bundle, data, ports)
    return ManagedProcess(
        name="rabbitmq",
        argv=[str(bundle.rabbitmq_script("rabbitmq-server"))],
        env=env,
        cwd=data.rabbitmq,
        log_dir=data.logs,
        graceful_stop=lambda _: _stop(bundle, env),
        stop_timeout=30,
    )


def wait_ready(port: int, user: str, password: str, timeout: float = 240) -> bool:
    import pika
    import pika.exceptions

    parameters = pika.ConnectionParameters(
        host="127.0.0.1",
        port=port,
        credentials=pika.PlainCredentials(user, password),
        connection_attempts=1,
        socket_timeout=3,
        blocked_connection_timeout=3,
    )

    def accepts_logins() -> bool:
        try:
            pika.BlockingConnection(parameters).close()
            return True
        except pika.exceptions.AMQPError:
            return False

    return wait_until(accepts_logins, timeout, interval=1)


def _stop(bundle: Bundle, env: dict[str, str]) -> None:
    _ctl(bundle, env, "stop")
    # Erlang leaves its port mapper daemon running after the node exits.
    epmd = next(bundle.erlang_home.glob(f"erts-*/bin/epmd{EXE}"), None)
    if epmd:
        subprocess.run(
            [str(epmd), "-kill"],
            env=env,
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _ctl(bundle: Bundle, env: dict[str, str], *args: str) -> None:
    subprocess.run(
        [str(bundle.rabbitmq_script("rabbitmqctl")), "-n", NODE_NAME, *args],
        env=env,
        capture_output=True,
        timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _short(path: Path) -> str:
    """RabbitMQ's Windows batch scripts and Erlang both stumble over spaces and
    non-ASCII characters, and %LOCALAPPDATA% contains the user's name. The
    8.3 alias of the same directory has neither."""
    if not WINDOWS:
        return str(path)
    import ctypes

    path.mkdir(parents=True, exist_ok=True)
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    return buffer.value if 0 < length < len(buffer) else str(path)
