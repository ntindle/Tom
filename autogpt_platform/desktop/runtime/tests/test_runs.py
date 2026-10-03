import json
import os
import socket
from pathlib import Path

import pytest

from autogpt_desktop import ports, runs, valkey
from autogpt_desktop.layout import Bundle, DataDir
from autogpt_desktop.process import wait_until

DESKTOP = Path(__file__).resolve().parents[2]


class Cursor:
    def __init__(self, stale: list[str]) -> None:
        self.stale = stale
        self.executed: list[tuple[str, dict | None]] = []
        self.rowcount = 0

    def execute(self, sql: str, arguments: dict | None = None) -> None:
        self.executed.append((sql, arguments))
        self.rowcount = len(arguments["ids"]) if arguments else 0

    def fetchall(self) -> list[tuple[str]]:
        return [(run,) for run in self.stale]


class Connection:
    def __init__(self, stale: list[str]) -> None:
        self.recorded = Cursor(stale)
        self.committed = self.closed = False

    def cursor(self) -> Cursor:
        return self.recorded

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def ended(connection: Connection) -> list[tuple[str, list[str]]]:
    """(status, run ids) of each batch of runs that was ended."""
    return [
        (arguments["status"], arguments["ids"])
        for sql, arguments in connection.recorded.executed
        if sql == runs.END_RUNS and arguments
    ]


def test_a_run_the_user_stopped_is_ended_and_an_old_one_failed(tmp_path: Path, caplog):
    stopped = tmp_path / runs.STOPPED_RUNS_FILE
    stopped.write_text(json.dumps(["run-the-user-stopped"]))
    connection = Connection(stale=["run-from-last-week"])
    caplog.set_level("INFO")

    runs.reconcile(lambda: connection, stopped)

    assert ended(connection) == [
        ("TERMINATED", ["run-the-user-stopped"]),
        ("FAILED", ["run-from-last-week"]),
    ]
    # Their running nodes first, as the platform does it.
    statements = [sql for sql, _ in connection.recorded.executed]
    assert statements.index(runs.END_NODES) < statements.index(runs.END_RUNS)
    assert connection.committed and connection.closed
    assert not stopped.exists()
    assert "marked 1 agent run(s) TERMINATED" in caplog.text


def test_with_nothing_interrupted_nothing_is_written(tmp_path: Path):
    connection = Connection(stale=[])
    runs.reconcile(lambda: connection, tmp_path / runs.STOPPED_RUNS_FILE)
    assert ended(connection) == []


def test_only_runs_older_than_a_day_are_failed_the_rest_resume():
    assert "interval '24 hours'" in runs.UNFINISHED_AND_OLD
    assert "IN ('RUNNING', 'QUEUED')" in runs.UNFINISHED_AND_OLD
    # A run that has already ended, or was deleted, is left alone.
    assert "IN ('RUNNING', 'QUEUED') AND NOT \"isDeleted\"" in runs.END_RUNS


@pytest.mark.parametrize("content", ["not json", '{"a": 1}', "[1, null]"])
def test_a_damaged_record_of_stopped_runs_is_ignored(tmp_path: Path, content: str):
    stopped = tmp_path / runs.STOPPED_RUNS_FILE
    stopped.write_text(content)
    connection = Connection(stale=[])

    runs.reconcile(lambda: connection, stopped)

    assert ended(connection) == []
    assert not stopped.exists()


def test_a_database_that_changed_shape_does_not_stop_the_start(tmp_path: Path, caplog):
    """Upstream renamed a table: runs stay as they are, the app starts, and
    the record of stopped runs is kept for a version that can apply it."""
    stopped = tmp_path / runs.STOPPED_RUNS_FILE
    stopped.write_text(json.dumps(["run"]))

    def connect():
        raise RuntimeError('relation "AgentGraphExecution" does not exist')

    runs.reconcile(connect, stopped)

    assert "could not settle interrupted agent runs" in caplog.text
    assert stopped.exists()


def test_a_cache_that_is_not_there_does_not_stop_the_start(caplog):
    pytest.importorskip("redis")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    runs.Cache(port, "password").clear_stale_locks(["executor"])

    assert "could not clear stale run locks" in caplog.text


def test_services_without_locks_do_not_touch_the_cache():
    runs.Cache(1, "password").clear_stale_locks(["scheduler", "rest"])  # nothing to connect to


def bundled_valkey() -> Bundle | None:
    configured = os.environ.get("AUTOGPT_DESKTOP_RUNTIME")
    bundle = Bundle(Path(configured) if configured else DESKTOP / "build" / "runtime")
    return bundle if bundle.valkey_server.is_file() else None


@pytest.fixture
def cache(tmp_path: Path):
    """The bundle's Valkey, in cluster mode as the runtime runs it."""
    redis = pytest.importorskip("redis")
    bundle = bundled_valkey()
    if bundle is None:
        pytest.skip("no assembled bundle (build/runtime) to take Valkey from")
    data = DataDir(tmp_path / "data")
    data.prepare()
    port = ports._free_port(exclude=set())
    valkey.write_config(data, port, ports._free_port(exclude={port}), "password")
    process = valkey.process(bundle, data, port, "password")
    process.start()
    try:
        assert wait_until(lambda: valkey.is_ready(port, "password"), 30, 0.2)
        valkey.ensure_cluster(port, "password")
        yield runs.Cache(port, "password"), redis.Redis(port=port, password="password")
    finally:
        process.stop()


def test_stale_locks_in_every_slot_are_cleared_and_nothing_else(cache):
    """The node is in cluster mode, where one command cannot name keys from
    different slots; the locks of many runs are spread over all of them."""
    locks, client = cache
    run_locks = [f"exec_lock:run-{number}" for number in range(40)]
    session_locks = [f"copilot:session:session-{number}:lock" for number in range(40)]
    for key in [*run_locks, *session_locks, "copilot:session:s:meta", "some:other:key"]:
        client.set(key, "an executor that is gone", ex=300)
    assert len({client.execute_command("CLUSTER", "KEYSLOT", key) for key in run_locks}) > 20
    with pytest.raises(redis_errors().ClusterCrossSlotError):
        client.delete(*run_locks)  # why the locks are deleted one per command

    locks.clear_stale_locks(["scheduler", "executor"])
    assert not any(client.exists(key) for key in run_locks)
    assert all(client.exists(key) for key in session_locks)

    locks.clear_stale_locks(["executor", "copilot-executor"])
    assert not any(client.exists(key) for key in session_locks)
    assert client.exists("copilot:session:s:meta") and client.exists("some:other:key")


def redis_errors():
    import redis

    return redis.exceptions


def test_a_record_of_stopped_runs_that_cannot_be_removed_does_not_stop_the_app(
    tmp_path: Path, monkeypatch, caplog
):
    """Read-only, or held open by a scanner on Windows. It is applied again
    on the next start, to runs that have all ended by then."""
    stopped = tmp_path / runs.STOPPED_RUNS_FILE
    stopped.write_text("[]", encoding="utf-8")

    def refuse(self, missing_ok=False):
        raise PermissionError(13, "in use by another process")

    monkeypatch.setattr(Path, "unlink", refuse)

    runs.reconcile(lambda: NothingToSettle(), stopped)

    assert f"could not remove {runs.STOPPED_RUNS_FILE}" in caplog.text


class NothingToSettle:
    """As much of a database connection as settling uses when no run needs it."""

    def cursor(self):
        return self

    def execute(self, statement, arguments=None) -> None:
        pass

    def fetchall(self) -> list:
        return []

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass
