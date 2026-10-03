"""Variants: experiment branches built into apps that install next to the
normal one (docs/MAINTAINING.md, "Variants"). What the automation on `main`
must keep true: a variant branch is never built as the normal app, the names
come from the code that is built, and a variant's release can never become
what the normal app updates to. No network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import release_plan
from test_release_plan import MAIN_ONLY, SHA, FakeGitHub
from test_workflows import (
    BUILD,
    MANUAL,
    RELEASE,
    bash,
    job,
    named_step,
    script,
    step_name,
    steps,
    without_comments,
    workflow,
)

SLUGS = ("voice", "voice-2", "local-models", "a", "0", "v1", "x" * 24, "a-b-c")
NOT_SLUGS = (
    "Voice",
    "-voice",
    "voice-",
    "-",
    "voice_2",
    "voice.2",
    "voice 2",
    "voice/2",
    "x" * 25,
    # The folders of other installs' update downloads.
    "updater",
    "voice-updater",
)
NAMES = "autogpt_platform/desktop/src/identity.js"
# What src/identity.js on `desktop` prints, in miniature: the real one is on
# another branch, and this job must take whatever names it is given.
STUB = """\
const variant = process.env.AUTOGPT_DESKTOP_VARIANT || "";
const names = variant
  ? {
      variant,
      product_name: `AutoGPT (${variant})`,
      product_filename: `autogpt-${variant}`,
      package_name: `autogpt-${variant}`,
      dir_name: `AutoGPT-${variant}`,
      artifact_base: `AutoGPT-${variant}`,
      tag_prefix: `desktop-${variant}-v`,
      branch: `variant/${variant}`,
    }
  : {
      variant: "",
      product_name: "AutoGPT",
      product_filename: "AutoGPT",
      package_name: "autogpt",
      dir_name: "AutoGPT",
      artifact_base: "AutoGPT",
      tag_prefix: "desktop-v",
      branch: "desktop",
    };
