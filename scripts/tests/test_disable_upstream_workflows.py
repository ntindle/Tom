"""Selection logic of disable_upstream_workflows.py. No network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import disable_upstream_workflows as script
from github_rest import GitHub, GitHubError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ALLOWED = frozenset(
    {
        ".github/workflows/sync-upstream.yml",
        ".github/workflows/desktop-build.yml",
        ".github/workflows/platform-desktop-build.yml",
    }
)


def workflow(path: str, state: str = "active", identifier: int = 1) -> dict[str, Any]:
    return {"id": identifier, "path": path, "state": state, "name": path}


class FakeGitHub(GitHub):
    def __init__(self, failing: frozenset[int] = frozenset()) -> None:
        super().__init__("owner/name", "not-a-token")
        self.calls: list[tuple[str, str]] = []
        self._failing = failing

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, str | int] | None = None,
    ) -> Any:
        self.calls.append((method, path))
        if int(path.split("/")[3]) in self._failing:
            raise GitHubError(403, method, path, "Resource not accessible")


@pytest.mark.parametrize(
    ("path", "state", "expected"),
    [
        (".github/workflows/sync-upstream.yml", "active", script.KEEP),
        (".github/workflows/platform-desktop-build.yml", "active", script.KEEP),
        (".github/workflows/sync-upstream.yml", "disabled_inactivity", script.KEEP),
        (".github/workflows/platform-backend-ci.yml", "active", script.DISABLE),
        (".github/workflows/codeql.yml", "disabled_manually", script.ALREADY_OFF),
        (".github/workflows/repo-stats.yml", "disabled_inactivity", script.ALREADY_OFF),
        (".github/workflows/old.yml", "deleted", script.ALREADY_OFF),
        ("dynamic/dependabot/dependabot-updates", "active", script.MANAGED),
        ("dynamic/github-code-scanning/codeql", "active", script.MANAGED),
        (".github/workflows/Sync-Upstream.yml", "active", script.DISABLE),
        (".github/workflows/sub/sync-upstream.yml", "active", script.DISABLE),
    ],
)
def test_classify(path: str, state: str, expected: str) -> None:
    assert script.classify(workflow(path, state), ALLOWED) == expected


def test_plan_disables_only_active_upstream_workflows() -> None:
    plan = script.build_plan(
        [
            workflow(".github/workflows/platform-frontend-ci.yml", identifier=3),
            workflow(".github/workflows/sync-upstream.yml", identifier=1),
            workflow(".github/workflows/codeql.yml", "disabled_manually", identifier=4),
            workflow("dynamic/dependabot/dependabot-updates", identifier=5),
            workflow(".github/workflows/batch-reconcile.yml", identifier=2),
        ],
        ALLOWED,
    )
    assert [w["path"] for w in plan.disable] == [
        ".github/workflows/batch-reconcile.yml",
        ".github/workflows/platform-frontend-ci.yml",
    ]
    assert [w["path"] for w in plan.keep] == [".github/workflows/sync-upstream.yml"]
    assert [w["path"] for w in plan.already_off] == [".github/workflows/codeql.yml"]
    assert [w["path"] for w in plan.managed] == [
        "dynamic/dependabot/dependabot-updates"
    ]


def test_a_second_run_has_nothing_to_do() -> None:
    first = [
        workflow(".github/workflows/codeql.yml"),
        workflow(".github/workflows/sync-upstream.yml"),
    ]
    assert len(script.build_plan(first, ALLOWED).disable) == 1
    second = [workflow(".github/workflows/codeql.yml", "disabled_manually"), first[1]]
    assert script.build_plan(second, ALLOWED).disable == []


def test_allow_list_comes_from_the_workflow_files(tmp_path: Path) -> None:
    for name in ("sync-upstream.yml", "desktop-build.yaml", "notes.md"):
        (tmp_path / name).write_text("", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "inner.yml").write_text("", encoding="utf-8")
    allowed = script.fork_owned_paths(
        tmp_path, [script.DESKTOP_CALLER, "\\.github\\workflows\\extra.yml"]
    )
    assert allowed == {
        ".github/workflows/sync-upstream.yml",
        ".github/workflows/desktop-build.yaml",
        ".github/workflows/platform-desktop-build.yml",
        ".github/workflows/extra.yml",
    }


def test_an_empty_workflows_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="refusing"):
        script.fork_owned_paths(tmp_path, [script.DESKTOP_CALLER])
    with pytest.raises(SystemExit, match="refusing"):
        script.fork_owned_paths(tmp_path / "missing", [script.DESKTOP_CALLER])


def test_allow_list_entries_must_be_workflow_paths(tmp_path: Path) -> None:
    (tmp_path / "sync-upstream.yml").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="must start with"):
        script.fork_owned_paths(tmp_path, ["platform-desktop-build.yml"])


def test_the_real_allow_list_contains_the_workflow_that_runs_the_script() -> None:
    allowed = script.fork_owned_paths(
        script.DEFAULT_WORKFLOWS_DIR, [script.DESKTOP_CALLER]
    )
    assert script.DEFAULT_WORKFLOWS_DIR == REPOSITORY_ROOT / ".github" / "workflows"
    assert ".github/workflows/sync-upstream.yml" in allowed
    assert script.DESKTOP_CALLER in allowed


def test_disable_all_calls_the_disable_endpoint_and_collects_failures() -> None:
    client = FakeGitHub(failing=frozenset({8}))
    failures = script.disable_all(
        client,
        [
            workflow(".github/workflows/a.yml", identifier=7),
            workflow(".github/workflows/b.yml", identifier=8),
        ],
    )
    assert client.calls == [
        ("PUT", "/actions/workflows/7/disable"),
        ("PUT", "/actions/workflows/8/disable"),
    ]
    assert len(failures) == 1
    assert failures[0].startswith(".github/workflows/b.yml: ")


def test_summary_names_what_changed_and_what_needs_attention() -> None:
    plan = script.build_plan(
        [
            workflow(".github/workflows/codeql.yml"),
            workflow(".github/workflows/sync-upstream.yml", "disabled_inactivity"),
        ],
        ALLOWED,
    )
    dry = script.render_summary(plan, ALLOWED, [], dry_run=True)
    assert "dry run, nothing changed" in dry
    assert "Would disable: 1" in dry
    assert "- .github/workflows/codeql.yml" in dry
    assert "- .github/workflows/sync-upstream.yml (disabled_inactivity)" in dry
    assert (
        "- .github/workflows/platform-desktop-build.yml"
        in dry.split("not registered")[1]
    )

    failed = script.render_summary(
        plan, ALLOWED, [".github/workflows/codeql.yml: HTTP 403"], dry_run=False
    )
    assert "Disabled: 0" in failed
    assert "FAILED to disable:" in failed


def test_dry_run_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "sync-upstream.yml").write_text("", encoding="utf-8")
    requests: list[str] = []

    class Client:
        def __init__(self, repository: str, token: str) -> None:
            assert (repository, token) == ("owner/name", "t")

        def pages(self, path: str, key: str) -> list[dict[str, Any]]:
            assert (path, key) == ("/actions/workflows", "workflows")
            return [workflow(".github/workflows/codeql.yml", identifier=9)]

        def request(self, method: str, path: str) -> None:
            requests.append(f"{method} {path}")

    monkeypatch.setattr(script, "GitHub", Client)
    monkeypatch.setattr(script, "token_from_environment", lambda: "t")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    arguments = ["--repo", "owner/name", "--workflows-dir", str(tmp_path)]

    assert script.main([*arguments, "--dry-run"]) == 0
    assert requests == []
    assert "Would disable: 1" in capsys.readouterr().out

    assert script.main(arguments) == 0
    assert requests == ["PUT /actions/workflows/9/disable"]


def test_keep_alive_re_enables_all_but_workflows_disabled_by_hand() -> None:
    client = FakeGitHub(failing=frozenset({4}))
    fork_owned = [
        workflow(".github/workflows/sync-upstream.yml", identifier=1),
        workflow(
            ".github/workflows/desktop-nightly.yml", "disabled_inactivity", identifier=2
        ),
        workflow(
            ".github/workflows/main-checks.yml", "disabled_manually", identifier=3
        ),
        workflow(".github/workflows/desktop-build.yml", identifier=4),
    ]
    result = script.keep_alive(client, fork_owned, dry_run=False)
    assert client.calls == [
        ("PUT", "/actions/workflows/1/enable"),
        ("PUT", "/actions/workflows/2/enable"),
        ("PUT", "/actions/workflows/4/enable"),
    ]
    assert result.enabled == [
        ".github/workflows/sync-upstream.yml (active)",
        ".github/workflows/desktop-nightly.yml (disabled_inactivity)",
    ]
    assert result.left_off == [".github/workflows/main-checks.yml (disabled_manually)"]
    assert len(result.failures) == 1
    assert result.failures[0].startswith(".github/workflows/desktop-build.yml: ")

    summary = script.render_keep_alive(result, dry_run=False)
    assert "re-enabled 2 fork-owned workflow(s)" in summary
    assert "Left off, disabled by hand:" in summary
    assert "FAILED to re-enable:" in summary

    dry = FakeGitHub()
    assert len(script.keep_alive(dry, fork_owned, dry_run=True).enabled) == 3
    assert dry.calls == []


SHA = "c" * 40


def run(
    identifier: int, path: str, status: str = "queued", sha: str = SHA
) -> dict[str, Any]:
    return {"id": identifier, "path": path, "status": status, "head_sha": sha}


class FakeRuns(GitHub):
    """Serves one list of runs per poll, then repeats the last one."""

    def __init__(
        self,
        polls: list[list[dict[str, Any]]],
        cancel_status: dict[int, int] | None = None,
    ) -> None:
        super().__init__("owner/name", "not-a-token")
        self._polls = polls
        self._cancel_status = cancel_status or {}
        self.listed = 0
        self.cancelled: list[int] = []

    def pages(
        self,
        path: str,
        query: dict[str, str | int] | None = None,
        key: str | None = None,
    ) -> Any:
        assert (path, query, key) == (
            "/actions/runs",
            {"head_sha": SHA},
            "workflow_runs",
        )
        poll = self._polls[min(self.listed, len(self._polls) - 1)]
        self.listed += 1
        return iter(poll)

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, str | int] | None = None,
    ) -> Any:
        parts = path.strip("/").split("/")
        assert method == "POST" and parts[:2] == ["actions", "runs"]
        assert parts[3] == "cancel"
        identifier = int(parts[2])
        if status := self._cancel_status.get(identifier):
            raise GitHubError(status, method, path, "refused")
        self.cancelled.append(identifier)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.parametrize(
    ("path", "status", "expected"),
    [
        (".github/workflows/copilot-setup-steps.yml", "queued", True),
        (".github/workflows/copilot-setup-steps.yml", "in_progress", True),
        (
            ".github/workflows/copilot-setup-steps.yml@refs/heads/desktop",
            "queued",
            True,
        ),
        (".github/workflows/copilot-setup-steps.yml", "completed", False),
        (".github/workflows/platform-desktop-build.yml", "queued", False),
        (
            ".github/workflows/platform-desktop-build.yml@refs/heads/desktop",
            "queued",
            False,
        ),
        (".github/workflows/sync-upstream.yml", "in_progress", False),
        ("dynamic/dependabot/dependabot-updates", "queued", False),
    ],
)
def test_is_foreign_run(path: str, status: str, expected: bool) -> None:
    assert script.is_foreign_run(run(1, path, status), ALLOWED) is expected


def test_only_upstream_runs_of_the_commit_are_cancelled() -> None:
    client = FakeRuns(
        [
            [
                run(1, ".github/workflows/copilot-setup-steps.yml"),
                run(2, script.DESKTOP_CALLER),
                run(3, ".github/workflows/codeql.yml", "completed"),
                run(4, "dynamic/dependabot/dependabot-updates"),
            ]
        ]
    )
    sweep = script.cancel_foreign_runs(client, SHA, ALLOWED)
    assert client.cancelled == [1]
    assert client.listed == 1
    assert sweep.cancelled == [".github/workflows/copilot-setup-steps.yml (run 1)"]
    assert sweep.failures == []
    assert sweep.caller_run is True


def test_runs_that_appear_late_are_caught_and_each_is_cancelled_once() -> None:
    clock = Clock()
    late = run(7, ".github/workflows/new-from-upstream.yml")
    client = FakeRuns([[], [], [late], [late, run(8, script.DESKTOP_CALLER)]])
    sweep = script.cancel_foreign_runs(
        client, SHA, ALLOWED, watch_seconds=20, sleep=clock.sleep, clock=clock
    )
    assert client.cancelled == [7]
    assert clock.sleeps == [script.POLL_SECONDS] * 4
    assert client.listed == 5
    assert sweep.caller_run is True


def test_no_caller_run_is_reported_when_the_push_started_none() -> None:
    client = FakeRuns([[run(1, ".github/workflows/codeql.yml", "completed")]])
    assert script.cancel_foreign_runs(client, SHA, ALLOWED).caller_run is False


def test_a_finished_run_is_not_a_failure_but_a_refused_cancel_is() -> None:
    client = FakeRuns(
        [
            [
                run(1, ".github/workflows/a.yml"),
                run(2, ".github/workflows/b.yml"),
            ]
        ],
        cancel_status={1: script.ALREADY_FINISHED, 2: 403},
    )
    sweep = script.cancel_foreign_runs(client, SHA, ALLOWED)
    assert sweep.cancelled == [".github/workflows/a.yml (run 1)"]
    assert len(sweep.failures) == 1
    assert sweep.failures[0].startswith(".github/workflows/b.yml (run 2): ")


def test_a_dry_run_cancels_nothing() -> None:
    client = FakeRuns([[run(1, ".github/workflows/a.yml")]])
    sweep = script.cancel_foreign_runs(client, SHA, ALLOWED, dry_run=True)
    assert client.cancelled == []
    assert sweep.cancelled == [".github/workflows/a.yml (run 1)"]


def test_after_cancelling_the_workflow_that_ran_is_disabled_in_the_same_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "sync-upstream.yml").write_text("", encoding="utf-8")
    output = tmp_path / "output"
    requests: list[str] = []
    new = ".github/workflows/new-from-upstream.yml"

    class Client:
        """GitHub lists the new workflow only once its first run exists."""

        def __init__(self, repository: str, token: str) -> None:
            self.workflow_lists = 0

        def pages(self, path: str, **arguments: Any) -> list[dict[str, Any]]:
            if path == "/actions/runs":
                assert arguments == {
                    "query": {"head_sha": SHA},
                    "key": "workflow_runs",
                }
                return [run(5, new), run(6, script.DESKTOP_CALLER)]
            assert (path, arguments) == ("/actions/workflows", {"key": "workflows"})
            self.workflow_lists += 1
            return [] if self.workflow_lists == 1 else [workflow(new, identifier=9)]

        def request(self, method: str, path: str) -> None:
            requests.append(f"{method} {path}")

    monkeypatch.setattr(script, "GitHub", Client)
    monkeypatch.setattr(script, "token_from_environment", lambda: "t")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    arguments = [
        *("--repo", "owner/name", "--workflows-dir", str(tmp_path)),
        *("--cancel-runs-for-sha", SHA),
    ]

    assert script.main(arguments) == 0
    assert requests == [
        "POST /actions/runs/5/cancel",
        "PUT /actions/workflows/9/disable",
    ]
    assert output.read_text(encoding="utf-8") == "caller_run=true\n"
    printed = capsys.readouterr().out
    assert "Cancelled: 1" in printed
    assert f"Disabled after the runs:\n  - {new}" in printed


def test_the_commit_to_sweep_must_be_a_full_sha(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        script.parse_arguments(
            ["--repo", "owner/name", "--cancel-runs-for-sha", "desktop"]
        )
