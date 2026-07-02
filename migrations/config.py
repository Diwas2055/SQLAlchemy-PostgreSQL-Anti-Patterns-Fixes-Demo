"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  Migration Configuration                                                    ║
║  ─────────────────────────────────────                                     ║
║  Loads settings from pyproject.toml or environment variables.               ║
║  Configuration priority: CLI flag > env var > config file > defaults       ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).parent / "pyproject.toml"
DEFAULT_VERSIONS_DIR = Path(__file__).parent / "versions"
DEFAULT_MIGRATIONS_TABLE = "_schema_migrations"


@dataclass
class MigrationConfig:
    """Central configuration for the migration tool."""

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "PG_URL",
            "postgresql://postgres:postgres@localhost:5432/sqlalchemy_demo",
        )
    )
    """PostgreSQL connection string."""

    # ── Migrations ────────────────────────────────────────────────────────────
    migrations_table: str = DEFAULT_MIGRATIONS_TABLE
    """Name of the tracking table in PostgreSQL."""

    versions_dir: str = str(DEFAULT_VERSIONS_DIR)
    """Directory where migration version files are stored."""

    # ── Behavior ──────────────────────────────────────────────────────────────
    auto_generate: bool = True
    """Auto-detect changes from models when creating a migration."""

    sqlalchemy_echo: bool = False
    """Echo SQLAlchemy SQL to stdout (debug)."""

    # ── PostgreSQL-specific ───────────────────────────────────────────────────
    postgres_schema: str = "public"
    """Database schema to inspect for existing tables."""

    @classmethod
    def load(cls, config_path: str | None = None) -> MigrationConfig:
        """Load configuration from TOML file, overlaying environment variables.

        The TOML file is optional — all settings have sensible defaults
        or can be set via environment variables.
        """
        cfg = cls()

        # ── Load from TOML if it exists ──────────────────────────────────────
        path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        if path.exists():
            try:
                with open(path, "rb") as f:
                    data: dict[str, Any] = tomllib.load(f)
                tool_data = data.get("tool", {}).get("migrations", {})
                cfg._apply_toml(tool_data)
            except (tomllib.TOMLDecodeError, OSError) as e:
                print(f"  ⚠️  Warning: could not load config from {path}: {e}")

        # ── Environment variable overrides ────────────────────────────────────
        cfg.database_url = os.environ.get("PG_URL", cfg.database_url)

        return cfg

    def _apply_toml(self, data: dict[str, Any]) -> None:
        """Apply values from parsed TOML data."""
        for key, val in data.items():
            if hasattr(self, key):
                if isinstance(val, str) and key in ("versions_dir",):
                    # Resolve relative to the config file location
                    val = str(Path(DEFAULT_CONFIG_PATH.parent, val))
                setattr(self, key, val)