for (const [key, value] of Object.entries(names)) process.stdout.write(`${key}=${value}\\n`);
"""
NORMAL = {
    "variant": "",
    "product_name": "AutoGPT",
    "product_filename": "AutoGPT",
    "package_name": "autogpt",
    "dir_name": "AutoGPT",
    "artifact_base": "AutoGPT",
    "tag_prefix": "desktop-v",
    "branch": "desktop",
    "suffix": "",
}


CHECK = "Check that the normal app is built from desktop"
# Stands in for `gh`: answers the one question the job asks GitHub, how
# `desktop` stands to the commit being built, and records that it was asked.
GH = """\
#!/usr/bin/env bash
echo "$*" >> "${GH_ASKED}"
[ -n "${GH_ANSWER}" ] || exit 1
echo "${GH_ANSWER}"
"""


def asked_github(tmp_path: Path) -> list[str]:
    log = tmp_path / "gh-asked"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def name_the_app(
    tmp_path: Path,
    variant: str,
    branch: str,
    names: str | None = None,
    *,
    named_branch: str = "",
    desktop_is: str = "identical",
) -> tuple[int, dict[str, str], str]:
    """Runs the identity job's two scripts, one after the other as the job
    does, in a checkout that has `names` as its src/identity.js, or none.
    `branch` is what the job is given as the branch or, failing that, the
    ref; `named_branch` is the branch a caller named. `desktop_is` is
    GitHub's answer about `desktop` relative to the commit ("" for no
    answer). Returns the exit code, the outputs and what was printed."""
    checkout = tmp_path / "checkout"
    if not checkout.exists():
        checkout.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "init", "-q"], cwd=checkout, check=True)
        commit = ["-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "x"]
        subprocess.run([*git, *commit], cwd=checkout, check=True)
    shutil.rmtree(checkout / "autogpt_platform", ignore_errors=True)
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    (tools / "gh").write_text(GH, encoding="utf-8", newline="\n")
    (tools / "gh").chmod(0o755)
    (tmp_path / "gh-asked").unlink(missing_ok=True)
    if names is not None:
        if not shutil.which("node"):
            pytest.skip("no node to run the stand-in for src/identity.js with")
        target = checkout / NAMES
        target.parent.mkdir(parents=True)
        target.write_text(names, encoding="utf-8", newline="\n")
    outputs = tmp_path / "outputs"
    outputs.write_text("", encoding="utf-8")
    environment = {
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": outputs.as_posix(),
        "GITHUB_STEP_SUMMARY": (tmp_path / "summary").as_posix(),
        "GITHUB_REPOSITORY": "owner/name",
        "GH_ASKED": (tmp_path / "gh-asked").as_posix(),
        "GH_ANSWER": desktop_is,
        "VARIANT": variant,
        "BRANCH": named_branch or branch,
    }
    printed = ""
    code = 0
    for name in (CHECK, "Name the app"):
        program = tmp_path / "step.sh"
        step = named_step(job(BUILD, "identity"), name)
        program.write_text(script(step), encoding="utf-8", newline="\n")
        # Only the first step is told which branch a caller named.
        given = {"NAMED_BRANCH": named_branch} if name == CHECK else {}
        done = subprocess.run(
            [bash(), "--noprofile", "--norc", "-eo", "pipefail", program.as_posix()],
            cwd=checkout,
            env={**environment, **given},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        printed += done.stdout + done.stderr
        code = done.returncode
        if code != 0:
            break
    pairs = (line.split("=", 1) for line in outputs.read_text().splitlines())
    return code, dict(pairs), printed


def test_a_commit_from_before_variants_is_the_normal_app(tmp_path: Path) -> None:
    for branch in ("desktop", "a" * 40):
        code, outputs, printed = name_the_app(tmp_path, "", branch)
        assert code == 0, printed
        assert outputs == NORMAL
    code, outputs, printed = name_the_app(tmp_path, "voice", "desktop")
    assert code == 1 and "older than variants" in printed
    assert outputs == {}


def head(tmp_path: Path) -> str:
    done = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path / "checkout",
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_only_a_commit_of_desktop_is_built_as_the_normal_app(tmp_path: Path) -> None:
    """A commit of a variant branch can be named without its branch: by its
    SHA, or by a tag. Built as the normal app it would replace the normal
    install and open its data."""
    for ref in ("desktop", "a" * 40, "desktop-v1.2.3"):
        for standing in ("identical", "ahead"):
            code, outputs, printed = name_the_app(tmp_path, "", ref, STUB, desktop_is=standing)
            assert code == 0, printed
            assert outputs == NORMAL
            assert asked_github(tmp_path) == [
                f"api repos/owner/name/compare/{head(tmp_path)}...desktop --jq .status"
            ]
    for ref in ("a" * 40, "desktop-voice-v0.3.0", "some-branch"):
        for standing in ("diverged", "behind"):
            code, outputs, printed = name_the_app(tmp_path, "", ref, STUB, desktop_is=standing)
            assert code == 1, printed
            assert "only desktop is built as the normal app" in printed
            assert "::error title=Variant refused::" in printed
            assert outputs == {}
    # No answer is not a yes.
    code, outputs, printed = name_the_app(tmp_path, "", "a" * 40, STUB, desktop_is="")
    assert code == 1 and "Could not find out" in printed, printed
    assert outputs == {}


def test_a_commit_that_is_not_on_desktop_is_built_under_a_name(tmp_path: Path) -> None:
    """As a variant, whatever the commit; and a pull request is built as what
    the branch it is to be merged into is, which the caller workflow names."""
    code, outputs, printed = name_the_app(tmp_path, "voice", "a" * 40, STUB, desktop_is="diverged")
    assert code == 0 and outputs["variant"] == "voice", printed
    assert asked_github(tmp_path) == []
    code, outputs, printed = name_the_app(
        tmp_path, "", "a" * 40, STUB, named_branch="desktop", desktop_is="diverged"
    )
    assert code == 0 and outputs == NORMAL, printed
    assert asked_github(tmp_path) == []
    code, outputs, printed = name_the_app(
        tmp_path, "", "a" * 40, STUB, named_branch="variant/voice", desktop_is="diverged"
    )
    assert code == 0 and outputs["variant"] == "voice", printed
    check = named_step(job(BUILD, "identity"), CHECK)
    assert "NAMED_BRANCH: ${{ inputs.branch }}" in check
    assert "BRANCH: ${{ inputs.branch || inputs.ref }}" in check
    assert "VARIANT: ${{ inputs.variant }}" in check
    # Only a workflow that calls this one can name a branch; a person
    # starting a build cannot.
    dispatch = BUILD.split("  workflow_dispatch:")[1].split("\npermissions:")[0]
    assert "      branch:\n" not in dispatch


def test_the_full_name_of_a_variant_branch_is_that_variant(tmp_path: Path) -> None:
    for spelling in ("refs/heads/variant/voice", "variant/voice"):
        code, outputs, printed = name_the_app(tmp_path, "", spelling, STUB, desktop_is="diverged")
        assert code == 0, printed
        assert (outputs["variant"], outputs["suffix"]) == ("voice", "-voice")
        code, _, printed = name_the_app(tmp_path, "lab", spelling, STUB)
        assert code == 1 and "cannot be built as 'lab'" in printed
    code, outputs, printed = name_the_app(
        tmp_path, "", "a" * 40, STUB, named_branch="refs/heads/variant/voice"
    )
    assert code == 0 and outputs["variant"] == "voice", printed


def test_the_names_are_the_ones_the_built_commit_gives(tmp_path: Path) -> None:
    code, outputs, printed = name_the_app(tmp_path, "", "desktop", STUB)
    assert code == 0, printed
    assert outputs == NORMAL
    code, outputs, printed = name_the_app(tmp_path, "voice", "desktop", STUB)
    assert code == 0, printed
    assert outputs == {
        "variant": "voice",
        "product_name": "AutoGPT (voice)",
        "product_filename": "autogpt-voice",
        "package_name": "autogpt-voice",
        "dir_name": "AutoGPT-voice",
        "artifact_base": "AutoGPT-voice",
        "tag_prefix": "desktop-voice-v",
        "branch": "variant/voice",
        "suffix": "-voice",
    }


def test_a_variant_branch_is_built_as_that_variant_and_as_nothing_else(
    tmp_path: Path,
) -> None:
    """Built as the normal app, an experiment would open the normal app's data."""
    code, outputs, printed = name_the_app(tmp_path, "", "variant/voice", STUB)
    assert code == 0, printed
    assert (outputs["variant"], outputs["suffix"]) == ("voice", "-voice")
    code, outputs, printed = name_the_app(tmp_path, "voice", "variant/voice", STUB)
    assert code == 0 and outputs["variant"] == "voice", printed
    code, outputs, printed = name_the_app(tmp_path, "lab", "variant/voice", STUB)
    assert code == 1 and "cannot be built as 'lab'" in printed
    assert outputs == {}
    # What the caller on a variant branch hands over is the branch, not a slug.
    assert "BRANCH: ${{ inputs.branch || inputs.ref }}" in job(BUILD, "identity")
    assert "VARIANT: ${{ inputs.variant }}" in job(BUILD, "identity")


