"""Properties of the workflow files on `main` that a later edit must not lose.

Read as text (the standard library has no YAML parser); no network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

import disable_upstream_workflows
import release_plan

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


BUILD = workflow("desktop-build.yml")
RELEASE = workflow("desktop-release.yml")
SECRET_VALUE = re.compile(r"\$\{\{ secrets\.(\w+) \}\}")


def step_name(step: str) -> str:
    return step.splitlines()[0].removeprefix("name: ")


def named_step(block: str, name: str) -> str:
    (found,) = [step for step in steps(block) if step_name(step) == name]
    return found


def script(step: str) -> str:
    """The shell script of a step whose `run` is a block."""
    return textwrap.dedent(step.split("run: |\n", 1)[1])


def bash() -> str:
    """A bash that is not WSL's launcher, or skip."""
    found = shutil.which("bash")
    if not found or "system32" in found.lower():
        pytest.skip("no bash to run the workflow's scripts with")
    return found


SIGN_HOLDERS = {
    "Package (macOS, Developer ID, notarized)",
    "Package (Windows, Azure Trusted Signing)",
    "Package (Windows, certificate file)",
}


def test_signing_secrets_reach_only_the_job_that_signs() -> None:
    holders = {
        step_name(step)
        for step in steps(job(BUILD, "sign"))
        if SECRET_VALUE.search(step)
    }
    assert holders == SIGN_HOLDERS
    for name in ("unit", "build", "e2e"):
        assert "secrets." not in without_comments(job(BUILD, name)), name
    # Anything written there is handed to every later step of the job.
    for step in steps(job(BUILD, "sign")):
        assert "GITHUB_ENV" not in step and "GITHUB_PATH" not in step, step_name(step)


def test_the_job_that_runs_the_build_is_outside_the_signing_environment() -> None:
    """Steps of one job share a machine: a secret given to a later step is
    within reach of everything an earlier step ran."""
    build = without_comments(job(BUILD, "build"))
    assert "environment:" not in build
    for marker in ("build/build_runtime.py", "build/smoke_test.py", "npm ci"):
        assert marker in build
    with_environment = [
        name
        for name in ("unit", "gate", "build", "sign", "e2e")
        if "environment:" in without_comments(job(BUILD, name))
    ]
    assert with_environment == ["gate", "sign"]
    for name in with_environment:
        assert "    environment: desktop-release\n" in job(BUILD, name)


def test_the_job_that_signs_runs_nothing_but_the_packaging_tools() -> None:
    sign = without_comments(job(BUILD, "sign"))
    for forbidden in (
        "build_runtime.py",
        "smoke_test.py",
        "pnpm",
        "uv run",
        "uvx",
        "node --test",
        "build/runtime/python",
        # A cache is written by jobs that ran third-party code.
        "actions/cache@",
        "enable-cache: true",
    ):
        assert forbidden not in sign, f"the sign job must not contain {forbidden!r}"
    assert "package-manager-cache: false" in sign
    assert sign.count("actions/checkout@") == sign.count("persist-credentials: false")
    installs = [line.strip() for line in sign.splitlines() if "npm ci" in line]
    assert installs == ["run: npm ci --no-audit --no-fund --ignore-scripts"]
    names = [step_name(step) for step in steps(job(BUILD, "sign"))]
    # The archive is checked before anything is installed or packaged.
    unpack = names.index("Unpack the runtime")
    assert unpack < names.index("Install the packaging tools")
    assert all(unpack < names.index(holder) for holder in SIGN_HOLDERS)
    assert "git status --porcelain --ignored" in named_step(
        job(BUILD, "sign"), "Unpack the runtime"
    )


def test_a_step_that_holds_a_secret_only_packages() -> None:
    for step in steps(job(BUILD, "sign")):
        if not SECRET_VALUE.search(step):
            continue
        commands = [line.strip() for line in step.split("run:", 1)[1].splitlines()]
        assert "npm ci" not in step, f"{step_name(step)} installs packages"
        assert sum("electron-builder" in line for line in commands) == 1
        assert "--publish never" in step


def test_the_gate_learns_only_whether_a_secret_exists() -> None:
    gate_job = without_comments(job(BUILD, "gate"))
    assert "    if: inputs.sign\n" in gate_job
    # It runs its own lines and nothing else: no checkout, no action.
    assert "uses:" not in gate_job
    (gate,) = steps(job(BUILD, "gate"))
    assert not SECRET_VALUE.search(gate)
    expressions = re.findall(r"^\s+(\w+): \$\{\{ (.*) \}\}$", gate, re.MULTILINE)
    assert len(expressions) >= 14
    for variable, expression in expressions:
        match = re.fullmatch(r"(?:secrets|vars)\.(\w+) != ''", expression)
        assert match, expression
        assert variable == f"HAS_{match.group(1)}"


