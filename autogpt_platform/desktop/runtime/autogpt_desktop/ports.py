"""Port allocation.

The appliance owns its network namespace and hardcodes every port. A desktop
shares the machine with whatever else the user runs, so each port is picked
free on first boot and remembered, then re-used for as long as it stays free.
Keeping the public port stable matters most: it is the origin that browser
sessions and OAuth redirect URIs are tied to.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import time
from pathlib import Path

logger = logging.getLogger("autogpt_desktop")

# Below the ephemeral range of every supported OS (Linux starts at 32768,
# Windows and macOS at 49152), so a remembered port is never handed out to
# some other program's outbound connection between boots.
PORT_RANGE = range(15000, 32000)
PUBLIC_PORT_PATIENCE_SECONDS = 5

PORT_NAMES = (
    "public",
    "frontend",
    "postgres",
    "valkey",
    "valkey_bus",
    "rabbitmq",
    "rabbitmq_dist",
    "epmd",
    "websocket",
    "execution_manager",
    "execution_scheduler",
    "database_api",
    "agent_api",
    "notification",
    "copilot_executor",
    "platform_linking",
    "copilot_chat_bridge",
    "batch_executor",
)


def allocate(path: Path, names: tuple[str, ...] = PORT_NAMES) -> dict[str, int]:
    stored = _read(path)
    ports: dict[str, int] = {}
    for name in names:
        port = stored.get(name)
        if not (isinstance(port, int) and _is_free(port, wait=name == "public")):
            port = None
        if port is None or port in ports.values():
            port = _free_port(exclude=set(ports.values()))
            if name in stored:
                logger.warning(f"the {name} port {stored[name]} is taken; using {port}")
        ports[name] = port
    if ports != stored:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ports, indent=2) + "\n", encoding="utf-8")
    return ports


def _read(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_free(port: int, *, wait: bool = False) -> bool:
    """`wait` gives a busy port a few seconds to come free before giving up
    on it. Worth it for the public port only: moving that one signs the user
    out and breaks the redirect URIs of their OAuth apps, and the usual
    holder is a previous run of this app that is still shutting down."""
    if port not in PORT_RANGE:
        return False
    deadline = time.monotonic() + (PUBLIC_PORT_PATIENCE_SECONDS if wait else 0)
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
        time.sleep(0.25)


def _free_port(exclude: set[int]) -> int:
    for _ in range(1000):
        port = random.choice(PORT_RANGE)
        if port not in exclude and _is_free(port):
            return port
    raise RuntimeError("no free local port found")