def test_the_build_and_the_release_plan_take_the_same_slugs(tmp_path: Path) -> None:
    for slug in SLUGS:
        # Past the slug check: refused only for the missing file.
        code, _, printed = name_the_app(tmp_path, slug, "desktop")
        assert code == 1 and "older than variants" in printed, slug
        code, _, printed = name_the_app(tmp_path, "", f"variant/{slug}")
        assert code == 1 and "older than variants" in printed, slug
        assert release_plan.tag_prefix(slug) == f"desktop-{slug}-v"
    for slug in NOT_SLUGS:
        code, outputs, printed = name_the_app(tmp_path, slug, "desktop")
        assert code == 1 and "cannot name a variant" in printed, slug
        assert outputs == {}
        code, _, printed = name_the_app(tmp_path, "", f"variant/{slug}")
        assert code == 1 and "cannot name a variant" in printed, slug
        with pytest.raises(release_plan.Refused, match="cannot name a variant"):
            release_plan.tag_prefix(slug)


@pytest.mark.parametrize(
    "printed_by_the_commit",
    [
        # Another app than the one asked for.
        STUB.replace('process.env.AUTOGPT_DESKTOP_VARIANT || ""', '"lab"'),
        # A second line smuggled into an output.
        STUB.replace("`${key}=${value}\\n`", "`${key}=${value}\\nsuffix=;rm\\n`"),
        STUB.replace("`AutoGPT-${variant}`", "`AutoGPT-${variant}$(id)`"),
        STUB.replace("tag_prefix: `desktop-${variant}-v`,", ""),
        "process.exit(3);\n",
    ],
)
def test_names_that_are_not_names_stop_the_build(
    tmp_path: Path, printed_by_the_commit: str
) -> None:
    code, outputs, printed = name_the_app(
        tmp_path, "voice", "variant/voice", printed_by_the_commit
    )
    assert code != 0, printed
    assert outputs == {}


