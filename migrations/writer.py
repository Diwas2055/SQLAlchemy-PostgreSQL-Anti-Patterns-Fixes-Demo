"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  Migration File Writer                                                     ║
║  ────────────────────────────                                               ║
║  Generates timestamped Python migration files with upgrade() and           ║
║  downgrade() functions containing PostgreSQL SQL statements.                ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from migrations.detector import SchemaDiff, generate_sql


MIGRATION_TEMPLATE = '''\
"""
Migration: {migration_id}
Created:   {created_at}
Description: {description}

╔══════════════════════════════════════════════════════════════════════════════╗
║  Auto-generated migration                                                   ║
║  Upgrade:  {upgrade_summary}
║  Downgrade: Reverses the above                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations


MIGRATION_ID = "{migration_id}"
"""Unique identifier for this migration (timestamp + sequence)."""

REVISION = "{revision}"
"""Previous migration ID for rollback ordering."""


def upgrade(connection) -> None:
    """Apply the migration."""
{upgrade_statements}


def downgrade(connection) -> None:
    """Roll back the migration."""
{downgrade_statements}
'''


def write_migration(
    versions_dir: str,
    description: str,
    upgrade_sql: list[str],
    downgrade_sql: list[str],
    revision: str = "base",
) -> str | None:
    """Generate and write a migration version file.

    Args:
        versions_dir: Path to the versions directory.
        description: Human-readable description of the migration.
        upgrade_sql: List of SQL statements for upgrade.
        downgrade_sql: List of SQL statements for downgrade.
        revision: Previous migration ID (or "base" for the first one).

    Returns:
        The migration ID (filename stem) if written, or None if no changes.
    """
    if not upgrade_sql and not downgrade_sql:
        return None

    # ── Create migration ID from timestamp ────────────────────────────────────
    now = datetime.now(timezone.utc)
    migration_id = now.strftime("%Y%m%d_%H%M%S")

    # ── Format SQL into indented function bodies ──────────────────────────────
    def _indent(stmts: list[str], level: int = 2) -> str:
        """Indent SQL statements for embedding in Python.

        Produces clean output like::

            connection.execute("CREATE TABLE ...")
            connection.execute("ALTER TABLE ...")
        """
        if not stmts:
            return "    pass"
        lines = []
        for stmt in stmts:
            if stmt.startswith("--"):
                lines.append(f"{' ' * (level * 2)}# {stmt[3:].lstrip()}")
            else:
                lines.append(f"{' ' * (level * 2)}connection.execute({stmt!r})")
        return "\n".join(lines) if lines else "    pass"

    upgrade_code = _indent(upgrade_sql)
    downgrade_code = _indent(downgrade_sql)

    # ── Count statements for summary ──────────────────────────────────────────
    n_up = len([s for s in upgrade_sql if not s.startswith("--")])
    n_down = len([s for s in downgrade_sql if not s.startswith("--")])
    upgrade_summary = f"{n_up} statement(s)" if n_up else "No changes"

    # ── Generate content ─────────────────────────────────────────────────────
    content = MIGRATION_TEMPLATE.format(
        migration_id=migration_id,
        created_at=now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        description=description,
        upgrade_summary=upgrade_summary,
        revision=revision,
        upgrade_statements=upgrade_code,
        downgrade_statements=downgrade_code,
    )

    # ── Write file ────────────────────────────────────────────────────────────
    path = Path(versions_dir) / f"{migration_id}.py"
    os.makedirs(str(Path(versions_dir)), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)

    print(f"  ✅  Created migration: {path.name}")
    return migration_id


def write_from_diff(
    versions_dir: str,
    description: str,
    diff: SchemaDiff,
    revision: str = "base",
) -> str | None:
    """Generate a migration file from a SchemaDiff.

    Convenience wrapper that calls ``generate_sql()`` then ``write_migration()``.
    """
    upgrade_sql, downgrade_sql = generate_sql(diff)
    return write_migration(
        versions_dir=versions_dir,
        description=description,
        upgrade_sql=upgrade_sql,
        downgrade_sql=downgrade_sql,
        revision=revision,
    )
