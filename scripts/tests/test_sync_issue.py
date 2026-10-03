"""Issue bookkeeping of sync_issue.py against an in-memory GitHub. No network."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import sync_issue as script
from github_rest import GitHub, GitHubError

SHA_A = "a" * 40
SHA_B = "b" * 40


class FakeGitHub(GitHub):
    """Implements the handful of endpoints the script uses."""

    def __init__(self, labels: tuple[str, ...] = ()) -> None:
        super().__init__("owner/name", "not-a-token")
        self.labels = set(labels)
        self.issues: list[dict[str, Any]] = []
        self.comments: dict[int, list[str]] = {}

    def add_issue(
        self, body: str, *, pull_request: bool = False, state: str = "open"
    ) -> int:
        number = len(self.issues) + 1
        issue: dict[str, Any] = {
            "number": number,
            "title": "x",
            "body": body,
            "state": state,
            "labels": [script.LABEL],
        }
        if pull_request:
            issue["pull_request"] = {}
        self.issues.append(issue)
        return number

    def pages(
        self,
        path: str,
        query: dict[str, str | int] | None = None,
        key: str | None = None,
    ) -> Iterator[Any]:
        assert (path, key) == ("/issues", None)
        assert query == {"labels": script.LABEL, "state": "open"}
        return iter([issue for issue in self.issues if issue["state"] == "open"])

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, str | int] | None = None,
    ) -> Any:
        parts = path.strip("/").split("/")
        if (method, parts[0]) == ("GET", "labels"):
            if parts[1] not in self.labels:
                raise GitHubError(404, method, path, "Not Found")
            return {"name": parts[1]}
        if (method, path) == ("POST", "/labels"):
            assert body is not None
            self.labels.add(body["name"])
            return body
        if (method, path) == ("POST", "/issues"):
            assert body is not None and body["labels"] == [script.LABEL]
            number = self.add_issue(body["body"])
            self.issues[-1]["title"] = body["title"]
            return self.issues[-1] | {"number": number}
        if method == "PATCH" and parts[0] == "issues":
            assert body is not None
            self.issues[int(parts[1]) - 1].update(body)
            return None
        if method == "POST" and parts[2:] == ["comments"]:
            assert body is not None
            self.comments.setdefault(int(parts[1]), []).append(body["body"])
            return None
        raise AssertionError(f"unexpected request {method} {path}")


def report(kind: str = "conflict", sha: str = SHA_A, **extra: Any) -> script.Report:
    return script.Report(
        repository="owner/name",
        kind=kind,
        upstream_sha=sha,
        run_url="https://run/1",
        **extra,
    )


def test_first_report_creates_the_label_and_one_issue() -> None:
    client = FakeGitHub()
    assert script.publish(client, report(conflicts=(".gitignore",))) == "opened #1"
    assert client.labels == {script.LABEL}
    assert len(client.issues) == 1
    assert client.issues[0]["title"] == script.TITLE
    assert "```text\n.gitignore\n```" in client.issues[0]["body"]
    assert client.comments == {}


def test_the_same_state_updates_the_body_without_a_comment() -> None:
    client = FakeGitHub(labels=(script.LABEL,))
    script.publish(client, report())
    outcome = script.publish(client, report())
    assert outcome == "updated #1 (same state, no comment)"
    assert len(client.issues) == 1
    assert client.comments == {}


@pytest.mark.parametrize("changed", [report(sha=SHA_B), report(kind="gate")])
def test_a_changed_state_updates_the_one_issue_and_comments_once(
    changed: script.Report,
) -> None:
    client = FakeGitHub(labels=(script.LABEL,))
    script.publish(client, report())
    assert script.publish(client, changed) == "updated #1 and commented"
    assert len(client.issues) == 1
    assert script.marker_of(client.issues[0]) == script.Marker(
        changed.kind, changed.upstream_sha
    )
    assert len(client.comments[1]) == 1


def test_an_issue_written_by_a_person_is_left_alone() -> None:
    client = FakeGitHub(labels=(script.LABEL,))
    client.add_issue("I am looking into the sync by hand.")

    assert script.publish(client, report()) == "opened #2"
    for _ in range(10):
        assert script.publish(client, report()) == "updated #2 (same state, no comment)"
    assert client.issues[0]["body"] == "I am looking into the sync by hand."
    assert client.comments == {}

    assert script.resolve(client, {"conflict"}, "done") == "closed #2"
    assert client.issues[0]["state"] == "open"
    assert client.comments == {2: ["done"]}


def test_an_issue_written_by_a_person_is_neither_closed_nor_counted() -> None:
    client = FakeGitHub(labels=(script.LABEL,))
    client.add_issue("Why does the sync run at 05:23?")
    assert script.resolve(client, {"conflict"}, "done") == "no open issue"
    assert script.build_is_pending(client) is False
    assert client.issues[0]["state"] == "open"
    assert client.comments == {}


def test_pull_requests_with_the_label_are_ignored() -> None:
    client = FakeGitHub(labels=(script.LABEL,))
    client.add_issue(script.render_body(report()), pull_request=True)
    assert script.publish(client, report()) == "opened #2"


def test_resolve_closes_only_the_kinds_it_was_given() -> None:
    client = FakeGitHub(labels=(script.LABEL,))
    assert (
        script.resolve(client, {"conflict", "gate", "error"}, "done") == "no open issue"
    )

    script.publish(client, report(kind="build"))
    assert script.resolve(client, {"conflict", "gate", "error"}, "done").startswith(
        "left #1 open"
    )
    assert client.issues[0]["state"] == "open"

    script.publish(client, report(kind="gate"))
    assert (
        script.resolve(client, {"conflict", "gate", "error"}, "synced") == "closed #1"
    )
    assert client.issues[0]["state"] == "closed"
    assert client.comments[1][-1] == "synced"


def test_resolve_rejects_an_unknown_kind() -> None:
    with pytest.raises(SystemExit, match="Unknown kind"):
        script.resolve(FakeGitHub(), {"conflcit"}, "done")


def test_body_carries_marker_facts_and_a_link_to_the_manual() -> None:
    body = script.render_body(
        report(kind="gate", logs=(("Runtime tests", "1 failed"),))
    )
    assert body.startswith(f"<!-- upstream-sync kind=gate sha={SHA_A} -->\n")
    assert (
        f"[`{SHA_A[:10]}`](https://github.com/Significant-Gravitas/AutoGPT/commit/{SHA_A})"
        in body
    )
    assert "- Run: https://run/1" in body
    assert "- `desktop` was not changed by this run." in body
    assert "### Runtime tests (last 60 lines)" in body
    assert (
        "https://github.com/owner/name/blob/main/docs/MAINTAINING.md#fix-a-failing-gate"
        in body
    )
    assert "WAS pushed" in script.render_body(report(kind="error", pushed=True))


def test_a_build_report_says_nothing_about_a_push() -> None:
    body = script.render_body(
        script.Report(repository="owner/name", kind="build", run_url="https://run/9")
    )
    assert body.startswith("<!-- upstream-sync kind=build sha= -->\n")
    assert "- Run: https://run/9" in body
    assert "pushed" not in body and "not changed" not in body
    assert "#the-build-of-desktop-failed" in body


def test_every_kind_links_to_a_heading_that_exists_in_the_manual() -> None:
    manual = (
        Path(__file__).resolve().parents[2] / "docs" / "MAINTAINING.md"
    ).read_text(encoding="utf-8")
    anchors = {
        "".join(
            c for c in line.lstrip("# ").lower() if c.isalnum() or c in " -"
        ).replace(" ", "-")
        for line in manual.splitlines()
        if line.startswith("#")
    }
    assert {kind.anchor for kind in script.KINDS.values()} <= anchors


def test_tail_keeps_the_end_strips_colour_and_is_bounded() -> None:
    text = "\n".join(f"\x1b[31mline {n}\x1b[0m" for n in range(200)) + "\n"
    kept = script.tail(text).split("\n")
    assert kept[0] == "line 140"
    assert kept[-1] == "line 199"
    assert len(script.tail("x" * 50_000)) == script.TAIL_CHARACTERS


def test_fence_is_longer_than_any_backticks_in_the_log() -> None:
    block = script.fenced("before\n````\ninside\n````\nafter")
    assert block.startswith("`````text\n")
    assert block.endswith("\n`````")
    assert script.fenced("plain").startswith("```text\n")


def test_long_conflict_lists_are_capped() -> None:
    paths = tuple(f"file{n}" for n in range(script.MAX_CONFLICTS + 5))
    listing = script.conflict_list(paths)
    assert f"### Conflicted files ({len(paths)})" in listing
    assert listing.endswith("... and 5 more\n```")


def test_conflicted_paths_cannot_inject_markdown() -> None:
    hostile = "docs/a` @some-org/team [x](https://example.test) `b.md"
    fence_breaker = "```@some-org/team"
    listing = script.conflict_list((hostile, fence_breaker))
    heading, _, block = listing.partition("\n\n")
    assert heading == "### Conflicted files (2)"
    # One code block from the first line to the last, fenced with more
    # backticks than any path contains, so no path is rendered as Markdown.
    assert block.split("\n") == ["````text", hostile, fence_breaker, "````"]


@pytest.mark.parametrize(
    ("results", "merge_state", "expected"),
    [
        ({"preflight": "success", "merge": "success"}, "current", ""),
        ({"merge": "success", "gate": "success", "publish": "success"}, "merged", ""),
        ({"merge": "success", "gate": "skipped", "catchup": "skipped"}, "current", ""),
        ({"merge": "success", "gate": "skipped"}, "conflict", "conflict"),
        ({"gate": "failure", "gate_shell": "failure"}, "merged", "gate"),
        ({"gate": "failure", "gate_runtime": "failure"}, "merged", "gate"),
        # The gate job broke before or after the tests: not a test failure.
        ({"gate": "failure", "gate_shell": "success"}, "merged", "error"),
        ({"gate": "failure", "gate_shell": "", "gate_runtime": ""}, "merged", "error"),
        # A job that hits its timeout is reported as cancelled.
        ({"gate": "cancelled", "gate_shell": "success"}, "merged", "error"),
        ({"preflight": "failure", "token": "skipped", "merge": "skipped"}, "", "error"),
        ({"merge": "failure"}, "", "error"),
        ({"gate": "success", "publish": "failure"}, "merged", "error"),
        ({"merge": "success", "catchup": "failure"}, "current", "error"),
        ({"merge": "success", "pending_check": "failure"}, "current", "error"),
    ],
)
def test_classify(results: dict[str, str], merge_state: str, expected: str) -> None:
    assert script.classify(results, merge_state) == expected


def test_classify_command_prints_ok_or_the_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    arguments = ["classify", "--merge-state", "merged", "--result", "gate=success"]
    assert script.main(arguments) == 0
    assert capsys.readouterr().out == "ok\n"
    assert script.main([*arguments, "--result", "publish=failure"]) == 0
    assert capsys.readouterr().out == "error\n"
    assert output.read_text(encoding="utf-8") == "kind=\nkind=error\n"


def test_a_pushed_commit_waits_for_its_build_until_one_is_started() -> None:
    client = FakeGitHub(labels=(script.LABEL,))
    assert script.build_is_pending(client) is False

    script.publish(client, report(kind="error", pushed=True, build_pending=True))
    assert script.build_is_pending(client) is True
    assert "No installer build has been started" in client.issues[0]["body"]

    # Later failures of any kind, which know nothing about the push, keep the debt.
    script.publish(client, report(kind="error", sha=""))
    script.publish(client, report(kind="conflict", sha=SHA_B))
    assert script.build_is_pending(client) is True
    assert script.marker_of(client.issues[0]) == script.Marker("conflict", SHA_B, True)

    script.publish(client, report(kind="conflict", sha=SHA_B, build_started=True))
    assert script.build_is_pending(client) is False
    assert "No installer build" not in client.issues[0]["body"]
    assert len(client.issues) == 1


def test_a_closed_issue_no_longer_owes_a_build() -> None:
    client = FakeGitHub(labels=(script.LABEL,))
    script.publish(client, report(kind="error", pushed=True, build_pending=True))
    assert script.resolve(client, {"error"}, "built") == "closed #1"
    assert script.build_is_pending(client) is False


def test_marker_round_trips() -> None:
    for marker in (
        script.Marker("gate", SHA_A),
        script.Marker("error", "", True),
        script.Marker("build", SHA_B, True),
    ):
        assert script.marker_of({"body": f"{marker.render()}\ntext"}) == marker
    assert script.marker_of({"body": "no marker"}) is None
    assert script.marker_of({"body": None}) is None


def test_report_arguments_read_files_and_skip_missing_logs(tmp_path: Path) -> None:
    conflicts = tmp_path / "conflicts.txt"
    conflicts.write_bytes(b".gitignore\r\n\r\nautogpt_platform/a b.py\r\n")
    log = tmp_path / "gate.log"
    log.write_text("boom\n", encoding="utf-8")
    args = script.parse_arguments(
        [
            *("--repo", "owner/name", "report", "--kind", "conflict"),
            *("--upstream-sha", SHA_A.upper()),
            *("--conflicts-file", str(conflicts)),
            *("--log", f"Shell tests={log}"),
            *("--log", f"Gone={tmp_path / 'none'}"),
        ]
    )
    built = script.report_from_arguments(args)
    assert built.conflicts == (".gitignore", "autogpt_platform/a b.py")
    assert built.logs == (("Shell tests", "boom\n"),)
    assert built.upstream_sha == SHA_A
    assert built.pushed is False
    assert built.build_pending is False and built.build_started is False


def test_build_flags_exclude_each_other() -> None:
    base = ["--repo", "owner/name", "report", "--kind", "error"]
    assert script.parse_arguments([*base, "--pushed", "--build-pending"]).build_pending
    assert script.parse_arguments([*base, "--build-started"]).build_started
    with pytest.raises(SystemExit):
        script.parse_arguments([*base, "--build-pending", "--build-started"])
