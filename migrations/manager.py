"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  Migration Manager                                                         ║
║  ────────────────────────                                                   ║
║  Core engine: tracks applied migrations in PostgreSQL, executes             ║
║  upgrade/downgrade functions, and manages the migration state.              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import importlib.util as import_util
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from migrations.config import MigrationConfig


# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MigrationRecord:
    """A single row from the _schema_migrations tracking table."""
    id: str
    description: str
    applied_at: datetime
    batch: int


@dataclass
class MigrationFile:
    """A migration version file loaded from disk."""
    id: str
    path: str
    description: str
    upgrade_fn: Callable | None
    downgrade_fn: Callable | None


# ──────────────────────────────────────────────────────────────────────────────
# Tracking table management
# ──────────────────────────────────────────────────────────────────────────────

TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    id          TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    batch       INTEGER NOT NULL DEFAULT 1
);
"""


def ensure_tracking_table(engine: Engine, table_name: str) -> None:
    """Create the _schema_migrations tracking table if it doesn't exist."""
    with engine.begin() as conn:
        conn.execute(text(TRACKING_TABLE_SQL.format(table=table_name)))


def get_applied_migrations(engine: Engine, table_name: str) -> list[MigrationRecord]:
    """Return all applied migrations, ordered by batch then id."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT id, description, applied_at, batch "
                f"FROM {table_name} ORDER BY batch, id"
            )
        ).fetchall()
        return [
            MigrationRecord(id=r[0], description=r[1], applied_at=r[2], batch=r[3])
            for r in rows
        ]


def record_migration(engine: Engine, table_name: str, mid: str, desc: str, batch: int) -> None:
    """Insert a record that a migration was applied."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {table_name} (id, description, batch) "
                f"VALUES (:id, :desc, :batch) "
                f"ON CONFLICT (id) DO NOTHING"
            ),
            {"id": mid, "desc": desc, "batch": batch},
        )


def remove_migration_record(engine: Engine, table_name: str, mid: str) -> None:
    """Remove a migration record (for rollback)."""
    with engine.begin() as conn:
        conn.execute(
            text(f"DELETE FROM {table_name} WHERE id = :id"),
            {"id": mid},
        )


def get_next_batch(engine: Engine, table_name: str) -> int:
    """Determine the next batch number."""
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT COALESCE(MAX(batch), 0) + 1 FROM {table_name}")
        ).scalar()
        return row or 1


# ──────────────────────────────────────────────────────────────────────────────
# Migration file loading
# ──────────────────────────────────────────────────────────────────────────────

def _extract_description(filepath: str) -> str:
    """Extract a human-readable description from a migration file path.

    Converts '20250101_120000_initial_schema.py' to 'initial schema'.
    """
    stem = Path(filepath).stem
    # Remove timestamp prefix (e.g., '20250101_120000_')
    parts = stem.split("_", 2)
    if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
        desc = parts[2]
    elif len(parts) >= 2 and parts[0].isdigit():
        desc = parts[1] if len(parts) > 1 else stem
    else:
        desc = stem
    # Replace underscores with spaces
    return desc.replace("_", " ").capitalize()


def load_migration_files(versions_dir: str) -> list[MigrationFile]:
    """Scan the versions directory and return sorted MigrationFile objects."""
    path = Path(versions_dir)
    if not path.exists():
        return []

    files: list[MigrationFile] = []
    pattern = re.compile(r"^\d{8}_\d{6}.*\.py$")

    for fpath in sorted(path.iterdir()):
        if not fpath.is_file() or not pattern.match(fpath.name):
            continue

        migration_id = fpath.stem
        description = _extract_description(str(fpath))

        # Dynamically load the module
        spec = import_util.spec_from_file_location(migration_id, str(fpath))
        if spec and spec.loader:
            mod = import_util.module_from_spec(spec)
            # Temporarily add the versions dir to sys.path for imports
            sys.path.insert(0, str(path))
            try:
                spec.loader.exec_module(mod)
            finally:
                sys.path.pop(0)

            upgrade_fn = getattr(mod, "upgrade", None)
            downgrade_fn = getattr(mod, "downgrade", None)
        else:
            upgrade_fn = None
            downgrade_fn = None

        files.append(MigrationFile(
            id=migration_id,
            path=str(fpath),
            description=description,
            upgrade_fn=upgrade_fn,
            downgrade_fn=downgrade_fn,
        ))

    return files


# ──────────────────────────────────────────────────────────────────────────────
# Migration execution
# ──────────────────────────────────────────────────────────────────────────────

