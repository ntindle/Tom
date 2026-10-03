import sys
import time
from pathlib import Path

import pytest

from autogpt_desktop import rabbitmq
from autogpt_desktop.layout import Bundle, DataDir
from autogpt_desktop.process import ManagedProcess
from autogpt_desktop.supervisor import Stack, StartupError


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


def test_rabbitmq_scripts_are_run_through_the_alias(tmp_path: Path):
    bundle = Bundle(tmp_path / "My Apps" / "runtime")
    script = Path(rabbitmq._script(bundle, "rabbitmq-server"))
    assert script.parent.name == "sbin"
    if not any(character.isspace() for character in str(tmp_path)):
        assert not any(character.isspace() for character in str(script))
