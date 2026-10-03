import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from autogpt_desktop import bootstrap, events, postgres, rabbitmq, valkey
from autogpt_desktop.layout import EXE, Bundle, DataDir
from autogpt_desktop.process import ChildRegistry, ManagedProcess, wait_until
from autogpt_desktop.supervisor import Stack, StartupError

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows ties children to the runtime with a Job Object"
)


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    data = DataDir(tmp_path / "data")
    data.prepare()
    return Stack(Bundle(tmp_path / "runtime"), data)


def python_process(stack: Stack, code: str) -> ManagedProcess:
    process = ManagedProcess(
        name="service",
        argv=[sys.executable, "-c", code],
        env={},
        cwd=stack.data.root,
        log_dir=stack.data.logs,
    )
    process.start()
    return process


def sleeper(stack: Stack, name: str, on_stop: Callable[[str], None]) -> ManagedProcess:
    def stop(process: ManagedProcess) -> None:
        on_stop(name)
        assert process.popen
        process.popen.terminate()

    return ManagedProcess(
        name=name,
        argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        env={},
        cwd=stack.data.root,
        log_dir=stack.data.logs,
        graceful_stop=stop,
    )


def launcher(stack: Stack, tmp_path: Path) -> tuple[ManagedProcess, int]:
    """Like rabbitmq-server: a process that starts the real one and waits.
    Returns it with the pid of what it started."""
    pid_file = tmp_path / "started.pid"
    process = python_process(
        stack,
        "import subprocess, sys, time;"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid));"
        "time.sleep(60)",
    )
    assert wait_until(lambda: pid_file.exists() and pid_file.read_text() != "", 20, 0.1)
    return process, int(pid_file.read_text())


def is_gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_tiers_stop_in_reverse_and_a_tier_stops_all_at_once(stack: Stack):
    stopped: list[str] = []

    def slow_stop(name: str) -> None:
        stopped.append(name)
        time.sleep(1)

    services = ["rest", "executor", "scheduler", "frontend"]
    stack.launch([sleeper(stack, "database", slow_stop)])
    stack.launch([sleeper(stack, name, slow_stop) for name in services])
    started = time.monotonic()
    stack.stop()

    assert sorted(stopped[:-1]) == sorted(services)
    assert stopped[-1] == "database"
    assert time.monotonic() - started < 4  # one at a time would take 5
    assert all(process.exit_code() is not None for process in stack.processes)
    assert not stack.registry.path.exists()


@posix_only
def test_killing_a_service_kills_what_it_started(stack: Stack, tmp_path: Path):
    process, started = launcher(stack, tmp_path)
    process.kill()
    assert wait_until(lambda: is_gone(started), 5, 0.1)


@posix_only
def test_what_a_dead_runtime_left_running_is_killed_on_the_next_start(
    stack: Stack, tmp_path: Path
):
    process, started = launcher(stack, tmp_path)
    stack.registry.record([process])

    ChildRegistry(stack.registry.path).reap_leftovers()

    assert wait_until(lambda: is_gone(started), 5, 0.1)
    assert process.popen and process.popen.wait(5) is not None
    assert not stack.registry.path.exists()


@pytest.mark.usefixtures("cache_home")
def test_the_port_mapper_is_a_supervised_process_not_a_daemon(tmp_path: Path):
    """Erlang would otherwise start `epmd -daemon`, which leaves the session
    and outlives the app."""
    bundle = Bundle(tmp_path / "runtime")
    data = DataDir(tmp_path / "data")
    data.prepare()
    epmd = bundle.erlang_home / "erts-15.0" / "bin" / f"epmd{EXE}"
    epmd.parent.mkdir(parents=True)
    epmd.touch()

    process = rabbitmq.epmd_process(bundle, data, {"epmd": 15001, "rabbitmq_dist": 15002})

    assert process.argv == [str(epmd)]
    assert process.env["ERL_EPMD_PORT"] == "15001"
    assert process.env["ERL_EPMD_ADDRESS"] == "127.0.0.1"


def test_a_service_that_dies_while_starting_fails_at_once(stack: Stack):
    process = python_process(stack, "import sys; sys.exit(3)")
    started = time.monotonic()
    with pytest.raises(StartupError, match="exited while starting"):
        stack.await_ready(process, lambda: False, timeout=60)
    assert time.monotonic() - started < 10


def test_a_stop_request_ends_the_wait_for_a_slow_service(stack: Stack):
    process = python_process(stack, "import time; time.sleep(60)")
    stack.stop_requested.set()
    started = time.monotonic()
    try:
        with pytest.raises(StartupError, match="cancelled"):
            stack.await_ready(process, lambda: False, timeout=60)
        assert time.monotonic() - started < 10
    finally:
        process.stop()


def test_a_service_that_never_answers_times_out(stack: Stack):
    process = python_process(stack, "import time; time.sleep(60)")
    try:
        with pytest.raises(StartupError, match="did not start"):
            stack.await_ready(process, lambda: False, timeout=1)
    finally:
        process.stop()


def test_a_ready_service_returns_normally(stack: Stack):
    process = python_process(stack, "import time; time.sleep(60)")
    try:
        stack.await_ready(process, lambda: True, timeout=5)
    finally:
        process.stop()


