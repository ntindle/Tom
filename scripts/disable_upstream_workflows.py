#!/usr/bin/env python3
"""Disable every GitHub Actions workflow in this repository that the fork does not own.

The `desktop` branch carries all of upstream's workflow files. Keeping them off
the default branch stops the scheduled and manually dispatched ones; this script
is the backstop for the rest (pushes and pull requests into `desktop`, releases).

Fork-owned workflows are the files in this checkout's `.github/workflows`
(the `main` branch) plus the one caller workflow on `desktop`. Everything else
under `.github/workflows/` that is still active gets disabled. Workflows that
GitHub manages itself (paths starting with `dynamic/`, such as Dependabot
updates) are left alone.

Run it from a checkout of `main`:

    python scripts/disable_upstream_workflows.py --repo OWNER/NAME --dry-run
    python scripts/disable_upstream_workflows.py --repo OWNER/NAME

Two more jobs for the sync workflow:

    --keep-alive                 re-enable every fork-owned workflow, which stops
                                 GitHub switching the scheduled ones off after 60
                                 days without repository activity
    --cancel-runs-for-sha SHA    cancel runs that upstream's workflows started for
                                 that commit, then disable those workflows

It needs a token with Actions write on the repository in GH_TOKEN or
GITHUB_TOKEN, or a signed-in `gh` CLI. Running it twice changes nothing the
second time.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from github_rest import GitHub, GitHubError, token_from_environment

WORKFLOW_PREFIX = ".github/workflows/"
# The caller workflow on `desktop`; it is not a file on `main`, so it is named here.
DESKTOP_CALLER = ".github/workflows/platform-desktop-build.yml"
DEFAULT_WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
POLL_SECONDS = 5.0
# GitHub answers 409 when asked to cancel a run that has already finished.
ALREADY_FINISHED = 409

KEEP = "keep"
MANAGED = "managed"
DISABLE = "disable"
ALREADY_OFF = "already-off"


@dataclass
class Plan:
    keep: list[dict[str, Any]] = field(default_factory=list)
    managed: list[dict[str, Any]] = field(default_factory=list)
    disable: list[dict[str, Any]] = field(default_factory=list)
    already_off: list[dict[str, Any]] = field(default_factory=list)

    @property
    def inactive_fork_owned(self) -> list[dict[str, Any]]:
        return [workflow for workflow in self.keep if workflow.get("state") != "active"]


@dataclass
class KeepAlive:
    enabled: list[str] = field(default_factory=list)
    left_off: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass
class Sweep:
    """What `--cancel-runs-for-sha` found among the runs of one commit."""

    cancelled: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    # The fork's own caller workflow started a build for the commit.
    caller_run: bool = False


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    allowed = fork_owned_paths(args.workflows_dir, [DESKTOP_CALLER, *args.also_allow])
    client = GitHub(args.repo, token_from_environment())
    plan = build_plan(client.pages("/actions/workflows", key="workflows"), allowed)
    failures = [] if args.dry_run else disable_all(client, plan.disable)
    lines = [render_summary(plan, allowed, failures, dry_run=args.dry_run)]

    if args.keep_alive:
        kept = keep_alive(client, plan.keep, dry_run=args.dry_run)
        lines.append(render_keep_alive(kept, dry_run=args.dry_run))
        for failure in kept.failures:
            print(f"::warning title=Keep-alive failed::{failure}")

    if args.cancel_runs_for_sha:
        sweep = cancel_foreign_runs(
            client,
            args.cancel_runs_for_sha,
            allowed,
            watch_seconds=args.watch_seconds,
            dry_run=args.dry_run,
        )
        failures += sweep.failures
        write_output("caller_run", str(sweep.caller_run).lower())
        # A workflow that GitHub had not listed before its first run is listed now.
        late = build_plan(client.pages("/actions/workflows", key="workflows"), allowed)
        late_failures = [] if args.dry_run else disable_all(client, late.disable)
        failures += late_failures
        lines.append(
            render_sweep(
                sweep,
                late,
                late_failures,
                args.cancel_runs_for_sha,
                dry_run=args.dry_run,
            )
        )

    summary = "\n\n".join(lines)
    print(summary)
    append_step_summary(summary)
    return 1 if failures else 0


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="OWNER/NAME (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--workflows-dir",
        type=Path,
        default=DEFAULT_WORKFLOWS_DIR,
        help="directory holding the fork-owned workflow files (default: this checkout's .github/workflows)",
    )
    parser.add_argument(
        "--also-allow",
        action="append",
        default=[],
        metavar="PATH",
        help="another workflow path to leave enabled, e.g. .github/workflows/x.yml (repeatable)",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help="re-enable every fork-owned workflow that is active or was disabled for inactivity",
    )
    parser.add_argument(
        "--cancel-runs-for-sha",
        metavar="SHA",
        help="cancel unfinished runs of workflows the fork does not own for this commit",
    )
    parser.add_argument(
        "--watch-seconds",
        type=float,
        default=0.0,
        help="with --cancel-runs-for-sha: keep looking for new runs for this long (default: look once)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be disabled or cancelled; change nothing",
    )
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo is required when GITHUB_REPOSITORY is not set")
    if args.cancel_runs_for_sha and not COMMIT_SHA.fullmatch(args.cancel_runs_for_sha):
        parser.error("--cancel-runs-for-sha takes a full 40-character commit SHA")
    return args


def fork_owned_paths(workflows_dir: Path, extra: Iterable[str]) -> frozenset[str]:
    """Repository paths of the workflow files in `workflows_dir`, plus `extra`.

    Refuses an empty directory: with nothing allow-listed, the script would
    disable the workflow that runs it.
    """
    files = sorted(
        p.name
        for p in workflows_dir.glob("*")
        if p.is_file() and p.suffix in (".yml", ".yaml")
    )
    if not files:
        raise SystemExit(
            f"No workflow files in {workflows_dir}: refusing to build an empty allow-list."
        )
    extras = {normalise(path) for path in extra}
    if stray := sorted(path for path in extras if not path.startswith(WORKFLOW_PREFIX)):
        raise SystemExit(
            f"Allow-list entries must start with {WORKFLOW_PREFIX}: {', '.join(stray)}"
        )
    return frozenset({f"{WORKFLOW_PREFIX}{name}" for name in files} | extras)


def normalise(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("/")


def classify(workflow: dict[str, Any], allowed: frozenset[str]) -> str:
    path = str(workflow.get("path", ""))
    if not path.startswith(WORKFLOW_PREFIX):
        return MANAGED
    if path in allowed:
        return KEEP
    return DISABLE if workflow.get("state") == "active" else ALREADY_OFF


def build_plan(workflows: Iterable[dict[str, Any]], allowed: frozenset[str]) -> Plan:
    plan = Plan()
    buckets = {
        KEEP: plan.keep,
        MANAGED: plan.managed,
        DISABLE: plan.disable,
        ALREADY_OFF: plan.already_off,
    }
    for workflow in sorted(workflows, key=lambda item: str(item.get("path", ""))):
        buckets[classify(workflow, allowed)].append(workflow)
    return plan


def disable_all(client: GitHub, workflows: Iterable[dict[str, Any]]) -> list[str]:
    """Disables each workflow; returns one message per failure instead of stopping at the first."""
    failures = []
    for workflow in workflows:
        try:
            client.request("PUT", f"/actions/workflows/{workflow['id']}/disable")
        except GitHubError as error:
            failures.append(f"{workflow['path']}: {error}")
    return failures


def keep_alive(
    client: GitHub, fork_owned: Iterable[dict[str, Any]], *, dry_run: bool
) -> KeepAlive:
    """Re-enables the fork's workflows, which resets GitHub's 60-day inactivity timer.

    A workflow somebody disabled by hand stays off: that was a decision.
    """
    result = KeepAlive()
    for workflow in fork_owned:
        label = f"{workflow['path']} ({workflow.get('state')})"
        if workflow.get("state") not in ("active", "disabled_inactivity"):
            result.left_off.append(label)
            continue
        try:
            if not dry_run:
                client.request("PUT", f"/actions/workflows/{workflow['id']}/enable")
            result.enabled.append(label)
        except GitHubError as error:
            result.failures.append(f"{workflow['path']}: {error}")
    return result


def run_path(run: dict[str, Any]) -> str:
    """The workflow file of a run; GitHub appends `@ref` for some kinds of run."""
    return str(run.get("path", "")).split("@")[0]


def is_foreign_run(run: dict[str, Any], allowed: frozenset[str]) -> bool:
    """An unfinished run of a workflow file the fork does not own."""
    path = run_path(run)
    return (
        path.startswith(WORKFLOW_PREFIX)
        and path not in allowed
        and run.get("status") != "completed"
    )


def cancel_foreign_runs(
    client: GitHub,
    sha: str,
    allowed: frozenset[str],
    *,
    watch_seconds: float = 0.0,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Sweep:
    """Cancels the runs upstream's workflows started for commit `sha`.

    A push made with a personal access token starts `on: push` workflows, and a
    workflow can only be disabled once GitHub lists it, which may be after its
    first run was queued. Runs appear a few seconds after the push, so the
    list is read repeatedly for `watch_seconds`.
    """
    sweep = Sweep()
    handled: set[int] = set()
    deadline = clock() + watch_seconds
    while True:
        runs = list(
            client.pages("/actions/runs", query={"head_sha": sha}, key="workflow_runs")
        )
        sweep.caller_run = sweep.caller_run or any(
            run_path(run) == DESKTOP_CALLER for run in runs
        )
        for run in runs:
            if run["id"] in handled or not is_foreign_run(run, allowed):
                continue
            handled.add(run["id"])
            label = f"{run_path(run)} (run {run['id']})"
            failure = "" if dry_run else cancel(client, run["id"])
            if failure:
                sweep.failures.append(f"{label}: {failure}")
            else:
                sweep.cancelled.append(label)
        if clock() >= deadline:
            return sweep
        sleep(POLL_SECONDS)


def cancel(client: GitHub, run_id: int) -> str:
    """Returns "" when the run is cancelled or already over, else the reason it is not."""
    try:
        client.request("POST", f"/actions/runs/{run_id}/cancel")
    except GitHubError as error:
        if error.status != ALREADY_FINISHED:
            return str(error)
    return ""


def render_summary(
    plan: Plan, allowed: frozenset[str], failures: list[str], *, dry_run: bool
) -> str:
    verb = "Would disable" if dry_run else "Disabled"
    done = len(plan.disable) - len(failures)
    lines = [
        "Upstream workflow check" + (" (dry run, nothing changed)" if dry_run else ""),
        f"  {verb}: {len(plan.disable) if dry_run else done}",
        f"  Already disabled: {len(plan.already_off)}",
        f"  Fork-owned, left enabled: {len(plan.keep)}",
        f"  Managed by GitHub, left alone: {len(plan.managed)}",
    ]
    lines += section(f"{verb}:", (w["path"] for w in plan.disable))
    lines += section("FAILED to disable:", failures)
    lines += section(
        "WARNING: fork-owned but not active (enable with `gh workflow enable <file>`):",
        (f"{w['path']} ({w.get('state')})" for w in plan.inactive_fork_owned),
    )
    seen = {str(w.get("path")) for w in plan.keep}
    lines += section(
        "Allow-listed but not registered with GitHub yet:", sorted(allowed - seen)
    )
    return "\n".join(lines)


def render_keep_alive(result: KeepAlive, *, dry_run: bool) -> str:
    verb = "Would re-enable" if dry_run else "Re-enabled"
    lines = [f"Keep-alive: {verb.lower()} {len(result.enabled)} fork-owned workflow(s)"]
    lines += section(f"{verb} (state before):", result.enabled)
    lines += section("Left off, disabled by hand:", result.left_off)
    lines += section("FAILED to re-enable:", result.failures)
    return "\n".join(lines)


def render_sweep(
    sweep: Sweep,
    late: Plan,
    late_failures: list[str],
    sha: str,
    *,
    dry_run: bool,
) -> str:
    verb = "Would cancel" if dry_run else "Cancelled"
    lines = [
        f"Runs for {sha[:10]}",
        f"  {verb}: {len(sweep.cancelled)}",
        f"  The desktop caller started a build: {'yes' if sweep.caller_run else 'no'}",
    ]
    lines += section(f"{verb}:", sweep.cancelled)
    lines += section("FAILED to cancel:", sweep.failures)
    lines += section(
        "Would disable after the runs:" if dry_run else "Disabled after the runs:",
        (w["path"] for w in late.disable),
    )
    lines += section("FAILED to disable after the runs:", late_failures)
    return "\n".join(lines)


def section(title: str, items: Iterable[str]) -> list[str]:
    rows = [f"  - {item}" for item in items]
    return ["", title, *rows] if rows else []


def append_step_summary(summary: str) -> None:
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"```\n{summary}\n```\n")


def write_output(name: str, value: str) -> None:
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


if __name__ == "__main__":
    sys.exit(main())
