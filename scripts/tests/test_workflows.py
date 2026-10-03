"""Properties of the workflow files on `main` that a later edit must not lose.

Read as text (the standard library has no YAML parser); no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import disable_upstream_workflows

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
MANUAL = (ROOT / "docs" / "MAINTAINING.md").read_text(encoding="utf-8")
USES = re.compile(r"^\s*(?:- )?uses: (\S+)(.*)$", re.MULTILINE)
PINNED = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")
TOKEN = "${{ secrets.FORK_SYNC_TOKEN }}"
NETWORK = re.compile(r"uses: |\b(gh|git|python3|uv|node) ")


def workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def job(text: str, name: str) -> str:
    """The lines of one job: from its key to the next key at the same depth."""
    match = re.search(
        rf"^  {name}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text, re.MULTILINE | re.DOTALL
    )
    assert match, f"no job named {name}"
    return match.group(1)


def without_comments(block: str) -> str:
    return "\n".join(
        line for line in block.splitlines() if not line.lstrip().startswith("#")
    )


def steps(block: str) -> list[str]:
    """The steps of a job, each starting at its name, comments removed."""
    return re.split(r"\n      - ", without_comments(block))[1:]


SYNC = workflow("sync-upstream.yml")
SYNC_JOBS = ("preflight", "token", "merge", "gate", "publish", "report")


def test_the_job_that_runs_upstream_code_holds_no_credential() -> None:
    gate = without_comments(job(SYNC, "gate"))
    for forbidden in ("secrets.", "github.token", "GH_TOKEN", "environment:"):
        assert forbidden not in gate, f"the gate job must not contain {forbidden!r}"
    assert not re.search(r": write\b", gate), "the gate job must be read-only"
    assert "permissions:\n      contents: read\n" in gate
    assert gate.count("actions/checkout@") == gate.count("persist-credentials: false")
    # No cache either: a cache written here would be restored by trusted jobs.
    assert "enable-cache: false" in gate
    assert "package-manager-cache: false" in gate


def test_the_push_token_reaches_only_the_job_that_pushes() -> None:
    assert without_comments(SYNC).count(TOKEN) == 1
    assert TOKEN in job(SYNC, "publish")
    with_environment = [
        name for name in SYNC_JOBS if "environment: sync" in job(SYNC, name)
    ]
    assert with_environment == ["token", "publish"]
    # The other jobs only ever learn whether a secret exists.
    for name in ("preflight", "token"):
        assert re.findall(r"secrets\.\w+[^}]*", job(SYNC, name)) == [
            "secrets.FORK_SYNC_TOKEN != '' "
        ]
    for name in ("merge", "gate", "report"):
        assert "secrets." not in without_comments(job(SYNC, name))


def test_the_job_that_pushes_takes_nothing_from_the_job_that_ran_the_tests() -> None:
    publish = without_comments(job(SYNC, "publish"))
    assert re.findall(r"needs\.gate\.(\w+)", publish) == ["result"]
    assert "download-artifact" not in publish
    for name in ("base_sha", "upstream_sha", "tree_sha"):
        assert f"needs.merge.outputs.{name}" in publish
    assert "--expect-tree" in publish


def test_the_gate_runs_the_same_tests_as_the_build() -> None:
    gate = job(SYNC, "gate")
    unit = job(workflow("desktop-build.yml"), "unit")
    packages = re.compile(r"--with (\S+)")
    assert packages.findall(gate), "the gate lists no packages"
    assert packages.findall(gate) == packages.findall(unit)
    for command in ('node --test "test/*.test.js"', "python -m pytest -q"):
        assert command in gate and command in unit


def test_every_gating_step_uses_bash_so_a_pipe_cannot_hide_a_failure() -> None:
    piped = [step for step in steps(job(SYNC, "gate")) if "| tee" in step]
    assert [step.splitlines()[0] for step in piped] == [
        "name: Gate - shell tests",
        "name: Gate - runtime tests",
    ]
    assert all("shell: bash\n" in step for step in piped)


def test_every_step_that_uses_the_network_has_its_own_time_limit() -> None:
    for name in SYNC_JOBS:
        for step in steps(job(SYNC, name)):
            if NETWORK.search(step):
                assert "timeout-minutes:" in step, (
                    f"{name}: step '{step.splitlines()[0]}' can hang until the job is "
                    "cancelled, and a cancelled job loses its report"
                )


def test_actions_are_pinned_to_a_commit_with_a_version_comment() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for target, rest in USES.findall(path.read_text(encoding="utf-8")):
            if target.startswith("./"):
                continue
            assert PINNED.fullmatch(target), f"{path.name}: {target} is not pinned"
            assert re.match(r" # v\d", rest), f"{path.name}: {target} has no version"


def test_third_party_actions_are_in_the_documented_allow_list() -> None:
    """MAINTAINING.md tells the owner to allow GitHub's own actions plus a list."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for target, _ in USES.findall(path.read_text(encoding="utf-8")):
            owner, _, rest = target.partition("/")
            if target.startswith("./") or owner == "actions":
                continue
            repository = f"{owner}/{rest.split('@')[0].split('/')[0]}"
            assert f"patterns_allowed[]={repository}@*" in MANUAL, (
                f"{path.name} uses {repository}: add it to the selected-actions "
                "command in docs/MAINTAINING.md and run that command"
            )


def test_the_build_reporter_listens_for_the_builds_by_their_real_names() -> None:
    reporter = workflow("build-report.yml")
    listened = re.findall(r"^      - (.+)$", reporter.split("types:")[0], re.MULTILINE)
    build_name = re.search(r"^name: (.+)$", workflow("desktop-build.yml"), re.MULTILINE)
    assert build_name and build_name.group(1) in listened
    assert "'.github/workflows/desktop-build.yml'" in reporter
    assert f"'{disable_upstream_workflows.DESKTOP_CALLER}'" in reporter
    for name in listened:
        assert f"`{name}`" in MANUAL, f"{name} is not explained in MAINTAINING.md"


def test_the_manual_names_every_workflow_on_main() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        assert f"`{path.name}`" in MANUAL, f"{path.name} is missing from MAINTAINING.md"
