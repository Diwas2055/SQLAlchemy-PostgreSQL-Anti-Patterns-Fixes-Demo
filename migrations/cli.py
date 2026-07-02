#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  Migration CLI — Cleo Application                                           ║
║  ────────────────────────────────                                           ║
║  A PostgreSQL-first migration tool with auto-detection of schema            ║
║  changes from SQLAlchemy model definitions.                                 ║
║                                                                             ║
║  Usage:  python migrations/cli.py [command] [options]                       ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is on sys.path so migrations package is importable
_proj_root = str(Path(__file__).resolve().parent.parent)
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

from cleo.application import Application as CleoApp
from cleo.commands.command import Command
from cleo.helpers import argument, option

from migrations.config import MigrationConfig
from migrations.detector import detect_changes
from migrations.manager import (
    run_migrations,
    rollback_migrations,
    show_status,
)
from migrations.writer import write_from_diff


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_config(command: Command) -> MigrationConfig:
    """Load config from pyproject.toml, with optional database URL override."""
    config_path = command.option("config")
    cfg = MigrationConfig.load(config_path)

    db_url = command.option("database")
    if db_url:
        cfg.database_url = db_url

    return cfg


# ──────────────────────────────────────────────────────────────────────────────
# Command: init
# ──────────────────────────────────────────────────────────────────────────────

class InitCommand(Command):
    name = "init"
    description = "Initialize the migration system: create tracking table and initial migration"

    options = [
        option("database", "d", "Database URL", flag=False),
        option("config", "c", "Path to config file (TOML)", flag=False),
    ]

    def handle(self) -> int:
        cfg = _load_config(self)

        from sqlalchemy import create_engine
        engine = create_engine(cfg.database_url, echo=cfg.sqlalchemy_echo, pool_pre_ping=True)

        from migrations.manager import ensure_tracking_table
        ensure_tracking_table(engine, cfg.migrations_table)
        self.line(f"  ✅  Tracking table <info>{cfg.migrations_table}</info> ready")

        # Create all model tables
        from migrations.models import Base
        Base.metadata.create_all(engine)
        self.line(f"  ✅  All model tables created ({len(ALL_MODELS)} tables)")

        engine.dispose()
        return 0


# ──────────────────────────────────────────────────────────────────────────────
# Command: make:migration
# ──────────────────────────────────────────────────────────────────────────────

class MakeMigrationCommand(Command):
    name = "make:migration"
    description = "Generate a new migration file by auto-detecting schema changes"

    arguments = [
        argument("name", "Description of the migration (e.g., 'add_user_table')")
    ]

    options = [
        option("database", "d", "Database URL", flag=False),
        option("config", "c", "Path to config file (TOML)", flag=False),
        option("no-detect", None, "Skip auto-detection, create empty migration", flag=True),
    ]

    def handle(self) -> int:
        cfg = _load_config(self)
        description = self.argument("name")

        from sqlalchemy import create_engine
        engine = create_engine(cfg.database_url, echo=cfg.sqlalchemy_echo, pool_pre_ping=True)

        diff = None
        revision = "base"

        # ── Auto-detect changes if not suppressed ─────────────────────────────
        if not self.option("no-detect"):
            self.line("  🔍  Auto-detecting schema changes...")
            diff = detect_changes(engine, schema=cfg.postgres_schema)
            self.line(f"      {diff.summary()}")

            # Get the latest migration ID as revision
            from migrations.manager import get_applied_migrations
            applied = get_applied_migrations(engine, cfg.migrations_table)
            if applied:
                revision = applied[-1].id

        # ── Detect also checks for new tables from models not yet in DB ───────
        from migrations.manager import ensure_tracking_table
        ensure_tracking_table(engine, cfg.migrations_table)

        # ── Write the migration file ─────────────────────────────────────────
        if diff and diff.has_changes:
            migration_id = write_from_diff(
                versions_dir=cfg.versions_dir,
                description=description,
                diff=diff,
                revision=revision,
            )
        else:
            # Write an empty migration template
            from migrations.writer import write_migration
            migration_id = write_migration(
                versions_dir=cfg.versions_dir,
                description=description,
                upgrade_sql=[f"-- No auto-detected changes for: {description}"],
                downgrade_sql=[f"-- Reverses: {description}"],
                revision=revision,
            )

        engine.dispose()

        if migration_id:
            self.line(f"  ✅  Migration <info>{migration_id}</info> created")
            self.line(f"      Edit the file in <comment>{cfg.versions_dir}/{migration_id}.py</comment>")
        else:
            self.line("  ℹ️   No migration file was created (no changes detected)")

        return 0


# ──────────────────────────────────────────────────────────────────────────────
# Command: migrate
# ──────────────────────────────────────────────────────────────────────────────

class MigrateCommand(Command):
    name = "migrate"
    description = "Run pending migrations"

    options = [
        option("database", "d", "Database URL", flag=False),
        option("config", "c", "Path to config file (TOML)", flag=False),
        option("target", "t", "Target migration ID to migrate up to", flag=False),
    ]

    def handle(self) -> int:
        cfg = _load_config(self)
        target = self.option("target")
        run_migrations(cfg, target=target)
        return 0


# ──────────────────────────────────────────────────────────────────────────────
# Command: rollback
# ──────────────────────────────────────────────────────────────────────────────

class RollbackCommand(Command):
    name = "rollback"
    description = "Roll back the last batch of migrations"

    options = [
        option("database", "d", "Database URL", flag=False),
        option("config", "c", "Path to config file (TOML)", flag=False),
        option("steps", "s", "Number of batches to roll back", flag=False, default="1"),
    ]

    def handle(self) -> int:
        cfg = _load_config(self)
        steps = int(self.option("steps"))
        rollback_migrations(cfg, steps=steps)
        return 0


# ──────────────────────────────────────────────────────────────────────────────
# Command: status
# ──────────────────────────────────────────────────────────────────────────────

class StatusCommand(Command):
    name = "status"
    description = "Show migration status (applied/pending)"

    options = [
        option("database", "d", "Database URL", flag=False),
        option("config", "c", "Path to config file (TOML)", flag=False),
    ]

    def handle(self) -> int:
        cfg = _load_config(self)
        show_status(cfg)
        return 0


# ──────────────────────────────────────────────────────────────────────────────
# Application bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    """Entry point for the Cleo CLI application."""
    app = CleoApp(
        name="Migration CLI",
        version="1.0.0",
    )

    app.add(InitCommand())
    app.add(MakeMigrationCommand())
    app.add(MigrateCommand())
    app.add(RollbackCommand())
    app.add(StatusCommand())

    return app.run()


if __name__ == "__main__":
    sys.exit(main())