def test_a_name_from_the_built_commit_never_becomes_part_of_a_script() -> None:
    """They are text from the branch being built: handed to a script as a
    variable, or used where the runner takes a plain string."""
    for text, jobs in ((BUILD, ("build", "sign", "e2e")), (RELEASE, ("release",))):
        for name in jobs:
            for step in steps(job(text, name)):
                if "run:" not in step:
                    continue
                commands = step.split("run:", 1)[1]
                assert "outputs.suffix" not in commands, step_name(step)
                assert "needs.identity" not in commands, step_name(step)
                assert "needs.build.outputs" not in commands, step_name(step)


def test_the_variant_is_told_to_the_steps_that_need_it_and_to_no_other() -> None:
    told = "AUTOGPT_DESKTOP_VARIANT: ${{ needs.identity.outputs.variant }}"
    build = job(BUILD, "build")
    packaging = [
        step for step in steps(build) if "npx electron-builder" in step
    ]
    assert [step_name(step) for step in packaging] == [
        "Package (no certificate)",
        "Package the next version for the upgrade test",
    ]
    assert all(told in step for step in packaging)
    # Besides those, only the step that asks the app's own code about the
    # files: the job's shell tests read the configuration as the normal app's.
    assert told in named_step(build, UPDATE_CHECK)
    assert without_comments(build).count(told) == len(packaging) + 1
    # The unit tests: the shell's are not told either. The runtime's are, for
    # the test that holds `desktop` to upstream's tree and must let a variant be.
    unit = job(BUILD, "unit")
    assert "    needs: identity\n" in unit
    assert "VARIANT" not in named_step(unit, "Shell tests")
    assert told in named_step(unit, "Runtime tests")
    # The sign job only packages, so the whole job is told.
    sign = job(BUILD, "sign")
    assert f"      {told}\n" in sign.split("    steps:\n")[0]
    assert sum("electron-builder" in step for step in steps(sign)) == 3
    e2e = named_step(job(BUILD, "e2e"), "Install, run, upgrade and uninstall the app")
    assert told in e2e


UPDATE_CHECK = "Check that installed apps will take these files as an update"
# Records what it was asked, in place of src/identity.js on `desktop`.
RECORDER = """\
require("node:fs").writeFileSync("asked.json", JSON.stringify(process.argv.slice(2)));
process.exitCode = process.env.ANSWER === "refuse" ? 1 : 0;
"""
LATEST_YML = """\
version: 1.2.3
files:
  - url: AutoGPT-voice-Setup-1.2.3-x64.exe
    sha512: abc
    size: 1
path: AutoGPT-voice-Setup-1.2.3-x64.exe
sha512: abc
releaseDate: '2026-10-03T00:00:00.000Z'
"""
LATEST_MAC_YML = """\
version: 1.2.3
files:
  - url: AutoGPT-voice-1.2.3-arm64.zip
    sha512: abc
    size: 1
  - url: AutoGPT-voice-1.2.3-arm64.dmg
    sha512: abc
    size: 1
path: AutoGPT-voice-1.2.3-arm64.zip
sha512: abc
"""


