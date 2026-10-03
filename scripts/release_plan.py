#!/usr/bin/env python3
"""Check a request for a desktop release before anything is built.

    python scripts/release_plan.py --repo OWNER/NAME --version 1.2.3 --ref desktop
    python scripts/release_plan.py --repo OWNER/NAME --version 1.3.0-rc.1 --ref desktop --prerelease

Refuses the request unless all of this holds:

- the version is `X.Y.Z` or `X.Y.Z-something` (semantic versioning: no
  leading zeros), and it is a pre-release exactly when it has the
  `-something` part;
- `X.Y.Z` is higher than the latest published release. Installed apps never
  move to a lower version, so a lower release would reach nobody;
- no tag named `desktop-v<version>` exists. A version that was published is
  never built again;
- `--ref` names a commit that the `desktop` branch contains;
- the `desktop-release` environment exists and only `main` may use it. That
  rule is what keeps the signing certificates away from workflows on
  `desktop`, which carries upstream's workflow files unreviewed. A token that
  may not read environments gets a warning instead, as in the sync.

Writes `sha` (the commit to build) and `tag` to $GITHUB_OUTPUT. Needs a token
that can read the repository in GH_TOKEN or GITHUB_TOKEN, or a signed-in `gh`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

from github_rest import GitHub, GitHubError, token_from_environment

TAG_PREFIX = "desktop-v"
# Semantic versioning to the letter: no leading zeros in a number (1.2.03,
# 1.2.3-rc.01). electron-updater reads versions that strictly, in the app and
# in latest.yml, so such a release could be published and never installed.
NUMBER = r"(0|[1-9][0-9]*)"
IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
VERSION = re.compile(
    rf"{NUMBER}\.{NUMBER}\.{NUMBER}(?:-({IDENTIFIER}(?:\.{IDENTIFIER})*))?"
)
NOT_FOUND = 404
MANUAL = "docs/MAINTAINING.md, 'Cutting a release'"


class Refused(Exception):
    """The release must not be made; the message says why."""


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: str

    @property
    def release(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    github = GitHub(args.repo, token_from_environment())
    try:
        check_version(args.version, args.prerelease, latest_release(github))
        check_unused(github, args.version)
        sha = commit_on_branch(github, args.ref, args.branch)
        warning = check_environment(github, args.environment, args.ops_branch)
    except Refused as refusal:
        print(f"::error title=Release refused::{refusal}")
        return 1
    if warning:
        print(f"::warning title=Environment not checked::{warning}")
    tag = f"{TAG_PREFIX}{args.version}"
    print(f"Release {tag} from {args.branch} commit {sha}")
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"sha={sha}\ntag={tag}\n")
    return 0


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    parser.add_argument("--version", required=True)
    parser.add_argument("--ref", required=True, help="branch, tag or commit to build")
    parser.add_argument("--prerelease", action="store_true")
    parser.add_argument("--branch", default="desktop", help="must contain --ref")
    parser.add_argument("--ops-branch", default="main")
    parser.add_argument("--environment", default="desktop-release")
    return parser.parse_args(argv)


def parse_version(text: str) -> Version:
    match = VERSION.fullmatch(text)
    if not match:
        raise Refused(
            f"'{text}' is not a version. Use 1.2.3, or 1.2.3-rc.1 for a pre-release."
        )
    major, minor, patch, prerelease = match.groups()
    return Version(int(major), int(minor), int(patch), prerelease or "")


def check_version(text: str, prerelease: bool, latest: str | None) -> None:
    version = parse_version(text)
    if version.prerelease and not prerelease:
        raise Refused(
            f"{text} has a pre-release part, so it must be published as a pre-release."
        )
    if prerelease and not version.prerelease:
        raise Refused(
            f"{text} would be a pre-release, and {text} could then never be released "
            f"properly. Give it a pre-release part, such as {text}-rc.1."
        )
    if latest is None:
        return
    if version.release <= parse_version(latest).release:
        raise Refused(
            f"{text} is not higher than the latest release, {latest}. Installed apps "
            "never move to a lower version."
        )


def latest_release(github: GitHub) -> str | None:
    """The version of the release GitHub calls the latest, which is the one
    installed apps update to. None before the first release."""
    try:
        tag = github.request("GET", "/releases/latest")["tag_name"]
    except GitHubError as error:
        if error.status == NOT_FOUND:
            return None
        raise
    if not tag.startswith(TAG_PREFIX):
        raise Refused(
            f"The latest release is '{tag}', which is not a desktop release. Installed "
            "apps update to whatever is latest: remove that release or stop it being "
            "the latest first."
        )
    return tag.removeprefix(TAG_PREFIX)


def check_unused(github: GitHub, version: str) -> None:
    tag = f"{TAG_PREFIX}{version}"
    if exists(github, f"/git/ref/tags/{tag}"):
        raise Refused(
            f"The tag {tag} exists. A version is never released twice: pick the next "
            f"one. See {MANUAL}."
        )


def commit_on_branch(github: GitHub, ref: str, branch: str) -> str:
    try:
        sha = github.request("GET", f"/commits/{ref}")["sha"]
    except GitHubError as error:
        raise Refused(f"Cannot find the commit '{ref}': {error}") from None
    comparison = github.request("GET", f"/compare/{sha}...{branch}")
    if comparison["status"] not in ("ahead", "identical"):
        raise Refused(
            f"The commit {sha} is not on the {branch} branch. Releases are built from "
            f"{branch} only."
        )
    return sha


def check_environment(github: GitHub, name: str, branch: str) -> str | None:
    """Refuses an environment that is missing or open to other branches.
    Returns a warning when the token may not look."""
    hint = f"See {MANUAL}."
    try:
        names = [
            found["name"] for found in github.pages("/environments", key="environments")
        ]
        if name not in names:
            raise Refused(f"The '{name}' environment does not exist. {hint}")
        policy = github.request("GET", f"/environments/{name}").get(
            "deployment_branch_policy"
        )
        allowed = sorted(
            f"{rule.get('type', 'branch')}:{rule['name']}"
            for rule in github.pages(
                f"/environments/{name}/deployment-branch-policies",
                key="branch_policies",
            )
        )
    except GitHubError as error:
        return f"Could not read the branch policy of the '{name}' environment: {error}"
    if not (policy or {}).get("custom_branch_policies") or allowed != [
        f"branch:{branch}"
    ]:
        found = ", ".join(allowed) or "no rule, so every branch"
        raise Refused(
            f"The '{name}' environment must admit the branch '{branch}' and nothing "
            f"else (found: {found}). {hint}"
        )
    return None


def exists(github: GitHub, path: str) -> bool:
    try:
        github.request("GET", path)
    except GitHubError as error:
        if error.status == NOT_FOUND:
            return False
        raise
    return True


if __name__ == "__main__":
    sys.exit(main())