def test_a_stop_during_a_database_update_asks_the_shell_to_wait(stack: Stack, capsys):
    """The shell kills a runtime that is slow to stop, and a migration that
    is killed leaves a database Prisma refuses to touch."""
    stack.migrating = True
    stack.request_stop()

    event = json.loads(capsys.readouterr().out)
    assert stack.stop_requested.is_set()
    assert event["grace_seconds"] > bootstrap.MIGRATION_TIMEOUT_SECONDS


def test_an_ordinary_stop_asks_for_nothing(stack: Stack, capsys):
    stack.request_stop()
    assert capsys.readouterr().out == ""


def test_a_shell_that_is_gone_does_not_stop_the_runtime_reporting(monkeypatch, tmp_path: Path):
    with open(tmp_path / "pipe", "w") as closed:
        pass
    monkeypatch.setattr(sys, "stdout", closed)
    events.error("the executor keeps stopping", fatal=True)


def fake_initdb(monkeypatch) -> None:
    def run_tool(argv: list[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
        pgdata = Path(next(arg for arg in argv if arg.startswith("--pgdata=")).split("=", 1)[1])
        pgdata.mkdir(parents=True)
        (pgdata / "PG_VERSION").write_text("18")
        (pgdata / "postgresql.conf").write_text("")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(postgres, "run_tool", run_tool)


def test_a_first_start_that_was_cut_short_starts_over(stack: Stack, monkeypatch):
    """Nobody has used that database yet, and half-created it cannot be
    repaired, only refused."""
    fake_initdb(monkeypatch)
    bundle, data = stack.bundle, stack.data
    postgres.initialize(bundle, data, "password")
    (data.postgres / "half-applied-migration").write_text("")
    assert not postgres.is_initialized(data)

    postgres.initialize(bundle, data, "password")

    assert not (data.postgres / "half-applied-migration").exists()
    assert (data.postgres / "PG_VERSION").is_file()


def test_a_database_that_finished_its_first_start_is_never_recreated(stack: Stack, monkeypatch):
    fake_initdb(monkeypatch)
    bundle, data = stack.bundle, stack.data
    postgres.initialize(bundle, data, "password")
    postgres.first_run_completed(data)
    (data.postgres / "user-data").write_text("")

    assert postgres.is_initialized(data)
    postgres.initialize(bundle, data, "password")

    assert (data.postgres / "user-data").exists()


@pytest.mark.parametrize(("stored", "compatible"), [("18", True), ("16", False)])
def test_data_from_another_postgres_major_is_refused_with_an_explanation(
    stack: Stack, monkeypatch, stored: str, compatible: bool
):
    stack.data.postgres.mkdir()
    (stack.data.postgres / "PG_VERSION").write_text(stored)
    monkeypatch.setattr(
        postgres,
        "run_tool",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"postgres (PostgreSQL) 18.6", b""),
    )
    if compatible:
        postgres.check_compatible(stack.bundle, stack.data)
        return
    with pytest.raises(postgres.IncompatibleDataError, match="PostgreSQL 16"):
        postgres.check_compatible(stack.bundle, stack.data)


def test_a_cache_that_cannot_read_its_data_starts_empty(stack: Stack, monkeypatch):
    """Another build's file format, or a damaged file: Valkey exits at once.
    What it holds is expendable, so that must not keep the app from starting."""
    data = stack.data
    unreadable = data.valkey / "dump.rdb"
    unreadable.write_text("written by something else")
    cache = ManagedProcess(
        name="valkey",
        argv=[
            sys.executable,
            "-c",
            "import os, sys, time; sys.exit(1) if os.path.exists('dump.rdb') else time.sleep(60)",
        ],
        env={},
        cwd=data.valkey,
        log_dir=data.logs,
    )
    monkeypatch.setattr(valkey, "is_ready", lambda port, password: cache.exit_code() is None)
    stack.launch([cache])
    wait_until(lambda: cache.exit_code() is not None, 20, 0.1)
    try:
        stack.await_cache(cache, {"valkey": 20000, "valkey_bus": 20001}, "password")

        assert cache.exit_code() is None
        assert not unreadable.exists()
        assert (data.valkey / valkey.CONFIG_NAME).is_file()
        assert [path.name for path in data.root.glob("valkey.unreadable-*/dump.rdb")] == ["dump.rdb"]
    finally:
        cache.stop()


@pytest.fixture
def cache_home(tmp_path: Path, monkeypatch) -> None:
    """Keep the space-free aliases out of the real cache directory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "home" / ".cache"))


@pytest.mark.usefixtures("cache_home")
def test_rabbitmq_is_given_a_path_without_spaces(tmp_path: Path):
    """Its launch scripts break on a space, and macOS keeps application data
    under "Application Support"."""
    spaced = tmp_path / "Application Support" / "AutoGPT" / "rabbitmq"
    alias = Path(rabbitmq._short(spaced))
    (spaced / "marker").write_text("x")

    assert (alias / "marker").read_text() == "x"
    if not any(character.isspace() for character in str(tmp_path)):
        assert not any(character.isspace() for character in str(alias))
    assert rabbitmq._short(spaced) == str(alias)


@pytest.mark.usefixtures("cache_home")
def test_rabbitmq_scripts_are_run_through_the_alias(tmp_path: Path):
    bundle = Bundle(tmp_path / "My Apps" / "runtime")
    script = Path(rabbitmq._script(bundle, "rabbitmq-server"))
    assert script.parent.name == "sbin"
    if not any(character.isspace() for character in str(tmp_path)):
        assert not any(character.isspace() for character in str(script))
