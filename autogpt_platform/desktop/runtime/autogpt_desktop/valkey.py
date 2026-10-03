"""Valkey as a single-node cluster.

The backend only speaks RedisCluster (backend/data/redis_client.py), but a
cluster does not need three nodes: one node that owns all 16384 slots is a
complete cluster. The appliance runs three to mirror production; a desktop
gains nothing from the extra memory and processes.
"""

from __future__ import annotations

import logging
import os

from autogpt_desktop.layout import Bundle, DataDir
from autogpt_desktop.process import ManagedProcess, wait_until

logger = logging.getLogger("autogpt_desktop")

CONFIG_NAME = "valkey.conf"
LAST_SLOT = 16383


def write_config(data: DataDir, port: int, bus_port: int, password: str) -> None:
    # Valkey runs with its data directory as cwd and every path relative, so
    # the same config works for the MSYS2-built Windows server, which does not
    # understand drive-letter paths.
    lines = [
        "bind 127.0.0.1",
        "protected-mode yes",
        f"port {port}",
        "dir ./",
        "appendonly yes",
        "appendfsync everysec",
        "cluster-enabled yes",
        "cluster-config-file nodes.conf",
        "cluster-node-timeout 5000",
        "cluster-require-full-coverage no",
        "cluster-announce-ip 127.0.0.1",
        f"cluster-announce-port {port}",
        # Without this the bus listens on port + 10000, which may be taken.
        f"cluster-port {bus_port}",
        f"cluster-announce-bus-port {bus_port}",
        f"requirepass {password}",
        f"masterauth {password}",
        "daemonize no",
        'logfile ""',
    ]
    (data.valkey / CONFIG_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def process(bundle: Bundle, data: DataDir, port: int, password: str) -> ManagedProcess:
    return ManagedProcess(
        name="valkey",
        argv=[str(bundle.valkey_server), CONFIG_NAME],
        env=dict(os.environ),
        cwd=data.valkey,
        log_dir=data.logs,
        graceful_stop=lambda _: _shutdown(port, password),
        stop_timeout=15,
    )


def wait_ready(port: int, password: str, timeout: float = 60) -> bool:
    import redis

    def answers() -> bool:
        try:
            return bool(_client(port, password).ping())
        except redis.RedisError:
            return False

    return wait_until(answers, timeout)


def ensure_cluster(port: int, password: str, timeout: float = 60) -> None:
    """Assign every slot to the only node on first boot; afterwards the node
    restores its slots from nodes.conf and this is a no-op."""
    client = _client(port, password)
    if _cluster_state(client) == "ok":
        return
    owned = client.execute_command("CLUSTER", "SLOTS")
    if not owned:
        logger.info("forming the single-node Valkey cluster")
        client.execute_command("CLUSTER", "ADDSLOTSRANGE", 0, LAST_SLOT)
    if not wait_until(lambda: _cluster_state(client) == "ok", timeout):
        raise RuntimeError("the Valkey cluster did not become healthy")


def _cluster_state(client) -> str:
    info = client.execute_command("CLUSTER", "INFO")
    if isinstance(info, bytes):
        info = info.decode()
    for line in str(info).splitlines():
        key, _, value = line.partition(":")
        if key == "cluster_state":
            return value.strip()
    return "unknown"


def _client(port: int, password: str):
    import redis

    return redis.Redis(
        host="127.0.0.1",
        port=port,
        password=password,
        socket_timeout=5,
        socket_connect_timeout=3,
        decode_responses=True,
    )


def _shutdown(port: int, password: str) -> None:
    import redis

    try:
        _client(port, password).shutdown()
    except redis.ConnectionError:
        pass  # SHUTDOWN closes the connection by design
