"""The smoke test's own checks on the bundle and on what it leaves behind.

They only bite on a real stack; these make sure they would: that a new
directory, a leftover of an earlier run and a refused write are each seen,
and that the temporary data directory really goes.
"""

import importlib
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

DESKTOP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DESKTOP / "build"))
smoke_test = importlib.import_module("smoke_test")

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="permission bits are POSIX's")


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "frontend" / ".next" / "server").mkdir(parents=True)
    (root / "frontend" / "server.js").write_text("server")
    (root / "prisma" / "node_modules" / "@prisma" / "engines").mkdir(parents=True)
    return root


def test_a_directory_a_run_creates_is_a_change_even_when_empty(runtime: Path):
    before = settled(runtime)
    assert smoke_test.changed_files(before, smoke_test.snapshot(runtime)) == []

    (runtime / "frontend" / ".next" / "cache" / "images").mkdir(parents=True)

    (failure,) = smoke_test.changed_files(before, smoke_test.snapshot(runtime))
    # The two new directories, and the one they were created in.
    assert "changed 3 file(s) or directories" in failure
    assert os.path.join("frontend", ".next", "cache") + os.sep in failure


def test_a_file_that_came_and_went_during_the_run_is_a_change(runtime: Path):
    """A temporary file, a lock, a pid file: gone by the time the run is
    over, and a write an installed bundle would have refused."""
    before = settled(runtime)
    passing = runtime / "frontend" / ".next" / "server" / "x.tmp"
    make_later_than(runtime / "frontend" / ".next" / "server", lambda: passing.write_text("x"))
    passing.unlink()

    (failure,) = smoke_test.changed_files(before, smoke_test.snapshot(runtime))
    assert "changed 1 file(s) or directories" in failure
    assert os.path.join("frontend", ".next", "server") + os.sep in failure


def test_a_file_in_the_top_of_the_bundle_that_came_and_went_is_a_change(runtime: Path):
    before = settled(runtime)
    make_later_than(runtime, lambda: (runtime / "lock").write_text("x"))
    (runtime / "lock").unlink()
    assert len(smoke_test.changed_files(before, smoke_test.snapshot(runtime))) == 1


def settled(runtime: Path) -> dict:
    """A snapshot of a bundle that was not touched a moment ago. A real one
    was built minutes before it is run; here it was made in the same tick of
    a clock that, on Windows, moves every 16 ms, and a directory changed
    again within that tick keeps the time it had."""
    past = time.time() - 600
    for directory in [runtime, *(path for path in runtime.rglob("*") if path.is_dir())]:
        if not directory.is_symlink() and not is_junction(directory):
            os.utime(directory, (past, past))
    return smoke_test.snapshot(runtime)