def ask_the_app(
    tmp_path: Path, files: dict[str, str], answer: str = "accept"
) -> tuple[int, list[str] | None, str]:
    """Runs the build's check in a directory that holds these files. Returns
    its exit code, what the app's code was asked, and what it printed."""
    if not shutil.which("node"):
        pytest.skip("no node to run the stand-in for src/identity.js with")
    desktop = tmp_path / "desktop"
    shutil.rmtree(desktop, ignore_errors=True)
    for name, content in files.items():
        target = desktop / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    desktop.mkdir(exist_ok=True)
    program = tmp_path / "owns.sh"
    step = named_step(job(BUILD, "build"), UPDATE_CHECK)
    program.write_text(script(step), encoding="utf-8", newline="\n")
    done = subprocess.run(
        [bash(), "--noprofile", "--norc", "-eo", "pipefail", program.as_posix()],
        cwd=desktop,
        env={"PATH": os.environ["PATH"], "VERSION": "1.2.3", "ANSWER": answer},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    asked = desktop / "asked.json"
    arguments = json.loads(asked.read_text("utf-8")) if asked.exists() else None
    return done.returncode, arguments, done.stdout + done.stderr


def test_the_built_apps_own_code_is_asked_whether_it_would_take_the_update(
    tmp_path: Path,
) -> None:
    files = {
        "src/identity.js": RECORDER,
        "dist/latest.yml": LATEST_YML,
        "dist/latest-mac.yml": LATEST_MAC_YML,
    }
    code, asked, printed = ask_the_app(tmp_path, files)
    assert code == 0, printed
    # Every file the metadata names, once, with the version.
    assert asked == [
        "owns",
        "1.2.3",
        "AutoGPT-voice-1.2.3-arm64.dmg",
        "AutoGPT-voice-1.2.3-arm64.zip",
        "AutoGPT-voice-Setup-1.2.3-x64.exe",
    ]
    code, asked, printed = ask_the_app(tmp_path, files, answer="refuse")
    assert code != 0 and asked is not None

    # Metadata that names nothing would be refused by every installed app.
    empty = {**files, "dist/latest.yml": "version: 1.2.3\n"}
    del empty["dist/latest-mac.yml"]
    code, asked, printed = ask_the_app(tmp_path, empty)
    assert code == 1 and asked is None and "names no file" in printed

    # No metadata (an ad-hoc macOS build has none), or an older commit.
    code, asked, printed = ask_the_app(tmp_path, {"src/identity.js": RECORDER})
    assert code == 0 and asked is None and "nothing to ask" in printed
    code, asked, printed = ask_the_app(tmp_path, {"dist/latest.yml": LATEST_YML})
    assert code == 0 and asked is None and "nothing to ask" in printed


def test_both_jobs_that_package_ask_before_they_upload() -> None:
    for name in ("build", "sign"):
        names = [step_name(step) for step in steps(job(BUILD, name))]
        assert names.index(UPDATE_CHECK) < names.index("Upload installers"), name
    build_check = script(named_step(job(BUILD, "build"), UPDATE_CHECK))
    sign_check = script(named_step(job(BUILD, "sign"), UPDATE_CHECK))
    assert build_check == sign_check
    assert 'node src/identity.js owns "${VERSION}"' in build_check


def test_the_installed_app_tests_get_the_definition_of_the_names() -> None:
    checkout = named_step(job(BUILD, "e2e"), "Check out the tests from ${{ inputs.ref }}")
    for path in ("autogpt_platform/desktop/e2e", "autogpt_platform/desktop/src"):
        assert f"            {path}\n" in checkout + "\n"


def test_every_job_that_names_the_app_waits_for_the_job_that_decides_it() -> None:
    for name in ("build", "sign", "e2e"):
        block = job(BUILD, name)
        needs = re.search(r"^    needs: \[(.+)\]$", block, re.MULTILINE)
        assert needs and "identity" in needs.group(1).split(", "), name
    assert "needs.identity.result == 'success'" in job(BUILD, "build")
    identity = without_comments(job(BUILD, "identity"))
    for forbidden in ("secrets.", "environment:"):
        assert forbidden not in identity
    # One step asks GitHub whether the commit is on `desktop`, with the job's
    # read-only token. No other step has a token, and that step is over
    # before the one that runs the built commit's code starts.
    names = [step_name(step) for step in steps(job(BUILD, "identity"))]
    assert names.index(CHECK) < names.index("Name the app")
    for step in steps(job(BUILD, "identity")):
        holds = "GH_TOKEN" in step or "github.token" in step
        assert holds == (step_name(step) == CHECK), step_name(step)
    check = script(named_step(job(BUILD, "identity"), CHECK))
    assert "node" not in check and "autogpt_platform" not in check
    assert re.search(r"^permissions:\n  contents: read\n", BUILD, re.MULTILINE)
    assert identity.count("actions/checkout@") == identity.count(
        "persist-credentials: false"
    )


def test_artifacts_of_a_variant_are_named_after_it() -> None:
    suffix = "${{ needs.identity.outputs.suffix }}"
    for name in ("build", "sign"):
        block = job(BUILD, name)
        assert f"name: installers-${{{{ matrix.key }}}}{suffix}\n" in block
        assert f"name: release-${{{{ matrix.key }}}}{suffix}\n" in block
    e2e = job(BUILD, "e2e")
    assert f"name: installers-${{{{ matrix.installers }}}}{suffix}\n" in e2e
    assert f"name: installers-${{{{ matrix.installers }}}}{suffix}-upgrade\n" in e2e
    release = job(RELEASE, "release")
    for key in ("windows-x64", "macos-arm64", "linux-x64"):
        assert f"name: installers-{key}${{{{ needs.build.outputs.suffix }}}}\n" in release


def test_a_dispatched_build_can_name_a_variant_and_the_nightly_never_does() -> None:
    call = BUILD.split("  workflow_call:")[1].split("  workflow_dispatch:")[0]
    dispatch = BUILD.split("  workflow_dispatch:")[1].split("\npermissions:")[0]
    assert "      variant:\n" in call and "      branch:\n" in call
    assert "      variant:\n" in dispatch
    assert "variant" not in without_comments(workflow("desktop-nightly.yml"))


def test_a_build_of_a_variant_is_not_reported_as_a_build_of_desktop() -> None:
    """The reporter sees a run's title and nothing of its inputs."""
    title = re.search(r"^run-name: >-\n  (.+)$", BUILD, re.MULTILINE)
    assert title, "desktop-build.yml no longer names its runs"
    assert "inputs.ref" in title.group(1)
    assert "format(' as variant {0}', inputs.variant)" in title.group(1)
    reporter = workflow("build-report.yml")
    assert (
        "!contains(github.event.workflow_run.display_title, 'variant')" in reporter
    )
    # A push to a variant branch is not a push to desktop either.
    assert "github.event.workflow_run.head_branch == 'desktop'" in reporter


def test_a_variant_is_released_as_a_prerelease_and_never_as_the_latest() -> None:
    """GitHub's latest release is all the normal app ever looks at, and a
    pre-release is never that."""
    release = job(RELEASE, "release")
    kinds = re.findall(r"^\s+KIND: (.+)$", release, re.MULTILINE)
    assert kinds == [
        "${{ (inputs.prerelease || inputs.variant != '') && '--prerelease' || '--latest=false' }}",
        "${{ (inputs.prerelease || inputs.variant != '') && '--prerelease' || '--latest' }}",
    ]
    assert release.count("--latest") == 2, "nothing else may make a release the latest"
    create = named_step(release, "Create the draft")
    publish = named_step(release, "Publish")
    assert '--draft "${KIND}"' in create
    assert '--draft=false "${KIND}"' in publish


def test_the_release_is_of_the_variant_that_was_asked_for() -> None:
    assert '--variant "${VARIANT}"' in job(RELEASE, "plan")
    assert "VARIANT: ${{ inputs.variant }}" in job(RELEASE, "plan")
    assert "      variant: ${{ inputs.variant }}\n" in job(RELEASE, "build")
    check = named_step(
        job(RELEASE, "release"), "Check that the release is complete and consistent"
    )
    assert "REQUESTED_VARIANT: ${{ inputs.variant }}" in check
    assert '[ "${VARIANT}" = "${REQUESTED_VARIANT}" ]' in check
    # The tag the plan made (main's rule) is the one the built app looks for
    # (the built commit's rule).
    assert '[ "${TAG}" = "${TAG_PREFIX}${VERSION}" ]' in check
    env = job(RELEASE, "release").split("    steps:\n")[0]
    for line in (
        "VARIANT: ${{ needs.build.outputs.variant }}",
        "BASE: ${{ needs.build.outputs.artifact_base }}",
        "TAG_PREFIX: ${{ needs.build.outputs.tag_prefix }}",
        "PRODUCT: ${{ needs.build.outputs.product_name }}",
    ):
        assert f"      {line}\n" in env
    exported = BUILD.split("    outputs:\n")[1].split("  workflow_dispatch:")[0]
    for name in ("variant", "suffix", "artifact_base", "tag_prefix", "product_name"):
        assert f"        value: ${{{{ jobs.identity.outputs.{name} }}}}\n" in exported


def run_release_check(
    tmp_path: Path, files: list[str], variant: str = "", **overrides: str
) -> tuple[int, str]:
    """Runs the first part of the release's check, up to the update metadata,
    over these files."""
    step = named_step(
        job(RELEASE, "release"), "Check that the release is complete and consistent"
    )
    body = script(step).split("for metadata in latest*.yml; do")[0]
    out = tmp_path / "out"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    for name in files:
        (out / name).write_text("x", encoding="utf-8")
    program = tmp_path / "check.sh"
    program.write_text(body, encoding="utf-8", newline="\n")
    names = {
        "VERSION": "1.2.3",
        "VARIANT": variant,
        "REQUESTED_VARIANT": variant,
        "BASE": f"AutoGPT-{variant}" if variant else "AutoGPT",
        "TAG_PREFIX": f"desktop-{variant}-v" if variant else "desktop-v",
        "TAG": f"desktop-{variant}-v1.2.3" if variant else "desktop-v1.2.3",
        "PRODUCT": f"AutoGPT ({variant})" if variant else "AutoGPT",
        **overrides,
    }
    done = subprocess.run(
        [bash(), "--noprofile", "--norc", "-eo", "pipefail", program.as_posix()],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], **names},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def release_files(base: str) -> list[str]:
    return [
        f"{base}-Setup-1.2.3-x64.exe",
        f"{base}-Setup-1.2.3-x64.exe.blockmap",
        f"{base}-1.2.3-arm64.dmg",
        f"{base}-1.2.3-arm64.zip",
        f"{base}-x86_64.AppImage",
        f"{base}-1.2.3-amd64.deb",
        "latest.yml",
        "latest-mac.yml",
        "latest-linux.yml",
        "signing-windows-x64.txt",
        "signing-macos-arm64.txt",
        "signing-linux-x64.txt",
    ]