SIGNING_SETS = {
    "apple": (
        "MAC_CSC_LINK",
        "MAC_CSC_KEY_PASSWORD",
        "APPLE_API_KEY_P8",
        "APPLE_API_KEY_ID",
        "APPLE_API_ISSUER",
    ),
    "azure": (
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_SIGN_ENDPOINT",
        "AZURE_SIGN_ACCOUNT",
        "AZURE_SIGN_PROFILE",
        "AZURE_SIGN_PUBLISHER",
    ),
    "pfx": ("WIN_CSC_LINK", "WIN_CSC_KEY_PASSWORD"),
}


def run_gate(tmp_path: Path, present: set[str]) -> tuple[int, dict[str, str], str]:
    """Runs the gate's script as the runner would, with these secrets and
    variables existing. Returns its exit code, its outputs and what it printed."""
    gate = named_step(job(BUILD, "gate"), "Signing gate")
    names = re.findall(r"^\s+HAS_(\w+): ", gate, re.MULTILINE)
    assert present <= set(names), present - set(names)
    program = tmp_path / "gate.sh"
    program.write_text(script(gate), encoding="utf-8", newline="\n")
    outputs = tmp_path / "outputs"
    outputs.write_text("", encoding="utf-8")
    environment = {
        "PATH": os.environ["PATH"],
        "GITHUB_OUTPUT": outputs.as_posix(),
        "GITHUB_STEP_SUMMARY": (tmp_path / "summary").as_posix(),
        **{f"HAS_{name}": str(name in present).lower() for name in names},
    }
    done = subprocess.run(
        [bash(), "--noprofile", "--norc", "-eo", "pipefail", program.as_posix()],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    pairs = (line.split("=", 1) for line in outputs.read_text().splitlines())
    return done.returncode, dict(pairs), done.stdout


def test_the_gate_knows_every_secret_and_variable_of_each_set() -> None:
    gate = named_step(job(BUILD, "gate"), "Signing gate")
    known = set(re.findall(r"^\s+HAS_(\w+): ", gate, re.MULTILINE))
    expected = {name for names in SIGNING_SETS.values() for name in names}
    assert known - expected == {"CSC_LINK", "CSC_KEY_PASSWORD", "APPLE_API_KEY"}
    assert expected <= known
    # Every value the sign job hands to electron-builder is one the gate asked about.
    used = set(
        re.findall(r"(?:secrets|vars)\.(\w+)", without_comments(job(BUILD, "sign")))
    )
    assert used == expected


def test_with_no_secret_nothing_is_signed_and_nothing_is_refused(
    tmp_path: Path,
) -> None:
    code, outputs, printed = run_gate(tmp_path, set())
    assert code == 0, printed
    assert outputs["mac"] == "" and outputs["win"] == ""
    assert json.loads(outputs["legs"]) == []
    assert "no certificate is configured" in outputs["mac_description"]
    assert "no certificate is configured" in outputs["win_description"]


def test_complete_sets_are_signed_by_the_matching_legs(tmp_path: Path) -> None:
    code, outputs, printed = run_gate(tmp_path, set(SIGNING_SETS["apple"]))
    assert code == 0, printed
    assert (outputs["mac"], outputs["win"]) == ("developer-id", "")
    (leg,) = json.loads(outputs["legs"])
    assert leg["mode"] == "developer-id" and leg["key"] == "macos-arm64"
    assert leg["description"] == outputs["mac_description"]
    assert "Developer ID" in leg["description"]

    everything = {name for names in SIGNING_SETS.values() for name in names}
    code, outputs, printed = run_gate(tmp_path, everything)
    assert code == 0, printed
    # Azure wins over a certificate file.
    assert (outputs["mac"], outputs["win"]) == ("developer-id", "azure")
    assert [leg["mode"] for leg in json.loads(outputs["legs"])] == [
        "developer-id",
        "azure",
    ]

    code, outputs, printed = run_gate(tmp_path, set(SIGNING_SETS["pfx"]))
    assert code == 0, printed
    assert (outputs["mac"], outputs["win"]) == ("", "pfx")
    assert [leg["key"] for leg in json.loads(outputs["legs"])] == ["windows-x64"]


@pytest.mark.parametrize("kind", sorted(SIGNING_SETS))
def test_any_partly_configured_set_is_refused(tmp_path: Path, kind: str) -> None:
    """Every way of having some but not all of a set, not only the ones that
    begin with its first secret."""
    names = SIGNING_SETS[kind]
    others = {
        name for other, group in SIGNING_SETS.items() if other != kind for name in group
    }
    for missing in names:
        for present in (set(names) - {missing}, {missing}):
            if present == set(names):
                continue
            # Also when the other sets are complete.
            for background in (set(), others):
                code, outputs, printed = run_gate(tmp_path, present | background)
                assert code == 1, f"{sorted(present)} was accepted:\n{printed}"
                assert "Signing is half configured" in printed
                assert "legs" not in outputs
                for name in set(names) - present:
                    assert name in printed


def test_a_certificate_under_electron_builders_own_name_is_refused(
    tmp_path: Path,
) -> None:
    for name in ("CSC_LINK", "CSC_KEY_PASSWORD", "APPLE_API_KEY"):
        code, _, printed = run_gate(tmp_path, {name})
        assert code == 1 and "MAC_CSC_LINK" in printed, printed


def test_the_legs_the_gate_names_are_legs_of_the_build() -> None:
    gate = named_step(job(BUILD, "gate"), "Signing gate")
    build = job(BUILD, "build")
    legs = re.findall(r'"key":"([\w-]+)","os":"([\w.-]+)"', gate)
    assert sorted(key for key, _ in legs) == ["macos-arm64", "windows-x64"]
    for key, runner in legs:
        assert re.search(rf"key: {key}\n\s+os: {runner}\n", build), (key, runner)
    for signer in ("mac", "win"):
        assert f"signer: {signer}\n" in build
        assert f"      {signer}: ${{{{ steps.gate.outputs.{signer} }}}}" in job(
            BUILD, "gate"
        )
    assert "SIGNED_LATER: ${{ needs.gate.outputs[matrix.signer] }}" in build
    assert "include: ${{ fromJSON(needs.gate.outputs.legs) }}" in job(BUILD, "sign")


def test_each_leg_is_packaged_by_exactly_one_job() -> None:
    """Two jobs uploading `installers-<key>` would fail the second; none would
    leave the release without that system."""
    build_steps = steps(job(BUILD, "build"))
    for name in ("Package (no certificate)", "Upload installers"):
        assert "if: env.SIGNED_LATER == ''" in named_step(job(BUILD, "build"), name)
    handed = [step_name(s) for s in build_steps if "if: env.SIGNED_LATER != ''" in s]
    assert handed == [
        "Pack the runtime for the signing job",
        "Hand the runtime to the signing job",
        "Free the disk the archive took",
    ]
    sign = job(BUILD, "sign")
    assert "name: runtime-${{ matrix.key }}" in named_step(sign, "Download the runtime")
    assert "name: installers-${{ matrix.key }}" in named_step(sign, "Upload installers")
    assert "needs: [build, sign]" in job(BUILD, "e2e")


VERSIONS = ("0.0.0", "1.2.3", "10.20.30", "1.2.3-rc.1", "1.2.3-rc.10", "1.2.3-0a.x-y")
NOT_VERSIONS = (
    "1.2",
    "v1.2.3",
    "1.2.3+build",
    "1.2.3-",
    "1.2.3-rc..1",
    # Leading zeros: electron-updater refuses them in an installed app.
    "01.2.3",
    "1.02.3",
    "1.2.03",
    "1.2.3-rc.01",
    "1.2.3-00",
)


def version_step(tmp_path: Path, requested: str) -> tuple[int, dict[str, str]]:
    program = tmp_path / "version.sh"
    program.write_text(
        script(named_step(job(BUILD, "build"), "Version")),
        encoding="utf-8",
        newline="\n",
    )
    outputs = tmp_path / "version-outputs"
    outputs.write_text("", encoding="utf-8")
    done = subprocess.run(
        [bash(), "--noprofile", "--norc", "-eo", "pipefail", program.as_posix()],
        env={
            "PATH": os.environ["PATH"],
            "GITHUB_OUTPUT": outputs.as_posix(),
            "GITHUB_RUN_NUMBER": "412",
            "REQUESTED": requested,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    pairs = (line.split("=", 1) for line in outputs.read_text().splitlines())
    return done.returncode, dict(pairs)


def test_the_build_takes_the_versions_the_release_plan_takes(tmp_path: Path) -> None:
    assert version_step(tmp_path, "") == (
        0,
        {"version": "0.0.0-dev.412", "next": "0.0.1-dev.412"},
    )
    assert version_step(tmp_path, "1.2.9-rc.1")[1]["next"] == "1.2.10-rc.1"
    for good in VERSIONS:
        assert version_step(tmp_path, good)[0] == 0, good
        release_plan.parse_version(good)
    for bad in NOT_VERSIONS:
        assert version_step(tmp_path, bad)[0] == 1, bad
        with pytest.raises(release_plan.Refused):
            release_plan.parse_version(bad)


def test_an_empty_certificate_is_never_handed_to_electron_builder() -> None:
    """electron-builder treats an empty CSC_LINK as a certificate."""
    for step in steps(job(BUILD, "sign")):
        for variable in ("CSC_LINK", "WIN_CSC_LINK"):
            if re.search(rf"^\s+{variable}:", step, re.MULTILINE):
                assert re.search(r"if: matrix\.mode == '(developer-id|pfx)'", step)
    unsigned = named_step(job(BUILD, "build"), "Package (no certificate)")
    # Without this electron-builder skips the ad-hoc macOS signature on pull requests.
    assert 'CSC_FOR_PULL_REQUEST: "true"' in unsigned
    assert 'CSC_IDENTITY_AUTO_DISCOVERY: "false"' in unsigned


def test_only_a_release_asks_for_signing_and_only_in_the_release_environment() -> None:
    dispatch = BUILD.split("  workflow_dispatch:")[1].split("\npermissions:")[0]
    assert "sign:" not in dispatch, "a hand-started build must not be able to sign"
    callers = {
        path.name: path.read_text(encoding="utf-8")
        for path in WORKFLOWS.glob("*.yml")
        if "uses: ./.github/workflows/desktop-build.yml"
        in path.read_text(encoding="utf-8")
    }
    assert sorted(callers) == ["desktop-nightly.yml", "desktop-release.yml"]
    assert "sign: true" in callers["desktop-release.yml"]
    for word in ("sign:", "secrets:", "environment:"):
        assert word not in without_comments(callers["desktop-nightly.yml"])
    assert "environment: desktop-release" not in without_comments(RELEASE)
    assert "`desktop-release`" in MANUAL
    # Without `sign` the gate is skipped, and the sign job with it.
    assert "needs.gate.result == 'success'" in job(BUILD, "sign")


def test_the_job_that_publishes_runs_nothing_that_was_built() -> None:
    assert "permissions: {}" in RELEASE
    writers = [
        name
        for name in ("plan", "build", "release")
        if re.search(r": write\b", without_comments(job(RELEASE, name)))
    ]
    assert writers == ["release"]
    release = without_comments(job(RELEASE, "release"))
    assert "needs: [plan, build]" in release
    assert "actions/checkout@" not in release
    for program in ("npm ", "npx ", "node ", "uv ", "python3 ", "bash out/", "chmod "):
        assert program not in release, f"the release job runs {program.strip()}"


def test_a_release_is_a_draft_until_its_files_are_checked() -> None:
    names = [step_name(step) for step in steps(job(RELEASE, "release"))]
    order = [
        "Check that the release is complete and consistent",
        "Attest where the files were built",
        "Create the draft",
        "Upload the files",
        "Check the uploaded files",
        "Publish",
    ]
    assert [name for name in names if name in order] == order
    assert names[-1] == "Publish"
    release = job(RELEASE, "release")
    assert "--draft " in release and "--draft=false" in release
    # The tag goes on main; a tag on desktop would start upstream's release workflows.
    assert '--target "${GITHUB_SHA}"' in release
    assert "refs/heads/main" in job(RELEASE, "plan")


def test_the_release_builds_the_commit_that_was_checked_and_tests_it() -> None:
    build = job(RELEASE, "build")
    assert "ref: ${{ needs.plan.outputs.sha }}" in build
    assert "e2e: true" in build
    assert "version: ${{ inputs.version }}" in build
    assert "release_files: true" in build


def test_the_manual_names_every_secret_and_variable_the_build_reads() -> None:
    names = set(re.findall(r"(?:secrets|vars)\.(\w+)", without_comments(BUILD)))
    assert names, "the workflow reads no secret"
    for name in sorted(names):
        assert f"`{name}`" in MANUAL, f"{name} is not explained in MAINTAINING.md"
