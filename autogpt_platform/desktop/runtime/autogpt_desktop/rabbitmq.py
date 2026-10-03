"""RabbitMQ on a bundled Erlang runtime.

The appliance copies /opt/erlang and /opt/rabbitmq out of the official image.
Here the same two trees sit under the bundle root, and everything RabbitMQ
would otherwise derive from the user's profile (data dirs, the Erlang cookie,
the epmd and distribution ports) is pinned so it cannot collide with another
Erlang installation on the machine.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import socket
import sys
import tempfile
from pathlib import Path

from autogpt_desktop import install
from autogpt_desktop.layout import EXE, SCRIPT, Bundle, DataDir, write_private
from autogpt_desktop.process import ManagedProcess, base_env, run_tool

NODE_NAME = "rabbit@localhost"
INETRC_NAME = "erl_inetrc"
LOOPBACK_DISTRIBUTION = "-kernel inet_dist_use_interface {127,0,0,1}"
# RabbitMQ starts its syslog client while it boots, before it knows logging
# goes to the console. In its default UDP mode the client opens a socket on
# every interface at once (it takes no bind address), which is enough for
# Windows Firewall to prompt. In TCP mode it connects only when it has a
# message to send, and with console logging it never does.
QUIET_SYSLOG = "-syslog protocol {rfc5424,tcp}"
WINDOWS = sys.platform == "win32"


def prepare(data: DataDir, port: int, user: str, password: str) -> None:
    base = data.rabbitmq
    (base / "mnesia").mkdir(parents=True, exist_ok=True)
    write_private(
        base / "rabbitmq.conf",
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
    )
    (base / "enabled_plugins").write_text("[].\n", encoding="utf-8")
    # The node is named rabbit@localhost. Answer that name from a static
    # table and leave everything else to the OS resolver; Erlang's own DNS
    # client would otherwise open a wildcard UDP socket and wait out a 2s
    # timeout on every boot.
    (base / INETRC_NAME).write_text(
        '{lookup, [file, native]}.\n{host, {127,0,0,1}, ["localhost"]}.\n',
        encoding="utf-8",
    )
    cookie = base / ".erlang.cookie"
    if not cookie.exists():
        descriptor = os.open(cookie, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(secrets.token_hex(20).upper())


def environment(bundle: Bundle, data: DataDir, ports: dict[str, int]) -> dict[str, str]:
    base = _short(data.rabbitmq)
    erlang = _short(bundle.erlang_home)
    env = {
        # Not the user's whole environment: Erlang and RabbitMQ read dozens of
        # variables (ERL_FLAGS, RABBITMQ_NODE_PORT, ...) that an Erlang
        # developer may have set, and any of them could undo the wiring below.
        **base_env(),
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
        "ERL_INETRC": str(Path(base) / INETRC_NAME),
        # Erlang distribution listens on every interface unless told
        # otherwise, and it starts listening before rabbitmq.conf is read.
        # ERL_AFLAGS reaches every VM started under this environment: the
        # server, rabbitmqctl, and the short-lived `epmd-starter` node that
        # rabbitmq-server.bat launches. A non-loopback socket from any of
        # them is needless exposure, and on Windows raises a firewall prompt.
        "ERL_AFLAGS": _erlang_flags(bundle),
        "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS": QUIET_SYSLOG,
        "ERL_CRASH_DUMP": str(Path(base) / "erl_crash.dump"),
        # Erlang finds its cookie in the home directory; point every flavour
        # of "home" at the data dir so the server and rabbitmqctl agree.
        "HOME": base,
        "USERPROFILE": base,
    }
    if WINDOWS:
        drive, rest = os.path.splitdrive(base)
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = rest or "\\"
    return env


def _erlang_flags(bundle: Bundle) -> str:
    flags = LOOPBACK_DISTRIBUTION
    if any(bundle.erlang_patches.glob("*.beam")):
        # Shadows OTP's inet_udp and inet_tcp so that sockets opened without
        # an address bind loopback (build/erlang_patches.py). Forward
        # slashes: the value is split on spaces and backslashes are escapes.
        flags += f" -pa {_short(bundle.erlang_patches).replace(os.sep, '/')}"
    return flags


def process(bundle: Bundle, data: DataDir, ports: dict[str, int]) -> ManagedProcess:
    env = environment(bundle, data, ports)
    return ManagedProcess(
        name="rabbitmq",
        argv=[_script(bundle, "rabbitmq-server")],
        env=env,
        cwd=data.rabbitmq,
        log_dir=data.logs,
        graceful_stop=lambda _: _stop(bundle, env),
        stop_timeout=30,
    )


def epmd_process(bundle: Bundle, data: DataDir, ports: dict[str, int]) -> ManagedProcess:
    """Erlang's port mapper, which RabbitMQ and rabbitmqctl find each other
    through. Left alone, the first Erlang VM starts one as a daemon that
    outlives everything and that nothing here would know to stop. Started
    first and in the foreground it is a child like any other, and the VMs
    use the one they find running."""
    epmd = next(bundle.erlang_home.glob(f"erts-*/bin/epmd{EXE}"))
    return ManagedProcess(
        name="epmd",
        argv=[str(epmd)],
        env=environment(bundle, data, ports),
        cwd=data.rabbitmq,
        log_dir=data.logs,
    )


def epmd_is_ready(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def is_ready(port: int, user: str, password: str) -> bool:
    import pika
    import pika.exceptions

    # pika logs every refused attempt at ERROR; while the broker boots those
    # are expected and would bury the runtime's own log.
    logging.getLogger("pika").setLevel(logging.CRITICAL)
    parameters = pika.ConnectionParameters(
        host="127.0.0.1",
        port=port,
        credentials=pika.PlainCredentials(user, password),
        connection_attempts=1,
        socket_timeout=3,
        blocked_connection_timeout=3,
    )
    try:
        pika.BlockingConnection(parameters).close()
        return True
    except pika.exceptions.AMQPError:
        return False


def _stop(bundle: Bundle, env: dict[str, str]) -> None:
    run_tool(
        [_script(bundle, "rabbitmqctl"), "-n", NODE_NAME, "stop"],
        env=env,
        capture_output=True,
        timeout=25,
    )


def _script(bundle: Bundle, name: str) -> str:
    # Through the space-free alias: the scripts locate each other from $0.
    return str(Path(_short(bundle.rabbitmq_home)) / "sbin" / f"{name}{SCRIPT}")


def _short(path: Path) -> str:
    """A spelling of `path` with no spaces in it.

    RabbitMQ's launch scripts (batch on Windows, sh elsewhere) source and
    execute unquoted paths, and both the bundle and the data directory
    normally contain a space or the user's name: `%LOCALAPPDATA%` on Windows,
    `~/Library/Application Support` on macOS.
    """
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        return _windows_short_name(path)
    if not any(character.isspace() for character in str(path)):
        return str(path)
    link = _alias_root() / hashlib.sha256(str(path).encode()).hexdigest()[:16]
    if link.is_symlink() and Path(os.readlink(link)) != path:
        link.unlink()
    if not link.is_symlink():
        link.symlink_to(path, target_is_directory=True)
    return str(link)


def _alias_root() -> Path:
    """Where the space-free symlinks live: the user's cache directory. Not
    the temp directory, which macOS and systemd clear of anything untouched
    for a few days; the broker would lose its data path while running."""
    assert sys.platform != "win32"
    if sys.platform == "darwin":
        cache = Path.home() / "Library" / "Caches"
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    # A folder of its own for each install: a variant of the app shares
    # none with the normal app.
    name = install.name()
    root = cache / name / "links"
    if any(character.isspace() for character in str(root)):
        root = Path(tempfile.gettempdir()) / f"{name.lower()}-desktop-{os.getuid()}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _windows_short_name(path: Path) -> str:
    """The 8.3 alias of the directory, which has neither spaces nor
    non-ASCII characters."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    short = buffer.value if 0 < length < len(buffer) else str(path)
    if any(character.isspace() for character in short):
        # Short names can be switched off per drive, and usually are on
        # drives other than the system one.
        raise RuntimeError(
            f"AutoGPT cannot run from a folder with a space in its name ({path}) "
            "on a drive that has short file names turned off. Install it on the "
            "system drive, or in a folder without spaces."
        )
    return short
