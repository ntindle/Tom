"""Port allocation.

The appliance owns its network namespace and hardcodes every port. A desktop
shares the machine with whatever else the user runs, so each port is picked
free on first boot and remembered, then re-used for as long as it stays free.
Keeping the public port stable matters most: it is the origin that browser
sessions and OAuth redirect URIs are tied to.
"""

from __future__ import annotations

import json
import random
import socket
from pathlib import Path

# Below the ephemeral range of every supported OS (Linux starts at 32768,
# Windows and macOS at 49152), so a remembered port is never handed out to
# some other program's outbound connection between boots.
PORT_RANGE = range(15000, 32000)

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
        if not (isinstance(port, int) and _is_free(port) and port not in ports.values()):
            port = _free_port(exclude=set(ports.values()))
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


def _is_free(port: int) -> bool:
    if port not in PORT_RANGE:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _free_port(exclude: set[int]) -> int:
    for _ in range(1000):
        port = random.choice(PORT_RANGE)
        if port not in exclude and _is_free(port):
            return port
    raise RuntimeError("no free local port found")
