"""Agent runs and AutoPilot turns that were in flight when a process died.

An executor takes a lock in Valkey for each run it works on, valid for five
minutes and written to disk. A new executor that finds the lock of a dead
one takes the run for someone else's, drops its message, and the run stays
"running" for ever. On a desktop there is one executor, so a lock that
exists before it starts is always a dead one's: `Cache.clear_stale_locks`
removes them before every start of the process that hosts an executor, at
boot and when the supervisor restarts it. The run's message is still in the
queue, and the platform then picks the run up where it left off.

`reconcile` is the other half, run once per boot before any service starts:
a run the user chose to stop does not come back, and one that has been
"running" for more than a day is not resumed out of the blue.

Nothing here may stop the app from starting. Every name below is upstream's
(tests/test_upstream_names.py checks them), and every failure is logged and
skipped.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("autogpt_desktop")

# Service name (apps.SERVICES) -> the lock keys its executor takes:
# backend/executor/manager.py and copilot/executor/utils.get_session_lock_key.
LOCK_PATTERNS = {
    "executor": "exec_lock:*",
    "copilot-executor": "copilot:session:*:lock",
}

GRAPH_EXECUTION_TABLE = "AgentGraphExecution"
GRAPH_EXECUTION_COLUMNS = ("id", "executionStatus", "createdAt", "updatedAt", "isDeleted")
NODE_EXECUTION_TABLE = "AgentNodeExecution"
NODE_EXECUTION_COLUMNS = ("executionStatus", "endedTime", "agentGraphExecutionId")
EXECUTION_STATUSES = ("QUEUED", "RUNNING", "TERMINATED", "FAILED")

# Runs the user chose to stop as the app closed: a list of run ids, left by a
# stop that could not finish ending them itself.
STOPPED_RUNS_FILE = "stopped-runs.json"
RESUME_WINDOW_HOURS = 24

UNFINISHED_AND_OLD = f"""\
SELECT id FROM platform."{GRAPH_EXECUTION_TABLE}"
WHERE "executionStatus" IN ('RUNNING', 'QUEUED') AND NOT "isDeleted"
AND "createdAt" < (now() AT TIME ZONE 'utc') - interval '{RESUME_WINDOW_HOURS} hours'
"""
# The platform's own shape for ending a run from outside the executor
# (backend/data/diagnostics.py, and the node cascade in data/execution.py).
END_NODES = f"""\
UPDATE platform."{NODE_EXECUTION_TABLE}"
SET "executionStatus" = 'FAILED', "endedTime" = now() AT TIME ZONE 'utc'
WHERE "executionStatus" = 'RUNNING' AND "agentGraphExecutionId" = ANY(%(ids)s)
"""
END_RUNS = f"""\
UPDATE platform."{GRAPH_EXECUTION_TABLE}"
SET "executionStatus" = %(status)s, "updatedAt" = now() AT TIME ZONE 'utc'
WHERE id = ANY(%(ids)s) AND "executionStatus" IN ('RUNNING', 'QUEUED') AND NOT "isDeleted"
"""


@dataclass(frozen=True)
class Cache:
    """The Valkey node, as the runtime reaches it."""

    port: int
    password: str

    def clear_stale_locks(self, services: Iterable[str]) -> None:
        """Before the process hosting `services` starts: any lock of theirs
        that exists belongs to a process that is gone."""
        patterns = [LOCK_PATTERNS[name] for name in services if name in LOCK_PATTERNS]
        if not patterns:
            return
        try:
            removed = self._delete_matching(patterns)
        except Exception as exc:  # a lock left behind costs one run, not the app
            logger.warning(f"could not clear stale run locks: {exc}")
            return
        if removed:
            logger.info(f"cleared {removed} lock(s) left by a process that is gone")

    def _delete_matching(self, patterns: list[str]) -> int:
        import redis
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        # No retries: this runs in the supervisor's own loop, and against a
        # node that is down each refused connection costs seconds on Windows.
        client = redis.Redis(
            host="127.0.0.1",
            port=self.port,
            password=self.password,
            socket_timeout=5,
            socket_connect_timeout=3,
            retry=Retry(NoBackoff(), 0),
        )
        removed = 0
        with closing(client):
            for pattern in patterns:
                # One key per command: the node is in cluster mode, where a
                # command naming keys from different slots is refused.
                for key in client.scan_iter(match=pattern, count=500):
                    removed += int(client.delete(key))
        return removed


def reconcile(connect: Callable, stopped_runs: Path) -> None:
    """Settle unfinished runs before the executor sees their messages. It
    skips a message whose run has already ended."""
    try:
        with closing(connect()) as connection:
            cursor = connection.cursor()
            stopped = _stopped_by_the_user(stopped_runs)
            _end(cursor, stopped, "TERMINATED", "stopped as AutoGPT closed")
            cursor.execute(UNFINISHED_AND_OLD)
            stale = [row[0] for row in cursor.fetchall()]
            _end(cursor, stale, "FAILED", f"interrupted over {RESUME_WINDOW_HOURS} hours ago")
            connection.commit()
    except Exception as exc:  # upstream moved a table: runs stay as they are
        logger.warning(f"could not settle interrupted agent runs: {exc}")
        return
    try:
        stopped_runs.unlink(missing_ok=True)
    except OSError as exc:
        # Applied again next time, which changes nothing: only runs that are
        # still unfinished are ended.
        logger.warning(f"could not remove {stopped_runs.name}: {exc}")


def _end(cursor, ids: list[str], status: str, why: str) -> None:
    if not ids:
        return
    arguments = {"ids": ids, "status": status}
    cursor.execute(END_NODES, arguments)
    cursor.execute(END_RUNS, arguments)
    if cursor.rowcount:
        logger.info(f"marked {cursor.rowcount} agent run(s) {status}: {why}")


def _stopped_by_the_user(path: Path) -> list[str]:
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning(f"ignoring an unreadable {path.name}: {exc}")
        return []
    if not isinstance(recorded, list):
        return []
    return [run for run in recorded if isinstance(run, str)]
