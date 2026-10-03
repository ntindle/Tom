"""What release_plan.py refuses, and what it lets through. No network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import release_plan as script
from github_rest import GitHub, GitHubError

SHA = "a" * 40
MAIN_ONLY = {
    "/environments": {"environments": [{"name": "sync"}, {"name": "desktop-release"}]},
    "/environments/desktop-release": {
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        }
    },
    "/environments/desktop-release/deployment-branch-policies": {
        "branch_policies": [{"name": "main", "type": "branch"}]
    },
}


class FakeGitHub(GitHub):
    """Answers from a table of path -> reply; anything else is a 404."""

    def __init__(self, replies: dict[str, Any]) -> None:
        super().__init__("owner/name", "not-a-token")
        self._replies = replies

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, str | int] | None = None,
    ) -> Any:
        assert method == "GET", "the plan only reads"
        reply = self._replies.get(path, GitHubError(404, method, path, "Not Found"))
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.mark.parametrize(
    "text", ["1.2", "v1.2.3", "desktop-v1.2.3", "1.2.3+build", "1.2.3-", "1.2.3 ", ""]
)
def test_only_a_version_is_a_version(text: str) -> None:
    with pytest.raises(script.Refused, match="not a version"):
        script.parse_version(text)


@pytest.mark.parametrize(
    "text", ["01.2.3", "1.02.3", "1.2.03", "1.2.3-rc.01", "1.2.3-00", "1.2.3-rc..1"]
)
def test_a_number_with_a_leading_zero_is_not_a_version(text: str) -> None:
    """electron-updater would refuse it in the app and in latest.yml."""
    with pytest.raises(script.Refused, match="not a version"):
        script.parse_version(text)
    with pytest.raises(script.Refused, match="not a version"):
        script.check_version(text, prerelease="-" in text, latest="1.2.2")


def test_versions_are_read_as_numbers_and_a_prerelease_part() -> None:
    assert script.parse_version("0.0.0").release == (0, 0, 0)
    assert script.parse_version("10.20.30").release == (10, 20, 30)
    parsed = script.parse_version("1.2.3-rc.10")
    assert (parsed.release, parsed.prerelease) == ((1, 2, 3), "rc.10")
    assert script.parse_version("1.2.3-0a.x-y").prerelease == "0a.x-y"


def test_a_version_with_a_prerelease_part_must_be_a_prerelease_and_the_reverse() -> (
    None
):
    script.check_version("1.2.3", prerelease=False, latest=None)
    script.check_version("1.2.3-rc.1", prerelease=True, latest=None)
    with pytest.raises(script.Refused, match="must be published as a pre-release"):
        script.check_version("1.2.3-rc.1", prerelease=False, latest=None)
    with pytest.raises(script.Refused, match="never be released"):
        script.check_version("1.2.3", prerelease=True, latest=None)


@pytest.mark.parametrize("version", ["1.9.9", "1.10.0", "0.99.0", "1.10.0-rc.1"])
def test_a_release_must_be_higher_than_the_latest_one(version: str) -> None:
    with pytest.raises(script.Refused, match="not higher than the latest release"):
        script.check_version(version, prerelease="-" in version, latest="1.10.0")


@pytest.mark.parametrize("version", ["1.10.1", "1.11.0", "2.0.0", "1.11.0-rc.1"])
def test_versions_compare_as_numbers(version: str) -> None:
    script.check_version(version, prerelease="-" in version, latest="1.10.0")


def test_the_latest_release_is_the_one_github_marks_latest() -> None:
    assert script.latest_release(FakeGitHub({})) is None
    released = FakeGitHub({"/releases/latest": {"tag_name": "desktop-v1.4.0"}})
    assert script.latest_release(released) == "1.4.0"
    foreign = FakeGitHub({"/releases/latest": {"tag_name": "v0.4.7"}})
    with pytest.raises(script.Refused, match="not a desktop release"):
        script.latest_release(foreign)


def test_a_version_is_never_released_twice() -> None:
    script.check_unused(FakeGitHub({}), "1.4.0")
    tagged = FakeGitHub(
        {"/git/ref/tags/desktop-v1.4.0": {"ref": "refs/tags/desktop-v1.4.0"}}
    )
    with pytest.raises(script.Refused, match="never released twice"):
        script.check_unused(tagged, "1.4.0")


@pytest.mark.parametrize("status", ["ahead", "identical"])
def test_a_commit_that_desktop_contains_is_built(status: str) -> None:
    github = FakeGitHub(
        {
            "/commits/desktop": {"sha": SHA},
            f"/compare/{SHA}...desktop": {"status": status},
        }
    )
    assert script.commit_on_branch(github, "desktop", "desktop") == SHA


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_a_commit_from_anywhere_else_is_not(status: str) -> None:
    github = FakeGitHub(
        {
            "/commits/some-branch": {"sha": SHA},
            f"/compare/{SHA}...desktop": {"status": status},
        }
    )
    with pytest.raises(script.Refused, match="not on the desktop branch"):
        script.commit_on_branch(github, "some-branch", "desktop")
    with pytest.raises(script.Refused, match="Cannot find the commit"):
        script.commit_on_branch(github, "no-such-ref", "desktop")


def test_the_environment_must_admit_main_and_nothing_else() -> None:
    assert (
        script.check_environment(FakeGitHub(MAIN_ONLY), "desktop-release", "main")
        is None
    )

    missing = {**MAIN_ONLY, "/environments": {"environments": [{"name": "sync"}]}}
    with pytest.raises(script.Refused, match="does not exist"):
        script.check_environment(FakeGitHub(missing), "desktop-release", "main")

    # What GitHub creates by itself when a job first names an environment.
    unrestricted = {
        **MAIN_ONLY,
        "/environments/desktop-release": {"deployment_branch_policy": None},
        "/environments/desktop-release/deployment-branch-policies": {
            "branch_policies": []
        },
    }
    with pytest.raises(script.Refused, match="every branch"):
        script.check_environment(FakeGitHub(unrestricted), "desktop-release", "main")

    two = {
        **MAIN_ONLY,
        "/environments/desktop-release/deployment-branch-policies": {
            "branch_policies": [
                {"name": "main", "type": "branch"},
                {"name": "desktop", "type": "branch"},
            ]
        },
    }
    with pytest.raises(script.Refused, match="branch:desktop, branch:main"):
        script.check_environment(FakeGitHub(two), "desktop-release", "main")


def test_a_token_that_may_not_read_environments_gets_a_warning() -> None:
    forbidden = GitHubError(403, "GET", "/environments", "Resource not accessible")
    warning = script.check_environment(
        FakeGitHub({"/environments": forbidden}), "desktop-release", "main"
    )
    assert warning and "Could not read" in warning


def test_a_good_request_writes_the_commit_and_the_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = {
        **MAIN_ONLY,
        "/releases/latest": {"tag_name": "desktop-v1.3.0"},
        "/commits/desktop": {"sha": SHA},
        f"/compare/{SHA}...desktop": {"status": "identical"},
    }
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GH_TOKEN", "not-a-token")
    monkeypatch.setattr(script, "GitHub", lambda repository, token: FakeGitHub(replies))

    arguments = ["--repo", "owner/name", "--ref", "desktop"]
    assert script.main([*arguments, "--version", "1.4.0"]) == 0
    assert output.read_text(encoding="utf-8") == f"sha={SHA}\ntag=desktop-v1.4.0\n"

    assert script.main([*arguments, "--version", "1.3.0"]) == 1
    assert "::error title=Release refused::" in capsys.readouterr().out
    assert output.read_text(encoding="utf-8").count("sha=") == 1
