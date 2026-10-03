"""merge_upstream.py against throwaway git repositories on disk. No network."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import merge_upstream as script

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@dataclass
class Repositories:
    upstream: Path
    fork: Path
    root: Path

    def clone_of_fork(self, name: str) -> Path:
        """What a job's checkout of `desktop` looks like."""
        target = self.root / name
        git(self.root, "clone", "--quiet", "--branch", "desktop", str(self.fork), name)
        return target


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


def commit(repo: Path, name: str, content: str, message: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    git(repo, "add", "--", name)
    git(repo, "commit", "--quiet", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repositories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repositories:
    """An upstream with a `dev` branch, and a fork whose `desktop` adds one directory."""
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    # The machine's own git configuration (commit signing, line endings) stays out.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.invalid")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "--quiet", "--initial-branch", "dev")
    commit(upstream, "shared.txt", "one\n", "upstream: first")

    fork = tmp_path / "fork"
    git(tmp_path, "clone", "--quiet", str(upstream), "fork")
    git(fork, "switch", "--quiet", "-c", "desktop")
    commit(fork, "desktop/app.txt", "app\n", "fork: desktop app")
    return Repositories(upstream, fork, tmp_path)


def first(repo: Path, repositories: Repositories) -> script.Outcome:
    return script.first_merge(repo, str(repositories.upstream), "dev", "desktop")


def repeat(
    repo: Path, repositories: Repositories, outcome: script.Outcome, **override: str
) -> script.Outcome:
    expected = {
        "base": outcome.base_sha,
        "upstream": outcome.upstream_sha,
        "tree": outcome.tree_sha,
    } | override
    return script.repeat_merge(
        repo, str(repositories.upstream), "dev", "desktop", **expected
    )


def test_nothing_to_merge_when_desktop_contains_upstream(
    repositories: Repositories,
) -> None:
    work = repositories.clone_of_fork("work")
    head = git(work, "rev-parse", "HEAD")
    outcome = first(work, repositories)
    assert outcome.state == "current"
    assert outcome.base_sha == head
    assert outcome.upstream_sha == git(repositories.upstream, "rev-parse", "dev")
    assert git(work, "rev-parse", "HEAD") == head


def test_a_clean_merge_is_a_merge_commit_of_desktop_and_upstream(
    repositories: Repositories,
) -> None:
    upstream_sha = commit(repositories.upstream, "new.txt", "new\n", "upstream: more")
    work = repositories.clone_of_fork("work")
    base = git(work, "rev-parse", "HEAD")

    outcome = first(work, repositories)

    assert outcome.state == "merged"
    assert (outcome.base_sha, outcome.upstream_sha) == (base, upstream_sha)
    assert outcome.merge_sha == git(work, "rev-parse", "HEAD")
    assert outcome.tree_sha == git(work, "rev-parse", "HEAD^{tree}")
    assert git(work, "log", "-1", "--format=%P").split() == [base, upstream_sha]
    assert git(work, "log", "-1", "--format=%an") == "github-actions[bot]"
    assert git(work, "log", "-1", "--format=%s") == (
        f"Merge upstream dev ({upstream_sha[:10]}) into desktop"
    )
    assert (work / "new.txt").exists() and (work / "desktop" / "app.txt").exists()


def test_a_conflict_is_listed_and_leaves_the_checkout_untouched(
    repositories: Repositories,
) -> None:
    commit(repositories.fork, "shared.txt", "fork\n", "fork: edit shared")
    commit(repositories.upstream, "shared.txt", "upstream\n", "upstream: edit shared")
    work = repositories.clone_of_fork("work")
    base = git(work, "rev-parse", "HEAD")

    outcome = first(work, repositories)

    assert outcome.state == "conflict"
    assert outcome.conflicts == ("shared.txt",)
    assert outcome.tree_sha == "" and outcome.merge_sha == ""
    assert git(work, "rev-parse", "HEAD") == base
    assert git(work, "status", "--porcelain") == ""


def test_another_machine_reproduces_the_merge(repositories: Repositories) -> None:
    commit(repositories.upstream, "new.txt", "new\n", "upstream: more")
    found = first(repositories.clone_of_fork("first"), repositories)

    again = repeat(repositories.clone_of_fork("second"), repositories, found)

    assert again.state == "merged"
    assert again.tree_sha == found.tree_sha
    assert (again.base_sha, again.upstream_sha) == (found.base_sha, found.upstream_sha)


def test_the_repeat_merges_the_tested_commit_not_what_upstream_pushed_since(
    repositories: Repositories,
) -> None:
    commit(repositories.upstream, "new.txt", "new\n", "upstream: more")
    found = first(repositories.clone_of_fork("first"), repositories)
    commit(repositories.upstream, "later.txt", "later\n", "upstream: after the test")
    second = repositories.clone_of_fork("second")

    again = repeat(second, repositories, found)

    assert again.tree_sha == found.tree_sha
    assert not (second / "later.txt").exists()
    assert git(second, "rev-parse", "refs/remotes/upstream/dev") == found.upstream_sha


def test_the_repeat_refuses_a_desktop_that_moved(repositories: Repositories) -> None:
    commit(repositories.upstream, "new.txt", "new\n", "upstream: more")
    found = first(repositories.clone_of_fork("first"), repositories)
    commit(repositories.fork, "desktop/app.txt", "changed\n", "fork: pushed meanwhile")
    second = repositories.clone_of_fork("second")
    head = git(second, "rev-parse", "HEAD")

    with pytest.raises(script.MergeError, match="moved from"):
        repeat(second, repositories, found)
    assert git(second, "rev-parse", "HEAD") == head


def test_the_repeat_refuses_a_different_tree(repositories: Repositories) -> None:
    commit(repositories.upstream, "new.txt", "new\n", "upstream: more")
    found = first(repositories.clone_of_fork("first"), repositories)
    with pytest.raises(script.MergeError, match="not the one that was tested"):
        repeat(repositories.clone_of_fork("second"), repositories, found, tree="0" * 40)


def test_the_repeat_refuses_a_commit_upstream_does_not_contain(
    repositories: Repositories,
) -> None:
    commit(repositories.upstream, "new.txt", "new\n", "upstream: more")
    found = first(repositories.clone_of_fork("first"), repositories)
    foreign = commit(repositories.fork, "x.txt", "x\n", "not an upstream commit")
    git(repositories.fork, "reset", "--quiet", "--hard", found.base_sha)
    second = repositories.clone_of_fork("second")
    git(second, "fetch", "--quiet", str(repositories.fork), foreign)

    with pytest.raises(script.MergeError, match="no longer contains"):
        repeat(second, repositories, found, upstream=foreign)
    with pytest.raises(script.MergeError, match="no longer contains"):
        repeat(second, repositories, found, upstream="1" * 40)


def test_command_writes_outputs_and_the_conflict_list(
    repositories: Repositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit(repositories.fork, "shared.txt", "fork\n", "fork: edit shared")
    commit(repositories.upstream, "shared.txt", "upstream\n", "upstream: edit shared")
    work = repositories.clone_of_fork("work")
    output = repositories.root / "output"
    conflicts = repositories.root / "conflicts.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    arguments = [
        *("--repo-dir", str(work), "--upstream-url", str(repositories.upstream)),
        *("--conflicts-file", str(conflicts)),
    ]
    assert script.main(arguments) == 0

    written = dict(
        line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines()
    )
    assert written["state"] == "conflict"
    assert set(written) == {"state", "upstream_sha", "base_sha"}
    assert conflicts.read_bytes() == b"shared.txt\n"


def test_command_fails_with_an_error_annotation_when_the_repeat_differs(
    repositories: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    commit(repositories.upstream, "new.txt", "new\n", "upstream: more")
    found = first(repositories.clone_of_fork("first"), repositories)
    second = repositories.clone_of_fork("second")
    arguments = [
        *("--repo-dir", str(second), "--upstream-url", str(repositories.upstream)),
        *("--expect-base", found.base_sha, "--expect-upstream", found.upstream_sha),
    ]
    assert script.main([*arguments, "--expect-tree", "0" * 40]) == 1
    assert capsys.readouterr().out.startswith("::error::This merge produced tree")


def test_expectations_must_be_complete_shas() -> None:
    base = ["--repo-dir", ".", "--upstream-url", "u"]
    with pytest.raises(SystemExit):
        script.parse_arguments([*base, "--expect-tree", "a" * 40])
    with pytest.raises(SystemExit):
        script.parse_arguments(
            [
                *base,
                *("--expect-base", "a" * 40, "--expect-upstream", "dev"),
                *("--expect-tree", "a" * 40),
            ]
        )
