"""Secrets and the service environment.

A port of single-container/entrypoint.sh `configure_environment`. Secrets come
from the same generator the appliance uses (runtime_config.py), so the two
distributions can never disagree about which secrets exist. Values the user
supplies (API keys, OAuth apps) go in config/settings.env and win over
nothing generated: they are layered underneath the runtime's own wiring.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

from autogpt_desktop.layout import Bundle, DataDir, write_private

DB_CONNECTION_LIMIT = 5
FRONTEND_DB_ROLE = "autogpt_frontend"

# Desktop-only secrets. runtime_config.py rejects unknown keys in its own file,
# so these live beside it rather than in it.
DESKTOP_SECRETS = ("AUTOGPT_FRONTEND_DB_PASSWORD",)

SETTINGS_TEMPLATE = """\
# AutoGPT desktop settings. Restart AutoGPT after editing.
# Configure only the providers you use; AutoPilot uses OpenRouter by default.
OPEN_ROUTER_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GROQ_API_KEY=
# OpenAI-compatible local inference (for example Ollama):
# CHAT_USE_LOCAL=true
# CHAT_BASE_URL=http://127.0.0.1:11434/v1
# CHAT_API_KEY=ollama
# Once you have your account, stop anyone else on this computer creating one:
# AUTH_ALLOW_NEW_ACCOUNTS=false
"""

# What the Next server is given of the backend's environment: the appliance's
# list (single-container/run-frontend.sh) without the social sign-in
# providers. Those send the whole window to the provider and back, and the
# shell keeps the window on the app (src/navigation.js).
FRONTEND_PASSTHROUGH = (
    "AGPT_SERVER_URL",
    "AGPT_WS_SERVER_URL",
    "AUTH_ALLOW_NEW_ACCOUNTS",
    "AUTH_DB_SCHEMA",
    "AUTH_REQUIRE_EMAIL_VERIFICATION",
    "AUTH_SIGNUP_ALLOWLIST",
    "BETTER_AUTH_INTERNAL_URL",
    "BETTER_AUTH_SECRET",
    "BETTER_AUTH_URL",
    "OPENAI_API_BASE_URL",
    "OPENAI_API_KEY",
    "TRANSCRIPTION_API_BASE_URL",
    "TRANSCRIPTION_API_KEY",
    "TRANSCRIPTION_MODEL",
)


def load_runtime_config_module(bundle: Bundle) -> ModuleType:
    candidates = (
        bundle.root / "assets" / "runtime_config.py",
        Path(__file__).resolve().parents[3] / "single-container" / "runtime_config.py",
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise RuntimeError("runtime_config.py is missing from the bundle")
    spec = importlib.util.spec_from_file_location("autogpt_runtime_config", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_secrets(bundle: Bundle, data: DataDir) -> dict[str, str]:
    runtime_config = load_runtime_config_module(bundle)
    values = dict(runtime_config.ensure_runtime_config(data.runtime_env, {}))
    values.update(_ensure_desktop_secrets(data.config / "desktop.env"))
    return values


def read_user_settings(data: DataDir) -> dict[str, str]:
    path = data.config / "settings.env"
    if not path.exists():
        write_private(path, SETTINGS_TEMPLATE)
    return {key: value for key, value in _read_env(path).items() if value}


def backend_environment(
    bundle: Bundle,
    data: DataDir,
    ports: dict[str, int],
    secret: dict[str, str],
    user: dict[str, str],
) -> dict[str, str]:
    public_url = f"http://127.0.0.1:{ports['public']}"
    database = _database_url(
        "postgres", secret["POSTGRES_PASSWORD"], ports["postgres"], "platform"
    )
    frontend_origin = f"http://127.0.0.1:{ports['frontend']}"
    env = {
        **user,
        "AUTOGPT_PUBLIC_URL": public_url,
        "APP_ENV": "dev",
        "BEHAVE_AS": "local",
        "ENABLE_AUTH": "true",
        "AUTH_ALLOW_NEW_ACCOUNTS": user.get("AUTH_ALLOW_NEW_ACCOUNTS", "true"),
        "AUTH_REQUIRE_EMAIL_VERIFICATION": "false",
        "JWT_VERIFY_KEY": "",
        "SUPABASE_JWT_SECRET": "",
        "BETTER_AUTH_SECRET": secret["BETTER_AUTH_SECRET"],
        "BETTER_AUTH_URL": public_url,
        "BETTER_AUTH_INTERNAL_URL": frontend_origin,
        "JWT_JWKS_URL": f"{frontend_origin}/api/auth/jwks",
        "ENCRYPTION_KEY": secret["ENCRYPTION_KEY"],
        "UNSUBSCRIBE_SECRET_KEY": secret["UNSUBSCRIBE_SECRET_KEY"],
        "VAPID_PRIVATE_KEY": secret["VAPID_PRIVATE_KEY"],
        "VAPID_PUBLIC_KEY": secret["VAPID_PUBLIC_KEY"],
        "VAPID_CLAIM_EMAIL": user.get("VAPID_CLAIM_EMAIL", "mailto:admin@localhost"),
        "DATABASE_URL": f"{database}&connection_limit={DB_CONNECTION_LIMIT}"
        "&connect_timeout=60&pool_timeout=300",
        "DIRECT_URL": f"{database}&connect_timeout=60",
        "DB_HOST": "127.0.0.1",
        "DB_PORT": str(ports["postgres"]),
        "DB_USER": "postgres",
        "DB_NAME": "postgres",
        "DB_PASS": secret["POSTGRES_PASSWORD"],
        "DB_SCHEMA": "platform",
        "AUTH_DB_SCHEMA": "platform",
        "PRISMA_SCHEMA": str(bundle.backend_dir / "schema.prisma"),
        "PRISMA_QUERY_ENGINE_BINARY": str(bundle.prisma_engine("query-engine")),
        "PRISMA_SCHEMA_ENGINE_BINARY": str(bundle.prisma_engine("schema-engine")),
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": str(ports["valkey"]),
        "REDIS_CLUSTER_HOST": "127.0.0.1",
        "REDIS_CLUSTER_PORT": str(ports["valkey"]),
        "REDIS_PASSWORD": secret["REDIS_PASSWORD"],
        "REDIS_USE_ANNOUNCED_ADDRESS": "false",
        "RABBITMQ_HOST": "127.0.0.1",
        "RABBITMQ_PORT": str(ports["rabbitmq"]),
        "RABBITMQ_CLUSTER_HOST": "127.0.0.1",
        "RABBITMQ_CLUSTER_PORT": str(ports["rabbitmq"]),
        "RABBITMQ_VHOST": "/",
        "RABBITMQ_DEFAULT_USER": secret["RABBITMQ_DEFAULT_USER"],
        "RABBITMQ_DEFAULT_PASS": secret["RABBITMQ_DEFAULT_PASS"],
        # Graphiti's FalkorDB store is a Linux Redis module under the SSPL;
        # the desktop build ships without it.
        "FORCE_FLAG_GRAPHITI_MEMORY": "false",
        "CLAMAV_SERVICE_ENABLED": "false",
        "MEM0_TELEMETRY": "false",
        "GRAPHITI_TELEMETRY_ENABLED": "false",
        "CHAT_DAILY_COST_LIMIT_MICRODOLLARS": user.get(
            "CHAT_DAILY_COST_LIMIT_MICRODOLLARS", "-1"
        ),
        "CHAT_WEEKLY_COST_LIMIT_MICRODOLLARS": user.get(
            "CHAT_WEEKLY_COST_LIMIT_MICRODOLLARS", "-1"
        ),
        "FRONTEND_BASE_URL": public_url,
        "PLATFORM_BASE_URL": f"{public_url}/_agpt",
        "PLATFORM_LINK_BASE_URL": f"{public_url}/link",
        "BACKEND_CORS_ALLOW_ORIGINS": json.dumps([public_url]),
        "AGPT_SERVER_URL": f"http://127.0.0.1:{ports['agent_api']}/api",
        "AGPT_WS_SERVER_URL": f"ws://127.0.0.1:{ports['websocket']}/ws",
        "WORKSPACE_STORAGE_DIR": str(data.workspaces),
        "NODE_ENV": "production",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(bundle.root / "assets" / "python"),
        "XDG_CACHE_HOME": str(data.backend_cache),
        **_home(data.home),
        # AutoPilot's coding agents keep their sign-in and transcripts under
        # the home directory. Like the appliance, the app has its own: it
        # must not pick up, or write into, the user's personal Claude Code or
        # Codex setup.
        "CLAUDE_CONFIG_DIR": str(data.home / ".claude"),
        "CODEX_HOME": str(data.home / ".codex"),
        **_service_addresses(ports),
    }
    return env


def frontend_environment(
    backend: dict[str, str], ports: dict[str, int], secret: dict[str, str], data: DataDir
) -> dict[str, str]:
    database = _database_url(
        FRONTEND_DB_ROLE,
        secret["AUTOGPT_FRONTEND_DB_PASSWORD"],
        ports["postgres"],
        None,
    )
    return {
        **{name: backend[name] for name in FRONTEND_PASSTHROUGH if name in backend},
        "DATABASE_URL": f"{database}&connection_limit=10",
        "PORT": str(ports["frontend"]),
        "HOSTNAME": "127.0.0.1",
        "NODE_ENV": "production",
        "NEXT_TELEMETRY_DISABLED": "1",
        "XDG_CACHE_HOME": str(data.next_cache),
        **_home(data.frontend_home),
    }


def _home(directory: Path) -> dict[str, str]:
    """The home directory, as each OS spells it. Python and Node both ignore
    HOME on Windows and read USERPROFILE."""
    home = {"HOME": str(directory)}
    if sys.platform == "win32":
        home["USERPROFILE"] = str(directory)
    return home


def _service_addresses(ports: dict[str, int]) -> dict[str, str]:
    hosts = (
        "PYRO_HOST",
        "AGENTSERVER_HOST",
        "SCHEDULER_HOST",
        "DATABASEMANAGER_HOST",
        "EXECUTIONMANAGER_HOST",
        "NOTIFICATIONMANAGER_HOST",
        "PLATFORMLINKINGMANAGER_HOST",
        "COPILOTEXECUTOR_HOST",
        "COPILOTCHATBRIDGE_HOST",
        "AGENT_API_HOST",
        "WEBSOCKET_SERVER_HOST",
    )
    return {
        **{name: "127.0.0.1" for name in hosts},
        "WEBSOCKET_SERVER_PORT": str(ports["websocket"]),
        "EXECUTION_MANAGER_PORT": str(ports["execution_manager"]),
        "EXECUTION_SCHEDULER_PORT": str(ports["execution_scheduler"]),
        "DATABASE_API_PORT": str(ports["database_api"]),
        "AGENT_API_PORT": str(ports["agent_api"]),
        "NOTIFICATION_SERVICE_PORT": str(ports["notification"]),
        "COPILOT_EXECUTOR_PORT": str(ports["copilot_executor"]),
        "PLATFORM_LINKING_SERVICE_PORT": str(ports["platform_linking"]),
        "COPILOT_CHAT_BRIDGE_PORT": str(ports["copilot_chat_bridge"]),
        "BATCH_EXECUTOR_PORT": str(ports["batch_executor"]),
    }


def _database_url(user: str, password: str, port: int, schema: str | None) -> str:
    query = f"schema={schema}" if schema else "sslmode=disable"
    return (
        f"postgresql://{user}:{quote(password, safe='')}@127.0.0.1:{port}/postgres?{query}"
    )


def _ensure_desktop_secrets(path: Path) -> dict[str, str]:
    values = _read_env(path) if path.exists() else {}
    missing = [name for name in DESKTOP_SECRETS if not values.get(name)]
    if missing:
        values.update({name: secrets.token_urlsafe(36) for name in missing})
        write_private(path, "".join(f"{k}={v}\n" for k, v in values.items()))
    return values


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values