def run_migrations(cfg: MigrationConfig, target: str | None = None) -> None:
    """Apply all pending migrations (or up to a specific target).

    Args:
        cfg: Migration configuration.
        target: Optional migration ID to migrate up to. If None, runs all.
    """
    engine = _get_engine(cfg)
    ensure_tracking_table(engine, cfg.migrations_table)
    applied = get_applied_migrations(engine, cfg.migrations_table)
    applied_ids = {r.id for r in applied}
    files = load_migration_files(cfg.versions_dir)

    pending = [f for f in files if f.id not in applied_ids]

    if not pending:
        print("  ✨  Nothing to migrate — all migrations are current.")
        engine.dispose()
        return

    # Filter up to target
    if target:
        pending = [f for f in pending if f.id <= target]

    if not pending:
        print(f"  ✨  No pending migrations up to {target}.")
        engine.dispose()
        return

    batch = get_next_batch(engine, cfg.migrations_table)
    print(f"\n  🚀  Running {len(pending)} migration(s) (batch #{batch})...\n")

    for mf in pending:
        print(f"  ── {mf.id}  {mf.description} ──")
        if mf.upgrade_fn:
            with engine.begin() as conn:
                mf.upgrade_fn(conn)
            record_migration(engine, cfg.migrations_table, mf.id, mf.description, batch)
            print(f"     ✅  Applied")
        else:
            print(f"     ⚠️  No upgrade function found, skipped")

    engine.dispose()
    print(f"\n  ✅  Migration complete.\n")


def rollback_migrations(cfg: MigrationConfig, steps: int = 1) -> None:
    """Roll back the last batch of migrations.

    Args:
        cfg: Migration configuration.
        steps: Number of batches to roll back (default 1).
    """
    engine = _get_engine(cfg)
    ensure_tracking_table(engine, cfg.migrations_table)
    applied = get_applied_migrations(engine, cfg.migrations_table)
    files = {f.id: f for f in load_migration_files(cfg.versions_dir)}

    if not applied:
        print("  ✨  Nothing to roll back.")
        engine.dispose()
        return

    # Group by batch
    batches: dict[int, list[MigrationRecord]] = {}
    for rec in applied:
        batches.setdefault(rec.batch, []).append(rec)

    sorted_batches = sorted(batches.keys(), reverse=True)
    batches_to_rollback = sorted_batches[:steps]

    total = sum(len(batches[b]) for b in batches_to_rollback)
    print(f"\n  🔙  Rolling back {total} migration(s) across {len(batches_to_rollback)} batch(es)...\n")

    for batch_num in batches_to_rollback:
        for rec in reversed(batches[batch_num]):
            mf = files.get(rec.id)
            desc = mf.description if mf else rec.id
            print(f"  ── {rec.id}  {desc} ──")
            if mf and mf.downgrade_fn:
                with engine.begin() as conn:
                    mf.downgrade_fn(conn)
                remove_migration_record(engine, cfg.migrations_table, rec.id)
                print(f"     ✅  Rolled back")
            else:
                raise RuntimeError(
                    f"Cannot roll back {rec.id}: no downgrade() function found. "
                    f"The tracking record will NOT be deleted — fix the migration "
                    f"file or add a downgrade function manually."
                )

    engine.dispose()
    print(f"\n  ✅  Rollback complete.\n")


def show_status(cfg: MigrationConfig) -> None:
    """Print the current migration status (read-only — no side effects)."""
    engine = _get_engine(cfg)

    # First check if the tracking table exists — if not, nothing is applied
    from sqlalchemy import inspect as sa_inspect
    inspector = sa_inspect(engine)
    if not inspector.has_table(cfg.migrations_table, schema=cfg.postgres_schema):
        print(f"\n  ℹ️   Tracking table <comment>{cfg.migrations_table}</comment> does not exist.")
        print(f"       Run <info>init</info> first to create it.\n")
        engine.dispose()
        return

    applied = get_applied_migrations(engine, cfg.migrations_table)
    applied_ids = {r.id for r in applied}
    files = load_migration_files(cfg.versions_dir)

    print(f"\n{'=' * 60}")
    print(f"  Migration Status")
    print(f"{'=' * 60}")
    print(f"  {'Migration ID':<22} {'Description':<25} {'Status':<10}")
    print(f"  {'─' * 22}  {'─' * 25}  {'─' * 10}")

    for mf in files:
        status = "✅ Applied" if mf.id in applied_ids else "⬜ Pending"
        print(f"  {mf.id:<22} {mf.description:<25} {status:<10}")

    if not files:
        print(f"  No migration files found in {cfg.versions_dir}")

    print(f"\n  Applied: {len(applied)}  |  Pending: {len(files) - len(applied_ids)}")
    print()

    engine.dispose()


def _get_engine(cfg: MigrationConfig) -> Engine:
    """Create a SQLAlchemy engine from config."""
    from sqlalchemy import create_engine
    return create_engine(
        cfg.database_url,
        echo=cfg.sqlalchemy_echo,
        pool_pre_ping=True,
    )
