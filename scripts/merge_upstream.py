#!/usr/bin/env python3
"""Merge upstream into a checkout of `desktop`, identically in every job of the sync.

The sync merges three times on three machines: once to find out what there is
to merge, once on the machine that runs the tests (which holds no credential),
and once on the machine that pushes (which never runs the merged code). The
repeats are only accepted when they reproduce the first merge exactly.

    merge_upstream.py --repo-dir desktop --upstream-url URL --upstream-branch dev
        The first merge. Writes to $GITHUB_OUTPUT: state (current, merged or
        conflict), upstream_sha, base_sha and, when merged, tree_sha and
        merge_sha. With --conflicts-file, the conflicted paths go there.

    merge_upstream.py ... --expect-base SHA --expect-upstream SHA --expect-tree SHA
        Repeats that merge. Fails unless the checkout is at the same commit,
        the upstream branch still contains the same commit, and the merge
        produces the same tree.

Standard library and `git` only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
REMOTE = "upstream"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class MergeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Outcome:
    state: str
    upstream_sha: str
    base_sha: str
    tree_sha: str = ""
    merge_sha: str = ""
    conflicts: tuple[str, ...] = ()


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    try:
        if args.expect_tree:
            outcome = repeat_merge(
                args.repo_dir,
                args.upstream_url,
                args.upstream_branch,
                args.product_branch,
                base=args.expect_base,
                upstream=args.expect_upstream,
                tree=args.expect_tree,
            )
        else:
            outcome = first_merge(
                args.repo_dir,
                args.upstream_url,
                args.upstream_branch,
                args.product_branch,
            )
    except MergeError as error:
        print(f"::error::{error}")
        return 1
    if args.conflicts_file and outcome.conflicts:
        args.conflicts_file.write_text(
            "\n".join(outcome.conflicts) + "\n", encoding="utf-8", newline="\n"
        )
    report(outcome)
    return 0


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--repo-dir", type=Path, required=True)
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument("--upstream-branch", default="dev")
    parser.add_argument(
        "--product-branch", default="desktop", help="only used in the merge message"
    )
    parser.add_argument("--conflicts-file", type=Path)
    parser.add_argument("--expect-base", default="")
    parser.add_argument("--expect-upstream", default="")
    parser.add_argument("--expect-tree", default="")
    args = parser.parse_args(argv)
    expected = (args.expect_base, args.expect_upstream, args.expect_tree)
    if any(expected) and not all(COMMIT_SHA.fullmatch(value) for value in expected):
        parser.error(
            "--expect-base, --expect-upstream and --expect-tree go together and each takes a full 40-character SHA"
        )
    return args


def first_merge(repo: Path, url: str, branch: str, product_branch: str) -> Outcome:
    upstream = fetch_upstream(repo, url, branch)
    base = git(repo, "rev-parse", "HEAD")
    if succeeds(repo, "merge-base", "--is-ancestor", upstream, "HEAD"):
        return Outcome("current", upstream, base)
    if merge(repo, upstream, branch, product_branch):
        return merged(repo, upstream, base)
    conflicts = git(repo, "diff", "--name-only", "--diff-filter=U").splitlines()
    if not conflicts:
        raise MergeError("git merge failed without reporting a conflicted file.")
    git(repo, "merge", "--abort")
    return Outcome("conflict", upstream, base, conflicts=tuple(conflicts))


def repeat_merge(
    repo: Path,
    url: str,
    branch: str,
    product_branch: str,
    *,
    base: str,
    upstream: str,
    tree: str,
) -> Outcome:
    head = git(repo, "rev-parse", "HEAD")
    if head != base:
        raise MergeError(
            f"{product_branch} moved from {base[:10]} to {head[:10]} while the sync ran. "
            "Nothing was pushed; the next run starts again from the new commit."
        )
    tip = fetch_upstream(repo, url, branch)
    if not succeeds(repo, "merge-base", "--is-ancestor", upstream, tip):
        raise MergeError(
            f"Upstream {branch} ({tip[:10]}) no longer contains {upstream[:10]}, the commit that was tested."
        )
    # Tests that compare against upstream must see the commit being merged,
    # not whatever upstream pushed since.
    git(repo, "update-ref", f"refs/remotes/{REMOTE}/{branch}", upstream)
    if not merge(repo, upstream, branch, product_branch):
        raise MergeError(
            f"Merging {upstream[:10]} conflicted here although the first merge was clean."
        )
    outcome = merged(repo, upstream, base)
    if outcome.tree_sha != tree:
        raise MergeError(
            f"This merge produced tree {outcome.tree_sha[:10]}, the first merge {tree[:10]}. "
            "Refusing to continue with a tree that is not the one that was tested."
        )
    return outcome


def fetch_upstream(repo: Path, url: str, branch: str) -> str:
    """Fetches the branch into `refs/remotes/upstream/<branch>` and returns its commit."""
    if REMOTE in git(repo, "remote").splitlines():
        git(repo, "remote", "set-url", REMOTE, url)
    else:
        git(repo, "remote", "add", REMOTE, url)
    ref = f"refs/remotes/{REMOTE}/{branch}"
    git(repo, "fetch", "--no-tags", REMOTE, f"+refs/heads/{branch}:{ref}")
    return git(repo, "rev-parse", ref)


def merge(repo: Path, upstream: str, branch: str, product_branch: str) -> bool:
    message = f"Merge upstream {branch} ({upstream[:10]}) into {product_branch}"
    return succeeds(repo, "merge", "--no-ff", "--no-edit", "-m", message, upstream)


def merged(repo: Path, upstream: str, base: str) -> Outcome:
    return Outcome(
        "merged",
        upstream,
        base,
        tree_sha=git(repo, "rev-parse", "HEAD^{tree}"),
        merge_sha=git(repo, "rev-parse", "HEAD"),
    )


def git(repo: Path, *arguments: str) -> str:
    result = run(repo, *arguments)
    if result.returncode != 0:
        raise MergeError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def succeeds(repo: Path, *arguments: str) -> bool:
    result = run(repo, *arguments)
    sys.stderr.write(result.stdout + result.stderr)
    return result.returncode == 0


def run(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    identity = {
        "GIT_AUTHOR_NAME": BOT_NAME,
        "GIT_AUTHOR_EMAIL": BOT_EMAIL,
        "GIT_COMMITTER_NAME": BOT_NAME,
        "GIT_COMMITTER_EMAIL": BOT_EMAIL,
    }
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        env=os.environ | identity,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        check=False,
    )


def report(outcome: Outcome) -> None:
    values = {
        "state": outcome.state,
        "upstream_sha": outcome.upstream_sha,
        "base_sha": outcome.base_sha,
        "tree_sha": outcome.tree_sha,
        "merge_sha": outcome.merge_sha,
    }
    lines = [f"{name}={value}" for name, value in values.items() if value]
    print("\n".join(lines))
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                f"Upstream `{outcome.upstream_sha[:10]}`: {outcome.state}.\n\n"
            )


if __name__ == "__main__":
    sys.exit(main())
