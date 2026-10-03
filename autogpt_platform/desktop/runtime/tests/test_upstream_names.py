"""Upstream names the service host, the profiles and the run settling use,
read from the backend's source without importing it.

These run wherever the unit tests run, including the gate that an upstream
sync has to pass, so a rename upstream fails here with what to update.
backend_contract.py checks the same dependencies, and the ones that need the
backend imported, against the backend in a bundle.
"""

import ast
import re
from pathlib import Path

import pytest

from autogpt_desktop import apps, ports, resources, runs, settings

PLATFORM = Path(__file__).resolve().parents[3]
BACKEND = PLATFORM / "backend" / "backend"
HOST = "desktop/runtime/autogpt_desktop/servicehost.py"


def source(*parts: str) -> str:
    return BACKEND.joinpath(*parts).read_text(encoding="utf-8")


def entry_module(service: apps.Service) -> Path:
    module = service.entry.partition(":")[0].removeprefix("backend.")
    return BACKEND.joinpath(*module.split(".")).with_suffix(".py")


def classes_handed_over(tree: ast.AST) -> set[str]:
    """The classes instantiated inside a call to run_processes."""
    return {
        node.func.id
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "run_processes"
        for node in ast.walk(call)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } - {"run_processes"}


def test_backend_app_still_has_the_seam_the_services_are_taken_from():
    assert re.search(r"^def run_processes\(\*processes", source("app.py"), re.M), (
        f"backend.app.run_processes(*processes) is gone or changed. {HOST} puts a collector "
        "in its place to be handed each service; find where an entry point hands its service "
        "over now and collect it there."
    )


@pytest.mark.parametrize("service", apps.SERVICES, ids=lambda service: service.name)
def test_each_entry_point_hands_one_service_to_run_processes(service: apps.Service):
    path = entry_module(service)
    update = f"Update {service.name}'s entry in SERVICES in desktop/runtime/autogpt_desktop/apps.py."
    assert path.is_file(), f"{service.entry} no longer exists upstream. {update}"
    text = path.read_text(encoding="utf-8")
    assert "from backend.app import run_processes" in text, (
        f"{path.name} no longer imports run_processes from backend.app, so the collector in "
        f"{HOST} never sees its service."
    )
    assert f"def {service.entry.partition(':')[2]}(" in text, update
    handed = classes_handed_over(ast.parse(text))
    assert len(handed) == 1, f"{path.name} hands over {sorted(handed)}; the host expects one."


def test_the_desktop_runs_every_service_upstream_runs_but_the_chat_bridges():
    upstream = classes_handed_over(ast.parse(source("app.py")))
    ours = set()
    for service in apps.SERVICES:
        ours |= classes_handed_over(ast.parse(entry_module(service).read_text(encoding="utf-8")))
    assert upstream - {"PlatformLinkingManager", "CoPilotChatBridge"} == ours, (
        f"backend.app.main runs {sorted(upstream)}; the desktop runs {sorted(ours)}. Add a new "
        "service to SERVICES and to a group in apps.py (and its port to ports.py), or remove "
        "one that is gone."
    )


def test_a_service_can_still_be_run_in_a_thread_and_cleaned_up():
    process = source("util", "process.py")
    update = f"Update run_service, clean_up and check_service in {HOST}."
    assert "class AppProcess(" in process, update
    assert re.search(r"def start\(self, background: bool = False", process), (
        f"AppProcess.start(background=False) changed. {update}"
    )
    assert re.search(r"^    def cleanup\(self\):", process, re.M), f"AppProcess.cleanup() changed. {update}"
    assert re.search(r"@property\s+def service_name\(self\)", process), update
    # Why the host makes signal.signal thread-aware. If this goes, so can that.
    assert "signal.signal(signal.SIGTERM, self._self_terminate)" in process, (
        "AppProcess no longer installs signal handlers in execute_run_command; "
        f"make_signals_thread_aware in {HOST} may no longer be needed."
    )


def test_the_database_switch_the_watchdog_reads_is_still_there():
    assert re.search(r"^def is_connected\(\)", source("data", "db.py"), re.M), (
        f"backend.data.db.is_connected() is gone. The watchdog in {HOST} (database_is_connected) "
        "uses it to notice a worker service connecting to the database itself."
    )


@pytest.mark.parametrize("module", ["rest_api.py", "ws_api.py"])
def test_the_api_servers_still_serve_through_uvicorn_run(module: str):
    assert "uvicorn.run(" in source("api", module), (
        f"api/{module} no longer calls uvicorn.run. {HOST} replaces that call to serve both API "
        "servers on one event loop (serve_on_one_loop); without it the second server never "
        "finishes starting. Intercept what is called now, or split the api group in apps.py."
    )


def test_services_answer_where_the_health_table_says():
    update = "Update the health paths in SERVICES in desktop/runtime/autogpt_desktop/apps.py."
    assert '"/health_check"' in source("util", "service.py"), update
    assert '@app.get("/health")' in source("api", "ws_api.py"), update
    assert '@app.get(path="/health"' in source("api", "rest_api.py"), update
    for manager in (("executor", "manager.py"), ("copilot", "executor", "manager.py")):
        assert "start_http_server(" in source(*manager), update


# Service -> the backend setting its server's port is read from, and the
# module that reads it. The four AppService services say theirs through
# get_port, which backend_contract.py asks.
PORT_SETTINGS = {
    "executor": ("execution_manager_port", ("executor", "manager.py")),
    "copilot-executor": ("copilot_executor_port", ("copilot", "executor", "manager.py")),
    "websocket": ("websocket_server_port", ("api", "ws_api.py")),
    "rest": ("agent_api_port", ("api", "rest_api.py")),
}