def test_a_release_holds_the_files_of_one_app(tmp_path: Path) -> None:
    code, printed = run_release_check(tmp_path, release_files("AutoGPT"))
    assert code == 0, printed
    code, printed = run_release_check(tmp_path, release_files("AutoGPT-voice"), "voice")
    assert code == 0, printed
    # The other app's files under this app's release.
    code, printed = run_release_check(tmp_path, release_files("AutoGPT-voice"))
    assert code == 1 and "is missing" in printed
    code, printed = run_release_check(tmp_path, release_files("AutoGPT"), "voice")
    assert code == 1 and "is missing" in printed
    # One stray installer of another app among the right ones.
    for stray, variant in (
        ("AutoGPT-voice-Setup-1.2.3-x64.exe", ""),
        ("AutoGPT-Setup-1.2.3-x64.exe", "voice"),
        ("AutoGPT-voice-2-1.2.3-amd64.deb", "voice"),
        ("AutoGPT-voice-x86_64.AppImage", ""),
        ("AutoGPT-Setup-1.2.4-x64.exe", ""),
    ):
        base = f"AutoGPT-{variant}" if variant else "AutoGPT"
        code, printed = run_release_check(
            tmp_path, [*release_files(base), stray], variant
        )
        assert code == 1 and f"{stray} is not a file of" in printed, printed


