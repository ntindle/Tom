import json
import socket
import stat
import sys
import threading
from pathlib import Path

import pytest

from autogpt_desktop import ports, rabbitmq, settings, valkey
from autogpt_desktop.layout import Bundle, DataDir

DESKTOP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DESKTOP / "build"))

import frontend_role_sql  # noqa: E402
import lock_export  # noqa: E402


@pytest.fixture
def data(tmp_path: Path) -> DataDir:
    data = DataDir(tmp_path / "data with space")
    data.prepare()
    return data


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    return Bundle(tmp_path / "runtime")


def test_ports_are_unique_in_range_and_remembered(tmp_path: Path):
    path = tmp_path / "ports.json"
    first = ports.allocate(path)
    assert set(first) == set(ports.PORT_NAMES)
    assert len(set(first.values())) == len(first)
    assert all(port in ports.PORT_RANGE for port in first.values())
    assert ports.allocate(path) == first


def test_a_taken_port_is_replaced_and_the_rest_are_kept(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ports, "PUBLIC_PORT_PATIENCE_SECONDS", 0)
    path = tmp_path / "ports.json"
    first = ports.allocate(path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind(("127.0.0.1", first["public"]))
        second = ports.allocate(path)
    assert second["public"] != first["public"]
    assert {k: v for k, v in second.items() if k != "public"} == {
        k: v for k, v in first.items() if k != "public"
    }
    assert json.loads(path.read_text()) == second


def test_the_public_port_is_waited_for_before_it_is_given_up(tmp_path: Path):
    """It is the app's origin: sessions and OAuth redirect URIs hang off it,
    and its usual holder is a previous run that is still shutting down."""
    path = tmp_path / "ports.json"
    first = ports.allocate(path)
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", first["public"]))
    threading.Timer(1, squatter.close).start()

    assert ports.allocate(path) == first


def test_secrets_are_generated_once(bundle: Bundle, data: DataDir):
    first = settings.ensure_secrets(bundle, data)
    assert first == settings.ensure_secrets(bundle, data)
    assert len(first["AUTOGPT_FRONTEND_DB_PASSWORD"]) >= 32
    assert {"POSTGRES_PASSWORD", "ENCRYPTION_KEY", "BETTER_AUTH_SECRET"} <= set(first)


def test_environment_is_wired_to_the_allocated_ports(bundle: Bundle, data: DataDir):
    secret = settings.ensure_secrets(bundle, data)
    port = ports.allocate(data.ports_file)
    env = settings.backend_environment(bundle, data, port, secret, {})

    public = f"http://127.0.0.1:{port['public']}"
    assert env["AUTOGPT_PUBLIC_URL"] == env["BETTER_AUTH_URL"] == public
    assert env["BACKEND_CORS_ALLOW_ORIGINS"] == json.dumps([public])
    assert f":{port['postgres']}/postgres?schema=platform" in env["DATABASE_URL"]
    assert env["REDIS_CLUSTER_PORT"] == str(port["valkey"])
    assert env["RABBITMQ_PORT"] == str(port["rabbitmq"])
    assert env["AGENT_API_HOST"] == env["WEBSOCKET_SERVER_HOST"] == "127.0.0.1"
    assert env["FORCE_FLAG_GRAPHITI_MEMORY"] == "false"
    assert env["JWT_JWKS_URL"].startswith(f"http://127.0.0.1:{port['frontend']}/")


def test_user_settings_cannot_override_the_runtime_wiring(bundle: Bundle, data: DataDir):
    secret = settings.ensure_secrets(bundle, data)
    port = ports.allocate(data.ports_file)
    user = {"OPEN_ROUTER_API_KEY": "sk-user", "DATABASE_URL": "postgresql://elsewhere"}
    env = settings.backend_environment(bundle, data, port, secret, user)
    assert env["OPEN_ROUTER_API_KEY"] == "sk-user"
    assert "elsewhere" not in env["DATABASE_URL"]


def test_registration_is_open_until_the_user_closes_it(bundle: Bundle, data: DataDir):
    secret = settings.ensure_secrets(bundle, data)
    port = ports.allocate(data.ports_file)

    def allowed(user: dict[str, str]) -> str:
        backend = settings.backend_environment(bundle, data, port, secret, user)
        frontend = settings.frontend_environment(backend, port, secret, data)
        assert frontend["AUTH_ALLOW_NEW_ACCOUNTS"] == backend["AUTH_ALLOW_NEW_ACCOUNTS"]
        return backend["AUTH_ALLOW_NEW_ACCOUNTS"]

    assert allowed({}) == "true"
    assert allowed({"AUTH_ALLOW_NEW_ACCOUNTS": "false"}) == "false"


def test_services_get_a_home_of_their_own(bundle: Bundle, data: DataDir):
    """AutoPilot's coding agents keep their sign-in under the home directory;
    the app must not find the user's personal one there."""
    secret = settings.ensure_secrets(bundle, data)
    port = ports.allocate(data.ports_file)
    backend = settings.backend_environment(bundle, data, port, secret, {})
    frontend = settings.frontend_environment(backend, port, secret, data)

    assert backend["HOME"] == str(data.home)
    assert backend["CLAUDE_CONFIG_DIR"] == str(data.home / ".claude")
    assert backend["CODEX_HOME"] == str(data.home / ".codex")
    assert frontend["HOME"] == str(data.frontend_home)
    if sys.platform == "win32":  # where HOME is ignored
        assert backend["USERPROFILE"] == str(data.home)
        assert frontend["USERPROFILE"] == str(data.frontend_home)


@pytest.mark.skipif(sys.platform == "win32", reason="the user profile's ACL covers this")
def test_files_holding_secrets_are_readable_by_their_owner_only(data: DataDir):
    settings.read_user_settings(data)
    valkey.write_config(data, 20000, 20001, "secret" * 8)
    rabbitmq.prepare(data, 20002, "autogpt", "secret" * 8)

    def mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    assert mode(data.root) == 0o700
    assert mode(data.config / "settings.env") == 0o600
    assert mode(data.valkey / valkey.CONFIG_NAME) == 0o600
    assert mode(data.rabbitmq / "rabbitmq.conf") == 0o600


def test_user_settings_file_is_created_and_blank_values_are_ignored(data: DataDir):
    assert settings.read_user_settings(data) == {}
    path = data.config / "settings.env"
    path.write_text("OPEN_ROUTER_API_KEY=sk-1\nOPENAI_API_KEY=\n# note\n", encoding="utf-8")
    assert settings.read_user_settings(data) == {"OPEN_ROUTER_API_KEY": "sk-1"}


def test_frontend_gets_its_own_database_role_and_no_backend_secrets(
    bundle: Bundle, data: DataDir
):
    secret = settings.ensure_secrets(bundle, data)
    port = ports.allocate(data.ports_file)
    backend = settings.backend_environment(bundle, data, port, secret, {})
    frontend = settings.frontend_environment(backend, port, secret, data)
    assert frontend["DATABASE_URL"].startswith("postgresql://autogpt_frontend:")
    assert frontend["PORT"] == str(port["frontend"])
    assert frontend["HOSTNAME"] == "127.0.0.1"
    for leaked in ("ENCRYPTION_KEY", "POSTGRES_PASSWORD", "REDIS_PASSWORD", "DB_PASS"):
        assert leaked not in frontend


def test_valkey_config_is_a_loopback_single_node_cluster(data: DataDir):
    valkey.write_config(data, 20000, 20001, "secret" * 8)
    config = (data.valkey / valkey.CONFIG_NAME).read_text()
    assert "cluster-enabled yes" in config
    assert "bind 127.0.0.1" in config
    assert "cluster-port 20001" in config
    assert "dir ./" in config


def test_lock_export_pins_main_dependencies_only():
    lines = lock_export.export(DESKTOP.parent / "backend" / "poetry.lock", "linux")
    names = {line.split("==")[0] for line in lines}
    assert {"fastapi", "prisma", "redis", "aio-pika"} <= names
    assert "autogpt-libs" not in names  # a path dependency, installed separately
    assert "pyright" not in names  # dev-only
    assert all("==" in line for line in lines)


def test_lock_export_applies_windows_overrides():
    lock = DESKTOP.parent / "backend" / "poetry.lock"
    windows = dict(line.split(" ; ")[0].split("==") for line in lock_export.export(lock, "win32"))
    linux = dict(line.split(" ; ")[0].split("==") for line in lock_export.export(lock, "linux"))
    assert windows["claude-agent-sdk"] == lock_export.PLATFORM_OVERRIDES["win32"]["claude-agent-sdk"]
    assert linux["claude-agent-sdk"] != windows["claude-agent-sdk"]


def test_frontend_role_policy_is_extracted_from_the_appliance_bootstrap():
    sql = frontend_role_sql.extract(DESKTOP.parent / "single-container" / "bootstrap.sh")
    assert sql.lstrip().startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")
    assert 'GRANT SELECT (id, email), UPDATE (email, "updatedAt")' in sql


def test_frontend_role_extraction_fails_loudly_when_the_layout_changes(tmp_path: Path):
    script = tmp_path / "bootstrap.sh"
    script.write_text("configure_frontend_database_role() {\n  psql -f role.sql\n}\n")
    with pytest.raises(ValueError):
        frontend_role_sql.extract(script)
