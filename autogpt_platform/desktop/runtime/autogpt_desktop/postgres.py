"""PostgreSQL, adapted from single-container/entrypoint.sh `initialize_postgres`.

Differences from the appliance: no peer authentication (Windows has none),
so every role, the frontend's included, authenticates with scram over
loopback TCP; and the port is whatever ports.py picked.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

from autogpt_desktop.layout import Bundle, DataDir
from autogpt_desktop.process import ManagedProcess, run_tool, send_posix_signal

CONFIG_MARKER = "# autogpt-desktop"


def is_initialized(data: DataDir) -> bool:
    return (data.postgres / "PG_VERSION").is_file()


def initialize(bundle: Bundle, data: DataDir, password: str) -> None:
    """Create the cluster on first boot."""
    pgdata = data.postgres
    if is_initialized(data):
        return
    password_file = data.run / f"postgres-password.{secrets.token_hex(8)}"
    descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(password + "\n")
    try:
        run_tool(
            [
                str(bundle.postgres_bin("initdb")),
                f"--pgdata={pgdata}",
                "--username=postgres",
                f"--pwfile={password_file}",
                "--auth=scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
            ],
            check=True,
            capture_output=True,
            env=_tool_env(bundle),
        )
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or b"").decode(errors="replace") + (
            exc.stderr or b""
        ).decode(errors="replace")
        raise RuntimeError(f"initdb failed:\n{output}") from exc
    finally:
        password_file.unlink(missing_ok=True)
    with open(pgdata / "postgresql.conf", "a", encoding="utf-8") as conf:
        conf.write(
            f"\n{CONFIG_MARKER}\n"
            "listen_addresses = '127.0.0.1'\n"
            "password_encryption = 'scram-sha-256'\n"  # pragma: allowlist secret
            "max_connections = 100\n"
            "shared_buffers = 128MB\n"
        )


def process(bundle: Bundle, data: DataDir, port: int) -> ManagedProcess:
    # The port is passed per boot rather than written to postgresql.conf, so a
    # port that ports.py had to move never leaves the config stale. Unix
    # sockets are off: everything connects over loopback TCP on every OS.
    return ManagedProcess(
        name="postgres",
        argv=[
            str(bundle.postgres_bin("postgres")),
            "-D",
            str(data.postgres),
            "-p",
            str(port),
            "-c",
            "unix_socket_directories=",
        ],
        env=_tool_env(bundle),
        cwd=data.postgres,
        log_dir=data.logs,
        graceful_stop=_pg_ctl_stop(bundle, data),
        stop_timeout=30,
    )


def is_ready(port: int, password: str) -> bool:
    import psycopg2

    try:
        connection = psycopg2.connect(
            host="127.0.0.1",
            port=port,
            user="postgres",
            password=password,
            dbname="postgres",
            connect_timeout=3,
        )
    except psycopg2.Error:
        return False
    connection.close()
    return True


def _pg_ctl_stop(bundle: Bundle, data: DataDir):
    # Fast shutdown checkpoints and exits; on Windows pg_ctl is also the only
    # way to signal the postmaster, which ignores TerminateProcess etiquette.
    def stop(managed: ManagedProcess) -> None:
        result = run_tool(
            [
                str(bundle.postgres_bin("pg_ctl")),
                "stop",
                "-D",
                str(data.postgres),
                "-m",
                "fast",
                "-w",
                "-t",
                "25",
            ],
            capture_output=True,
            env=_tool_env(bundle),
        )
        if result.returncode != 0:
            send_posix_signal(2)(managed)

    return stop


def _tool_env(bundle: Bundle) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PG", "LC_"))
    }
    env["LANG"] = "C"
    if sys.platform == "darwin":
        lib = str(bundle.root / "postgres" / "lib")
        env["DYLD_FALLBACK_LIBRARY_PATH"] = lib
    elif sys.platform != "win32":
        lib = str(bundle.root / "postgres" / "lib")
        env["LD_LIBRARY_PATH"] = lib
    return env


def bin_dir(bundle: Bundle) -> Path:
    return bundle.postgres_bin("postgres").parent