def test_a_release_whose_tag_the_built_app_would_not_look_for_is_refused(
    tmp_path: Path,
) -> None:
    files = release_files("AutoGPT-voice")
    code, printed = run_release_check(
        tmp_path, files, "voice", TAG_PREFIX="desktop-variant-voice-v"
    )
    assert code == 1 and "where the app that was built looks for updates" in printed
    code, printed = run_release_check(
        tmp_path, files, "voice", REQUESTED_VARIANT="lab"
    )
    assert code == 1 and "The build is of 'voice', not of 'lab'" in printed
    code, printed = run_release_check(
        tmp_path, release_files("AutoGPT"), "", REQUESTED_VARIANT="voice"
    )
    assert code == 1 and "The build is of 'the normal app', not of 'voice'" in printed


# --- the release plan ---------------------------------------------------------


def test_a_variants_tag_and_branch_come_from_its_slug() -> None:
    assert release_plan.tag_prefix("") == "desktop-v"
    assert release_plan.tag_prefix("voice") == "desktop-voice-v"
    assert release_plan.branch_of("") == "desktop"
    assert release_plan.branch_of("voice") == "variant/voice"


def releases(*tags: str, **flags: Any) -> dict[str, Any]:
    return {"/releases": [{"tag_name": tag, "draft": False, **flags} for tag in tags]}


def test_a_variants_latest_release_is_the_highest_of_its_own() -> None:
    listed = releases(
        "desktop-v9.0.0",
        "desktop-voice-2-v8.0.0",
        "desktop-voice-v1.10.0",
        "desktop-voice-v1.9.0",
        "desktop-voice-v2.0.0-rc.1",
        "desktop-lab-v7.0.0",
        "v0.4.7",
    )
    github = FakeGitHub(listed)
    assert release_plan.latest_variant_release(github, "desktop-voice-v") == "1.10.0"
    assert release_plan.latest_variant_release(github, "desktop-voice-2-v") == "8.0.0"
    assert release_plan.latest_variant_release(github, "desktop-gpu-v") is None
    drafts = FakeGitHub(
        {"/releases": [{"tag_name": "desktop-voice-v3.0.0", "draft": True}]}
    )
    assert release_plan.latest_variant_release(drafts, "desktop-voice-v") is None