@pytest.mark.parametrize("name", PORT_SETTINGS)
def test_a_service_still_listens_on_the_port_the_runtime_gives_it(name: str):
    """The supervisor waits for each service on that port; the port numbers
    differ per install, so a renamed setting is a service that never answers."""
    setting, module = PORT_SETTINGS[name]
    update = (
        f"Update the *_PORT names in settings._service_addresses and {name}'s entry in "
        "SERVICES in desktop/runtime/autogpt_desktop/apps.py."
    )
    assert re.search(rf"^    {setting}: int = Field\(", source("util", "settings.py"), re.M), (
        f"backend.util.settings.Config has no {setting} any more. {update}"
    )
    assert f"config.{setting}" in source(*module) or f"Config().{setting}" in source(*module), (
        f"{'/'.join(module)} no longer serves on {setting}. {update}"
    )
    port = {name: 20000 + index for index, name in enumerate(ports.PORT_NAMES)}
    given = settings._service_addresses(port)
    assert given[setting.upper()] == str(port[apps.service(name).port])


def test_the_scheduler_still_holds_the_connections_the_limit_counts():
    scheduler = source("executor", "scheduler.py")
    update = (
        "Count the scheduler's connections again and update SCHEDULER_POOLS and "
        "POSTGRES_LIMITS in desktop/runtime/autogpt_desktop/resources.py."
    )
    assert scheduler.count("create_engine(") == resources.SCHEDULER_POOLS, update
    assert scheduler.count("pool_size=self.db_pool_size()") == resources.SCHEDULER_POOLS, update
    assert scheduler.count("max_overflow=0") == resources.SCHEDULER_POOLS, update
    assert "return config.scheduler_db_pool_size" in scheduler, update
    default = re.search(
        r"^    scheduler_db_pool_size: int = Field\(\s+default=(\d+)", source("util", "settings.py"), re.M
    )
    assert default, f"backend.util.settings.Config has no scheduler_db_pool_size any more. {update}"
    assert int(default.group(1)) == resources.SCHEDULER_DB_POOL_SIZE, (
        f"Upstream's scheduler pool is now {default.group(1)}; the desktop pins "
        f"{resources.SCHEDULER_DB_POOL_SIZE}. Follow it if the connection limit has room. {update}"
    )


def test_the_pool_sizes_a_profile_sets_are_backend_settings():
    config = source("util", "settings.py")
    for profile in resources.PROFILES:
        for name in resources.choose({resources.PROFILE_SETTING: profile}, 1, 1).backend_env:
            assert re.search(rf"^    {name.lower()}: int = Field\(", config, re.M), (
                f"backend.util.settings.Config has no {name.lower()} any more. "
                "Update desktop/runtime/autogpt_desktop/resources.py."
            )


def test_the_lock_keys_cleared_before_an_executor_starts_are_the_executors():
    update = "Update LOCK_PATTERNS in desktop/runtime/autogpt_desktop/runs.py."
    assert 'key=f"exec_lock:{graph_exec_id}"' in source("executor", "manager.py"), update
    assert runs.LOCK_PATTERNS["executor"] == "exec_lock:*"
    assert 'return f"copilot:session:{session_id}:lock"' in source(
        "copilot", "executor", "utils.py"
    ), update
    assert runs.LOCK_PATTERNS["copilot-executor"] == "copilot:session:*:lock"
    assert set(runs.LOCK_PATTERNS) <= {service.name for service in apps.SERVICES}


def prisma_model(schema: str, name: str) -> str:
    found = re.search(rf"^model {name} \{{\n(.*?)^\}}", schema, re.DOTALL | re.MULTILINE)
    return found.group(1) if found else ""


@pytest.mark.parametrize(
    "table, columns",
    [
        (runs.GRAPH_EXECUTION_TABLE, runs.GRAPH_EXECUTION_COLUMNS),
        (runs.NODE_EXECUTION_TABLE, runs.NODE_EXECUTION_COLUMNS),
    ],
)
def test_the_run_tables_still_have_the_columns_settling_uses(table: str, columns: tuple[str, ...]):
    schema = (PLATFORM / "backend" / "schema.prisma").read_text(encoding="utf-8")
    model = prisma_model(schema, table)
    update = "Update the names and the SQL in desktop/runtime/autogpt_desktop/runs.py."
    assert model, f"backend/schema.prisma has no model {table} any more. {update}"
    assert "@@map(" not in model, f"{table} is now stored under another table name. {update}"
    fields = set(re.findall(r"^\s+(\w+)\s+\w", model, re.MULTILINE))
    missing = set(columns) - fields
    assert not missing, f"{table} no longer has {sorted(missing)}. {update}"
    for column in columns:
        assert column in runs.END_NODES + runs.END_RUNS + runs.UNFINISHED_AND_OLD


def test_the_statuses_settling_writes_are_execution_statuses():
    schema = (PLATFORM / "backend" / "schema.prisma").read_text(encoding="utf-8")
    statuses = re.search(r"^enum AgentExecutionStatus \{\n(.*?)^\}", schema, re.DOTALL | re.MULTILINE)
    assert statuses, "backend/schema.prisma has no AgentExecutionStatus enum any more."
    missing = set(runs.EXECUTION_STATUSES) - set(statuses.group(1).split())
    assert not missing, f"AgentExecutionStatus lost {sorted(missing)}; update runs.py."


def test_the_skills_catalog_publisher_is_still_where_the_runtime_calls_it():
    module, _, function = apps.SKILLS_CATALOG_ENTRY.partition(":")
    text = source(*module.removeprefix("backend.").split(".")[:-1], module.split(".")[-1] + ".py")
    assert f"def {function}(" in text
    assert "--skip-missing-preloads" in text, "supervisor.publish_skills_catalog passes this flag."
