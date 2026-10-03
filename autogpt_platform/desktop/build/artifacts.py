"""Third-party runtimes the desktop bundle is assembled from.

Every entry is a prebuilt upstream release. `sha256` pins are verified when
present; an entry without one prints the digest it saw so it can be pinned.
Nothing here is fetched on a user's machine: the build downloads it once and
the installer carries it.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass

PYTHON_VERSION = "3.13.16"
PYTHON_BUILD = "20261001"
NODE_VERSION = "24.21.0"
POSTGRES_BUNDLE = "v0.3.1"  # PostgreSQL 18.6 + pgvector 0.8.6 + pg_trgm
ERLANG_VERSION = "27.3.4.18"  # RabbitMQ 4.1 supports Erlang 26.2-27.x
RABBITMQ_VERSION = "4.1.8"  # matches single-container/Dockerfile
VALKEY_VERSION = "8.1.10"
REDIS_WINDOWS_VERSION = "8.10.2"


@dataclass(frozen=True)
class Artifact:
    url: str
    sha256: str | None = None

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[1]


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}[machine]
    return f"{sys.platform}-{arch}"


_PBS = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{PYTHON_BUILD}/cpython-{PYTHON_VERSION}+{PYTHON_BUILD}-{{triple}}-install_only_stripped.tar.gz"
)
_NODE = f"https://nodejs.org/dist/v{NODE_VERSION}"
_POSTGRES = (
    "https://github.com/boomship/postgres-vector-embedded/releases/download/"
    f"{POSTGRES_BUNDLE}/postgres-lite-{{target}}.tar.gz"
)
_RABBITMQ = (
    "https://github.com/rabbitmq/rabbitmq-server/releases/download/"
    f"v{RABBITMQ_VERSION}"
)

ARTIFACTS: dict[str, dict[str, Artifact]] = {
    "win32-x64": {
        "python": Artifact(_PBS.format(triple="x86_64-pc-windows-msvc")),
        "node": Artifact(f"{_NODE}/win-x64/node.exe"),
        "postgres": Artifact(_POSTGRES.format(target="win32-x64")),
        "erlang": Artifact(
            "https://github.com/erlang/otp/releases/download/"
            f"OTP-{ERLANG_VERSION}/otp_win64_{ERLANG_VERSION}.zip"
        ),
        "rabbitmq": Artifact(f"{_RABBITMQ}/rabbitmq-server-windows-{RABBITMQ_VERSION}.zip"),
        # Valkey publishes no Windows build. Until this project's own MSYS2
        # build of Valkey lands (see build/README.md), the Windows bundle
        # carries the MSYS2 build of Redis from the redis-windows project,
        # which the runtime was verified against in single-node cluster mode.
        "valkey": Artifact(
            "https://github.com/redis-windows/redis-windows/releases/download/"
            f"{REDIS_WINDOWS_VERSION}/Redis-{REDIS_WINDOWS_VERSION}-Windows-x64-msys2.zip"
        ),
    },
    "darwin-arm64": {
        "python": Artifact(_PBS.format(triple="aarch64-apple-darwin")),
        "node": Artifact(f"{_NODE}/node-v{NODE_VERSION}-darwin-arm64.tar.gz"),
        "postgres": Artifact(_POSTGRES.format(target="darwin-arm64")),
        "erlang": Artifact(
            "https://github.com/erlef/otp_builds/releases/download/"
            f"OTP-{ERLANG_VERSION}/OTP-{ERLANG_VERSION}-macos-arm64.tar.gz"
        ),
        "rabbitmq": Artifact(
            f"{_RABBITMQ}/rabbitmq-server-generic-unix-{RABBITMQ_VERSION}.tar.xz"
        ),
        # No macOS binaries upstream; compiled from this source tarball.
        "valkey": Artifact(
            f"https://github.com/valkey-io/valkey/archive/refs/tags/{VALKEY_VERSION}.tar.gz"
        ),
    },
    "linux-x64": {
        "python": Artifact(_PBS.format(triple="x86_64-unknown-linux-gnu")),
        "node": Artifact(f"{_NODE}/node-v{NODE_VERSION}-linux-x64.tar.gz"),
        "postgres": Artifact(_POSTGRES.format(target="linux-x64")),
        "erlang": Artifact(
            f"https://builds.hex.pm/builds/otp/amd64/ubuntu-22.04/OTP-{ERLANG_VERSION}.tar.gz"
        ),
        "rabbitmq": Artifact(
            f"{_RABBITMQ}/rabbitmq-server-generic-unix-{RABBITMQ_VERSION}.tar.xz"
        ),
        "valkey": Artifact(
            f"https://download.valkey.io/releases/valkey-{VALKEY_VERSION}-jammy-x86_64.tar.gz"
        ),
    },
}
