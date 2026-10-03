#!/usr/bin/env python3
"""Keep ONE issue labelled `upstream-sync` describing why `desktop` is not in step with upstream.

    sync_issue.py classify --result job=outcome ...              which kind of failure a sync run was
    sync_issue.py report  --kind conflict|gate|error|build ...   open the issue, or update the open one
    sync_issue.py resolve --kinds conflict,gate,error            close it when those problems are gone
    sync_issue.py pending-build                                  does `desktop` still wait for a build?

The issue body always shows the latest state. A comment is added only when the
state changes (another kind of failure, or another upstream commit), so a
problem that lasts a week does not produce seven identical comments.

The script only ever touches the issue it opened itself, which it recognises by
a marker in the body. An issue a person opened with the same label is left
alone: not edited, not commented on, not closed.

Needs a token with Issues write in GH_TOKEN or GITHUB_TOKEN.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from github_rest import GitHub, GitHubError, token_from_environment

LABEL = "upstream-sync"
LABEL_COLOUR = "B60205"
LABEL_DESCRIPTION = "The daily merge of upstream dev into desktop is blocked"
TITLE = "Upstream sync is blocked"
UPSTREAM_COMMIT_URL = "https://github.com/Significant-Gravitas/AutoGPT/commit"
MARKER = re.compile(
    r"<!-- upstream-sync kind=([a-z]+) sha=([0-9a-f]*)( build=pending)? -->"
)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TAIL_LINES = 60
# GitHub's answer on a repository whose Issues are off, which a fork is until
# someone turns them on.
ISSUES_OFF_STATUS = 410
ISSUES_OFF = (
    "Issues are turned off in {repository}, and the sync keeps its state in one."
    " Turn them on under Settings > General > Features,"
    " or run: gh repo edit {repository} --enable-issues"
)
TAIL_CHARACTERS = 12_000
MAX_CONFLICTS = 200
# Job and step results that mean "did not finish": a job that hits its timeout is cancelled.
BROKEN = frozenset({"failure", "cancelled"})


@dataclass(frozen=True)
class Kind:
    headline: str
    anchor: str
    action: str


KINDS = {
    "conflict": Kind(
        "Upstream `dev` no longer merges cleanly into `desktop`.",
        "resolve-a-merge-conflict-by-hand",
        "Merge upstream by hand, resolve the files listed below, run the tests and push `desktop`.",
    ),
    "gate": Kind(
        "Upstream `dev` merges, but the desktop tests fail on the result.",
        "fix-a-failing-gate",
        "Merge upstream by hand, fix `autogpt_platform/desktop` until the tests pass, and push `desktop`.",
    ),
    "error": Kind(
        "The sync job failed at a step that is neither the merge nor the tests.",
        "the-sync-job-itself-failed",
        "Read the run log. An expired or under-privileged `FORK_SYNC_TOKEN` is the usual cause.",
    ),
    "build": Kind(
        "The installer build of `desktop` failed.",
        "the-build-of-desktop-failed",
        "Read the build run. Do not cut a release from this `desktop` commit.",
    ),
}


@dataclass(frozen=True)
class Marker:
    """What the hidden first line of the issue body records about the last report."""

    kind: str
    upstream_sha: str
    build_pending: bool = False

    def render(self) -> str:
        pending = " build=pending" if self.build_pending else ""
        return (
            f"<!-- upstream-sync kind={self.kind} sha={self.upstream_sha}{pending} -->"
        )


@dataclass(frozen=True)
class Report:
    repository: str
    kind: str
    upstream_sha: str = ""
    run_url: str = ""
    pushed: bool = False
    # `desktop` was pushed and no installer build was started for it.
    build_pending: bool = False
    # A build of the current `desktop` was started; clears an earlier `build_pending`.
    build_started: bool = False
    conflicts: tuple[str, ...] = ()
    logs: tuple[tuple[str, str], ...] = field(default=())


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    if args.command == "classify":
        kind = classify(parse_results(args.result), args.merge_state)
        print(kind or "ok")
        write_output("kind", kind)
        return 0
    client = GitHub(args.repo, token_from_environment())
    try:
        if args.command == "report":
            print(publish(client, report_from_arguments(args)))
        elif args.command == "resolve":
            print(resolve(client, set(args.kinds.split(",")), args.message))
        else:
            pending = build_is_pending(client)
            print(f"build pending: {str(pending).lower()}")
            write_output("pending", str(pending).lower())
    except GitHubError as error:
        if error.status != ISSUES_OFF_STATUS:
            raise
        message = ISSUES_OFF.format(repository=args.repo)
        print(f"::error title=Issues are turned off::{message}")
        print(message, file=sys.stderr)
        return 1
    return 0


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="OWNER/NAME"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sort = commands.add_parser(
        "classify", help="print the kind of failure, or `ok`; needs no token"
    )
    sort.add_argument(
        "--result",
        action="append",
        default=[],
        metavar="NAME=OUTCOME",
        help="result of a job or step of the sync run (repeatable)",
    )
    sort.add_argument("--merge-state", default="", help="output of the merge job")

    report = commands.add_parser("report", help="open or update the issue")
    report.add_argument("--kind", required=True, choices=sorted(KINDS))
    report.add_argument("--upstream-sha", default="")
    report.add_argument("--run-url", default="")
    report.add_argument(
        "--pushed", action="store_true", help="desktop was pushed before the failure"
    )
    build = report.add_mutually_exclusive_group()
    build.add_argument(
        "--build-pending",
        action="store_true",
        help="desktop was pushed and no build was started for it",
    )
    build.add_argument(
        "--build-started",
        action="store_true",
        help="a build of the current desktop was started",
    )
    report.add_argument(
        "--conflicts-file", type=Path, help="one conflicted path per line"
    )
    report.add_argument(
        "--log",
        action="append",
        default=[],
        metavar="TITLE=PATH",
        help="log to quote the tail of",
    )

    close = commands.add_parser(
        "resolve", help="close the issue if it describes one of --kinds"
    )
    close.add_argument(
        "--kinds", required=True, help="comma-separated kinds that are now fixed"
    )
    close.add_argument("--message", required=True, help="closing comment")

    commands.add_parser(
        "pending-build",
        help="print whether the open issue records a pushed desktop without a build",
    )

    args = parser.parse_args(argv)
    if args.command != "classify" and not args.repo:
        parser.error("--repo is required when GITHUB_REPOSITORY is not set")
    return args


def parse_results(entries: list[str]) -> dict[str, str]:
    results = {}
    for entry in entries:
        name, _, outcome = entry.partition("=")
        results[name.strip()] = outcome.strip()
    return results


def classify(results: dict[str, str], merge_state: str) -> str:
    """The kind of failure a sync run ended in, or "" when nothing is wrong.

    `results` maps job and step names of sync-upstream.yml to their results.
    `gate_shell` and `gate_runtime` are the two test steps: only a failed test
    makes the kind `gate`; a gate job that broke elsewhere is an `error`.
    """
    if merge_state == "conflict":
        return "conflict"
    broken = {name for name, outcome in results.items() if outcome in BROKEN}
    if broken & {"gate_shell", "gate_runtime"}:
        return "gate"
    return "error" if broken else ""


def report_from_arguments(args: argparse.Namespace) -> Report:
    logs = []
    for entry in args.log:
        title, _, path = entry.partition("=")
        if text := read_if_present(Path(path)):
            logs.append((title, text))
    conflicts = (
        read_if_present(args.conflicts_file).split("\n") if args.conflicts_file else []
    )
    return Report(
        repository=args.repo,
        kind=args.kind,
        upstream_sha=args.upstream_sha.strip().lower(),
        run_url=args.run_url,
        pushed=args.pushed,
        build_pending=args.build_pending,
        build_started=args.build_started,
        conflicts=tuple(line.strip() for line in conflicts if line.strip()),
        logs=tuple(logs),
    )


def read_if_present(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return ""


def publish(client: GitHub, report: Report) -> str:
    """Opens the issue, or updates the open one. Returns a one-line description of what it did."""
    ensure_label(client)
    issue = open_issue(client)
    previous = marker_of(issue) if issue else None
    # A pushed commit keeps waiting for its build through later failures of any
    # kind, until a run reports that a build was started.
    pending = report.build_pending or bool(
        previous and previous.build_pending and not report.build_started
    )
    marker = Marker(report.kind, report.upstream_sha, pending)
    body = render_body(report, marker)
    if issue is None or previous is None:
        created = client.request(
            "POST", "/issues", {"title": TITLE, "body": body, "labels": [LABEL]}
        )
        return f"opened #{created['number']}"
    number = issue["number"]
    client.request("PATCH", f"/issues/{number}", {"body": body})
    if (previous.kind, previous.upstream_sha) == (report.kind, report.upstream_sha):
        return f"updated #{number} (same state, no comment)"
    client.request(
        "POST", f"/issues/{number}/comments", {"body": change_comment(report)}
    )
    return f"updated #{number} and commented"


def resolve(client: GitHub, kinds: set[str], message: str) -> str:
    """Closes the open issue when it describes one of `kinds`."""
    if unknown := kinds - set(KINDS):
        raise SystemExit(f"Unknown kind(s): {', '.join(sorted(unknown))}")
    issue = open_issue(client)
    previous = marker_of(issue) if issue else None
    if issue is None or previous is None:
        return "no open issue"
    number = issue["number"]
    if previous.kind not in kinds:
        return f"left #{number} open (it does not describe {', '.join(sorted(kinds))})"
    client.request("POST", f"/issues/{number}/comments", {"body": message})
    client.request(
        "PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"}
    )
    return f"closed #{number}"


def build_is_pending(client: GitHub) -> bool:
    issue = open_issue(client)
    previous = marker_of(issue) if issue else None
    return bool(previous and previous.build_pending)


def ensure_label(client: GitHub) -> None:
    try:
        client.request("GET", f"/labels/{LABEL}")
    except GitHubError as error:
        if error.status != 404:
            raise
        client.request(
            "POST",
            "/labels",
            {"name": LABEL, "color": LABEL_COLOUR, "description": LABEL_DESCRIPTION},
        )


def open_issue(client: GitHub) -> dict[str, Any] | None:
    """The oldest open issue with the label that this script wrote.

    Issues without the marker belong to people and are skipped, as are pull
    requests, which the same endpoint lists.
    """
    issues = [
        item
        for item in client.pages("/issues", query={"labels": LABEL, "state": "open"})
        if "pull_request" not in item and marker_of(item) is not None
    ]
    return min(issues, key=lambda item: item["number"], default=None)


def marker_of(issue: dict[str, Any]) -> Marker | None:
    match = MARKER.search(issue.get("body") or "")
    if not match:
        return None
    return Marker(match.group(1), match.group(2), match.group(3) is not None)


def render_body(report: Report, marker: Marker | None = None) -> str:
    kind = KINDS[report.kind]
    marker = marker or Marker(report.kind, report.upstream_sha, report.build_pending)
    parts = [
        marker.render(),
        f"**{kind.headline}**",
        "\n".join(facts(report, marker)),
    ]
    if report.conflicts:
        parts.append(conflict_list(report.conflicts))
    for title, text in report.logs:
        parts.append(f"### {title} (last {TAIL_LINES} lines)\n\n{fenced(tail(text))}")
    guide = f"https://github.com/{report.repository}/blob/main/docs/MAINTAINING.md#{kind.anchor}"
    parts.append(
        f"### What to do\n\n{kind.action}\nStep by step: [docs/MAINTAINING.md]({guide})."
    )
    parts.append(
        "This issue is maintained by the workflows on `main`. Do not edit the "
        "description: it is rewritten on every run. The issue is closed by the "
        "first run that finds the problem gone."
    )
    return "\n\n".join(parts) + "\n"


def facts(report: Report, marker: Marker) -> list[str]:
    rows = []
    if report.upstream_sha:
        rows.append(
            f"- Upstream commit: [`{report.upstream_sha[:10]}`]({UPSTREAM_COMMIT_URL}/{report.upstream_sha})"
        )
    if report.run_url:
        rows.append(f"- Run: {report.run_url}")
    if report.kind != "build":
        rows.append(
            "- `desktop` WAS pushed before the failure; later steps did not finish."
            if report.pushed
            else "- `desktop` was not changed by this run."
        )
    if marker.build_pending:
        rows.append(
            "- No installer build has been started for the current `desktop` commit. "
            "The next sync run starts one."
        )
    return rows


def conflict_list(conflicts: tuple[str, ...]) -> str:
    """The paths in one code block: they come from upstream and must not render as Markdown."""
    rows = list(conflicts[:MAX_CONFLICTS])
    if len(conflicts) > MAX_CONFLICTS:
        rows.append(f"... and {len(conflicts) - MAX_CONFLICTS} more")
    return f"### Conflicted files ({len(conflicts)})\n\n{fenced(chr(10).join(rows))}"


def change_comment(report: Report) -> str:
    kind = KINDS[report.kind]
    where = (
        f" Upstream is at `{report.upstream_sha[:10]}`." if report.upstream_sha else ""
    )
    run = f" Run: {report.run_url}" if report.run_url else ""
    return f"Still blocked, and the state changed: {kind.headline}{where} The description above has the details.{run}"


def tail(text: str) -> str:
    lines = ANSI.sub("", text).rstrip("\n").split("\n")[-TAIL_LINES:]
    return "\n".join(lines)[-TAIL_CHARACTERS:]


def fenced(text: str) -> str:
    """A code block whose fence is longer than any run of backticks inside it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def write_output(name: str, value: str) -> None:
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


if __name__ == "__main__":
    sys.exit(main())