def test_the_normal_app_never_follows_a_variants_release() -> None:
    """`desktop-voice-v1.0.0` begins with `desktop-v` too."""
    for tag in ("desktop-voice-v1.0.0", "desktop-v1-v1.0.0", "desktop-v-v1.0.0"):
        marked = FakeGitHub({"/releases/latest": {"tag_name": tag}})
        with pytest.raises(release_plan.Refused, match="not a desktop release"):
            release_plan.latest_release(marked)


def variant_replies(status: str = "identical") -> dict[str, Any]:
    return {
        **MAIN_ONLY,
        # The normal app is far ahead; that is no concern of the variant's.
        "/releases/latest": {"tag_name": "desktop-v5.0.0"},
        **releases("desktop-v5.0.0", "desktop-voice-v0.2.0"),
        "/commits/variant/voice": {"sha": SHA},
        f"/compare/{SHA}...variant/voice": {"status": status},
        "/commits/desktop": {"sha": "b" * 40},
        f"/compare/{'b' * 40}...variant/voice": {"status": "diverged"},
        f"/compare/{'b' * 40}...desktop": {"status": "identical"},
    }


def plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replies: dict[str, Any], *arguments: str
) -> tuple[int, str]:
    output = tmp_path / "output"
    output.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GH_TOKEN", "not-a-token")
    monkeypatch.setattr(
        release_plan, "GitHub", lambda repository, token: FakeGitHub(replies)
    )
    code = release_plan.main(["--repo", "owner/name", *arguments])
    return code, output.read_text(encoding="utf-8")


def test_a_variant_is_released_from_its_own_branch_with_its_own_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = variant_replies()
    # The head of variant/voice when no commit is named, and when the
    # workflow hands over its empty default.
    for ref in ((), ("--ref", "")):
        code, written = plan(
            monkeypatch, tmp_path, replies, "--version", "0.3.0", "--variant", "voice", *ref
        )
        assert code == 0, capsys.readouterr().out
        assert written == f"sha={SHA}\ntag=desktop-voice-v0.3.0\n"
    assert "from variant/voice commit" in capsys.readouterr().out

    # A commit of desktop is not a commit of the variant.
    arguments = ("--version", "0.3.0", "--variant", "voice", "--ref", "desktop")
    code, written = plan(monkeypatch, tmp_path, replies, *arguments)
    assert code == 1 and written == ""
    assert "not on the variant/voice branch" in capsys.readouterr().out


def test_a_variants_versions_are_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = variant_replies()
    for version in ("0.2.0", "0.1.9"):
        code, written = plan(
            monkeypatch, tmp_path, replies, "--version", version, "--variant", "voice"
        )
        assert code == 1 and written == ""
        assert "not higher than the latest release, 0.2.0" in capsys.readouterr().out
    tagged = {
        **replies,
        "/git/ref/tags/desktop-voice-v0.3.0": {"ref": "refs/tags/desktop-voice-v0.3.0"},
    }
    code, _ = plan(
        monkeypatch, tmp_path, tagged, "--version", "0.3.0", "--variant", "voice"
    )
    assert code == 1 and "never released twice" in capsys.readouterr().out
    code, _ = plan(
        monkeypatch, tmp_path, replies, "--version", "0.3.0", "--variant", "Voice"
    )
    assert code == 1 and "cannot name a variant" in capsys.readouterr().out
    # The normal app still releases from desktop, from its head by default.
    code, written = plan(monkeypatch, tmp_path, replies, "--version", "5.0.1")
    assert code == 0, capsys.readouterr().out
    assert written == f"sha={'b' * 40}\ntag=desktop-v5.0.1\n"


def test_the_manual_explains_variants() -> None:
    assert "\n## Variants\n" in MANUAL
    for phrase in ("`variant/<slug>`", "`desktop-<slug>-v<version>`", "-f variant="):
        assert phrase in MANUAL, f"{phrase} is not in docs/MAINTAINING.md"
