"""Database bootstrap, ported from single-container/bootstrap.sh.

Order and semantics match the appliance: create the schemas, refuse to run
over an interrupted migration, apply Prisma migrations, then lock the
frontend's role down to the auth tables. The role policy SQL is the
appliance's own, extracted from bootstrap.sh when the bundle is built
(see build/frontend_role_sql.py), so the two distributions grant the same
privileges.
"""

from __future__ import annotations

import os
from contextlib import closing

from autogpt_desktop.layout import Bundle
from autogpt_desktop.process import run_tool
from autogpt_desktop.settings import FRONTEND_DB_ROLE

MIGRATION_TIMEOUT_SECONDS = 900


class InterruptedMigrationError(RuntimeError):
    pass


def create_schemas(bundle: Bundle, connect) -> None:
    sql = bundle.init_sql.read_text(encoding="utf-8")
    with closing(_autocommit(connect)) as connection:
        connection.cursor().execute(sql)


def refuse_interrupted_migration(connect) -> None:
    with closing(_autocommit(connect)) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT to_regclass('platform._prisma_migrations') IS NOT NULL")
        (exists,) = cursor.fetchone()
        if not exists:
            return
        cursor.execute(
            "SELECT coalesce(string_agg(migration_name, ', ' ORDER BY started_at), '') "
            "FROM platform._prisma_migrations "
            "WHERE finished_at IS NULL AND rolled_back_at IS NULL"
        )
        (unfinished,) = cursor.fetchone()
    if unfinished:
        raise InterruptedMigrationError(
            f"A database update did not finish ({unfinished}). This usually means "
            "AutoGPT was closed while it was starting for the first time. If you "
            "have no data yet, delete the AutoGPT data folder and start again."
        )


def apply_migrations(bundle: Bundle, env: dict[str, str]) -> None:
    command = [
        *bundle.node_command(),
        str(bundle.prisma_cli),
        "migrate",
        "deploy",
        "--schema",
        str(bundle.backend_dir / "schema.prisma"),
    ]
    result = run_tool(
        command,
        cwd=bundle.backend_dir,
        env={
            **os.environ,
            **env,
            "ELECTRON_RUN_AS_NODE": "1",
            "CHECKPOINT_DISABLE": "1",
            "PRISMA_HIDE_UPDATE_MESSAGE": "1",
        },
        capture_output=True,
        timeout=MIGRATION_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).decode(errors="replace")
        raise RuntimeError(f"prisma migrate deploy failed:\n{output[-4000:]}")


def configure_frontend_role(bundle: Bundle, connect, password: str) -> None:
    policy = (bundle.root / "assets" / "frontend-role.sql").read_text(encoding="utf-8")
    with closing(_autocommit(connect)) as connection:
        cursor = connection.cursor()
        cursor.execute(policy)
        # The appliance authenticates this role by Unix-socket peer auth and
        # leaves it passwordless; loopback TCP needs a password instead.
        cursor.execute(f'ALTER ROLE "{FRONTEND_DB_ROLE}" PASSWORD %s', (password,))


def _autocommit(connect):
    # Not `with connection:` -- psycopg2 opens a transaction for that block
    # even in autocommit mode, and the scripts here manage their own.
    connection = connect()
    connection.autocommit = True
    return connection