def is_junction(path: Path) -> bool:
    return sys.platform == "win32" and bool(
        os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def make_later_than(directory: Path, change) -> None:
    """Make `change`, and make sure the directory's modification time is
    seen to move on a file system whose clock is coarser than this test."""
    was = directory.stat().st_mtime_ns
    change()
    if directory.stat().st_mtime_ns == was:
        os.utime(directory, ns=(was + 1_000_000, was + 1_000_000))


def test_a_link_a_run_creates_or_repoints_is_a_change(runtime: Path, tmp_path: Path):
    one, other = tmp_path / "one", tmp_path / "other"
    one.mkdir()
    other.mkdir()
    link = runtime / "frontend" / "link"
    make_link(link, one)
    before = settled(runtime)
    assert before[os.path.join("frontend", "link")][0] == "link"
    assert not any(name.startswith(os.path.join("frontend", "link") + os.sep) for name in before)

    remove_link(link)
    make_link(link, other)

    (failure,) = smoke_test.changed_files(before, smoke_test.snapshot(runtime))
    assert os.path.join("frontend", "link") in failure


@posix_only
def test_a_socket_or_a_pipe_a_run_leaves_is_a_change(runtime: Path):
    before = settled(runtime)
    os.mkfifo(runtime / "frontend" / "pipe")
    (failure,) = smoke_test.changed_files(before, smoke_test.snapshot(runtime))
    assert os.path.join("frontend", "pipe") in failure


def make_link(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        from autogpt_desktop import winlinks

        winlinks.create_junction(target, link)  # a symlink needs a privilege
    else:
        link.symlink_to(target, target_is_directory=True)


def remove_link(link: Path) -> None:
    if sys.platform == "win32":
        link.rmdir()
    else:
        link.unlink()


def test_a_file_added_changed_or_removed_is_a_change(runtime: Path):
    before = settled(runtime)
    (runtime / "frontend" / "server.js").write_text("rewritten")
    (runtime / "prisma" / "libquery_engine.so.node").write_text("downloaded")
    (failure,) = smoke_test.changed_files(before, smoke_test.snapshot(runtime))
    # Both files, and the directory the new one is in.
    assert "changed 3 file(s)" in failure
    assert os.path.join("frontend", "server.js") in failure
    assert os.path.join("prisma", "libquery_engine.so.node") in failure

    again = settled(runtime)
    (runtime / "prisma" / "libquery_engine.so.node").unlink()
    (failure,) = smoke_test.changed_files(again, smoke_test.snapshot(runtime))
    assert os.path.join("prisma", "libquery_engine.so.node") in failure


def test_what_an_earlier_run_left_in_the_bundle_fails_the_run(runtime: Path, capsys):
    assert smoke_test.left_by_earlier_runs(runtime) == []

    (runtime / "frontend" / ".next" / "cache").mkdir()
    engines = runtime / "prisma" / "node_modules" / "@prisma" / "engines"
    (engines / "libquery_engine-debian-openssl-1.1.x.so.node").write_text("downloaded")

    (failure,) = smoke_test.left_by_earlier_runs(runtime)
    assert "libquery_engine-debian-openssl-1.1.x.so.node" in failure
    assert "cache" in failure and "--only seal" in failure


def test_a_log_that_speaks_of_a_refused_write_fails_a_read_only_run(tmp_path: Path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "api.log").write_text("INFO started\nINFO serving\n")
    assert smoke_test.refused_writes(tmp_path) == []

    (logs / "frontend.log").write_text(
        " ⨯ Failed to write image to cache abc Error: EACCES: permission denied, mkdir\n",
        encoding="utf-8",
    )
    (logs / "rabbitmq.log").write_text("could not open: Read-only file system\nagain: EROFS\n")

    failures = smoke_test.refused_writes(tmp_path)
    assert [failure.split(" ", 1)[0] for failure in failures] == ["frontend.log", "rabbitmq.log"]
    assert "2 line(s)" in failures[1]

    # The backend's own file logs are a folder further down (LOG_DIR), and
    # the runtime's is beside the services'.
    (logs / "backend").mkdir()
    (logs / "backend" / "rest.log").write_text("PermissionError: [Errno 13] Permission denied: 'x'\n")
    (logs / "runtime.log").write_text("OSError: [Errno 30] Read-only file system\n")
    names = [failure.split(" ", 1)[0] for failure in smoke_test.refused_writes(tmp_path)]
    assert names == ["backend/rest.log", "frontend.log", "rabbitmq.log", "runtime.log"]


def test_the_temporary_data_directory_is_removed_with_what_is_in_it(tmp_path: Path):
    data = tmp_path / "autogpt-smoke-x"
    (data / "postgres" / "base").mkdir(parents=True)
    (data / "postgres" / "PG_VERSION").write_text("18")
    (data / "rabbitmq").mkdir()
    cookie = data / "rabbitmq" / ".erlang.cookie"  # as rabbitmq.prepare writes it
    cookie.write_text("COOKIE")
    cookie.chmod(0o400)

    smoke_test.remove_data(data)

    assert not data.exists()
    smoke_test.remove_data(data)  # already gone: nothing to do, no error


def test_read_only_mode_is_refused_where_it_proves_nothing(monkeypatch):
    monkeypatch.setattr(smoke_test.sys, "platform", "win32")
    assert "macOS and Linux" in str(smoke_test.cannot_run_read_only())
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(smoke_test.sys, "platform", "linux")
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        assert "root" in str(smoke_test.cannot_run_read_only())
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        assert smoke_test.cannot_run_read_only() is None


@posix_only
def test_the_bundle_is_unwritable_for_the_run_and_as_it_was_afterwards(runtime: Path):
    script = runtime / "frontend" / "server.js"
    script.chmod(0o755)
    before = {path: stat.S_IMODE(path.stat().st_mode) for path in [runtime, *runtime.rglob("*")]}

    with pytest.raises(RuntimeError, match="the run failed"), smoke_test.unwritable(runtime):
        assert stat.S_IMODE(script.stat().st_mode) == 0o555
        if os.geteuid() != 0:
            with pytest.raises(PermissionError):
                (runtime / "frontend" / ".next" / "cache").mkdir()
            with pytest.raises(PermissionError):
                script.write_text("changed")
        raise RuntimeError("the run failed")

    after = {path: stat.S_IMODE(path.stat().st_mode) for path in [runtime, *runtime.rglob("*")]}
    assert after == before


@posix_only
def test_a_run_being_ended_can_put_the_permissions_back_first_and_only_once(runtime: Path):
    script = runtime / "frontend" / "server.js"
    with smoke_test.unwritable(runtime) as give_back:
        assert not os.access(runtime / "frontend", os.W_OK) or os.geteuid() == 0
        give_back()
        assert stat.S_IMODE(script.stat().st_mode) & 0o200
        script.chmod(0o400)  # what happens after is not undone a second time
        give_back()
    assert stat.S_IMODE(script.stat().st_mode) == 0o400


@pytest.fixture
def handlers():
    """This test process's own answers to the signals, put back afterwards."""
    before = {number: signal.getsignal(number) for number in smoke_test.END_SIGNALS}
    yield before
    for number, handler in before.items():
        signal.signal(number, handler)


def test_being_told_to_end_is_an_interrupt_so_that_everything_is_cleared_up(handlers):
    """Left to Python, SIGTERM ends the process where it stands: no
    `finally` runs, the data directory stays, a read-only bundle stays
    read-only."""
    smoke_test.end_on_signals()
    for number in smoke_test.END_SIGNALS:
        if number == signal.SIGINT:
            continue
        with pytest.raises(KeyboardInterrupt, match=signal.Signals(number).name):
            signal.getsignal(number)(number, None)


@posix_only
def test_a_signal_that_arrives_while_permissions_go_back_does_not_stop_that(handlers):
    smoke_test.end_on_signals()
    with smoke_test.deaf_to_signals():
        os.kill(os.getpid(), signal.SIGTERM)  # and no KeyboardInterrupt
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
    with pytest.raises(KeyboardInterrupt):  # heard again afterwards
        os.kill(os.getpid(), signal.SIGTERM)
        signal.pause()


# A stand-in runtime: starts a service that outlives it, says which, and
# stays until its stdin closes.
RUNTIME = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(child.pid, flush=True)
sys.stdin.read()
"""


def test_only_what_the_runtime_started_counts_as_left_running(tmp_path: Path):
    """The bundle's interpreter also runs the unit tests, perhaps right now
    and for somebody else: a process is a leftover because the runtime
    started it, not because of where its executable is."""
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    runtime = subprocess.Popen(
        [sys.executable, "-c", RUNTIME], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert runtime.stdout and runtime.stdin
        service = psutil.Process(int(runtime.stdout.readline()))
        family = smoke_test.Family(psutil.Process(runtime.pid), tmp_path, BUNDLE)
        family.look()
        runtime.stdin.close()
        runtime.wait(20)

        (failure,) = smoke_test.left_running(family)

        assert "processes left running" in failure
        assert not smoke_test.is_alive(service)  # ended, so the machine is left clean
        assert bystander.poll() is None
    finally:
        for process in (bystander, runtime):
            process.kill()
            process.wait(10)


def test_a_runtime_that_took_everything_with_it_left_nothing(tmp_path: Path):
    runtime = subprocess.Popen([sys.executable, "-c", "pass"])
    family = smoke_test.Family(psutil.Process(runtime.pid), tmp_path, BUNDLE)
    runtime.wait(20)
    assert smoke_test.left_running(family) == []


def test_a_process_that_left_the_family_before_it_was_seen_is_still_found(
    tmp_path: Path, monkeypatch
):
    """A daemon: by the time anyone looks, its parent has gone and it is
    nobody's child. It runs from the bundle, was started after the runtime,
    and inherited what the runtime was started with; the developer's tests,
    running from the same bundle, did not, and are left alone."""
    monkeypatch.setattr(smoke_test.Family, "look", lambda self: None)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    runtime = subprocess.Popen(
        [sys.executable, "-c", RUNTIME],
        env={**os.environ, "AUTOGPT_DESKTOP_DATA_DIR": str(tmp_path / "data")},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    daemon = None
    try:
        assert runtime.stdout and runtime.stdin
        daemon = psutil.Process(int(runtime.stdout.readline()))
        family = smoke_test.Family(psutil.Process(runtime.pid), tmp_path / "data", BUNDLE)
        runtime.stdin.close()
        runtime.wait(20)

        (failure,) = smoke_test.left_running(family)

        assert "processes left running" in failure
        assert not smoke_test.is_alive(daemon)
        assert bystander.poll() is None
    finally:
        for process in (bystander, runtime):
            process.kill()
            process.wait(10)
        if daemon and smoke_test.is_alive(daemon):
            daemon.kill()


def test_a_process_started_with_this_runs_data_directory_is_of_this_run(
    tmp_path: Path, monkeypatch
):
    """Whoever its parent is: here, this test, which is alive and no part of
    the run."""
    monkeypatch.setattr(smoke_test.Family, "look", lambda self: None)
    runtime = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    sleep = [sys.executable, "-c", "import time; time.sleep(120)"]
    family = smoke_test.Family(psutil.Process(runtime.pid), tmp_path / "data", BUNDLE)
    of_this_run = subprocess.Popen(sleep, env={**os.environ, "RABBITMQ_BASE": str(tmp_path / "data" / "rabbitmq")})
    bystander = subprocess.Popen(sleep, env={**os.environ, "RABBITMQ_BASE": str(tmp_path / "other")})
    try:
        runtime.communicate(timeout=20)

        (failure,) = smoke_test.left_running(family)

        assert "processes left running" in failure
        assert of_this_run.wait(10) is not None
        assert bystander.poll() is None
    finally:
        for process in (bystander, of_this_run, runtime):
            process.kill()
            process.wait(10)


def test_a_process_working_in_the_data_directory_is_of_this_run(tmp_path: Path):
    """Valkey is started with nothing but a working directory to tell."""
    (tmp_path / "data" / "valkey").mkdir(parents=True)
    sleep = [sys.executable, "-c", "import time; time.sleep(120)"]
    inside = subprocess.Popen(sleep, cwd=tmp_path / "data" / "valkey")
    outside = subprocess.Popen(sleep, cwd=tmp_path)
    try:
        marks = [str(tmp_path / "data")]
        assert smoke_test.mentions(psutil.Process(inside.pid), marks)
        assert not smoke_test.mentions(psutil.Process(outside.pid), marks)
    finally:
        for process in (inside, outside):
            process.kill()
            process.wait(10)


# Where this interpreter's image is, as the check sees it for the processes
# started below. Not sys.executable: a framework build on macOS (GitHub's
# runners have one) runs Python.app from another directory.
BUNDLE = Path(psutil.Process().exe()).resolve().parent


# --- the links a run leaves outside the data directory ------------------------


@pytest.fixture
def link_roots(tmp_path: Path, monkeypatch) -> list[Path]:
    roots = [tmp_path / "cache" / "AutoGPT" / "links", tmp_path / "shared" / "AutoGPT-user"]
    (tmp_path / "shared").mkdir()
    monkeypatch.setattr(smoke_test, "alias_roots", lambda: roots)
    return roots


def test_the_links_a_run_made_to_its_data_and_to_the_bundle_go_with_it(
    runtime: Path, tmp_path: Path, link_roots: list[Path]
):
    """With --data-dir too: the directory is the caller's, the link the
    runtime made to it is not."""
    data = tmp_path / "autogpt smoke"
    (data / "rabbitmq").mkdir(parents=True)
    (runtime / "erlang").mkdir()
    aliases = smoke_test.Aliases(runtime, data)

    for root in link_roots:
        root.mkdir(parents=True)
    make_link(link_roots[0] / "to-data", data / "rabbitmq")
    make_link(link_roots[0] / "to-erlang", runtime / "erlang")
    make_link(link_roots[1] / "to-data-too", data / "rabbitmq")
    aliases.remove_new()

    assert not any(os.path.lexists(root) for root in link_roots)
    assert not (tmp_path / "cache" / "AutoGPT").exists()  # made for the links, and empty
    assert (tmp_path / "shared").is_dir()
    assert (data / "rabbitmq").is_dir() and (runtime / "erlang").is_dir()


def test_links_that_were_there_or_lead_elsewhere_are_somebody_elses(
    runtime: Path, tmp_path: Path, link_roots: list[Path]
):
    data = tmp_path / "autogpt smoke"
    (data / "rabbitmq").mkdir(parents=True)
    installed = tmp_path / "the installed app's data"
    installed.mkdir()
    root = link_roots[0]
    root.mkdir(parents=True)
    make_link(root / "from-an-earlier-run", data / "rabbitmq")
    (root / "a-folder").mkdir()
    aliases = smoke_test.Aliases(runtime, data)

    make_link(root / "the-installed-apps", installed)  # it was started meanwhile
    make_link(root / "this-runs", data / "rabbitmq")
    aliases.remove_new()

    assert sorted(path.name for path in root.iterdir()) == [
        "a-folder",
        "from-an-earlier-run",
        "the-installed-apps",
    ]


def test_a_link_is_known_by_where_it_goes_not_by_how_the_path_is_spelled(tmp_path: Path):
    data = tmp_path / "data"
    (data / "rabbitmq").mkdir(parents=True)
    make_link(tmp_path / "another way in", tmp_path / "data")
    make_link(tmp_path / "link", tmp_path / "another way in" / "rabbitmq")

    assert smoke_test.leads_into(tmp_path / "link", data)
    assert not smoke_test.leads_into(tmp_path / "link", tmp_path / "elsewhere")


def test_a_stale_postmaster_pid_is_not_taken_for_this_runs_server(tmp_path: Path):
    """It names a process that is gone, or by now somebody else's, which
    must not be counted, let alone ended."""
    (tmp_path / "postgres").mkdir()
    (tmp_path / "postgres" / "postmaster.pid").write_text(f"{os.getppid()}\n")
    me = psutil.Process()
    assert smoke_test.nobodys_children(tmp_path, me) == []
    (tmp_path / "postgres" / "postmaster.pid").write_text("not a pid\n")
    assert smoke_test.nobodys_children(tmp_path, me) == []
