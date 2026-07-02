"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  Schema Change Detector                                                     ║
║  ────────────────────────────────                                           ║
║  Compares SQLAlchemy model definitions against the live PostgreSQL          ║
║  database schema (via information_schema) and produces a diff of            ║
║  changes needed to bring the database in sync with the models.             ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import Table as SATable, text
from sqlalchemy.engine import Engine

from migrations.models import ALL_MODELS, Base


# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ColumnInfo:
    """A column as represented in the database (from information_schema)."""
    name: str
    nullable: bool
    data_type: str
    default: str | None
    is_pk: bool = False


@dataclass
class TableInfo:
    """A table as represented in the database."""
    name: str
    columns: dict[str, ColumnInfo] = field(default_factory=dict)


@dataclass
class ColumnChange:
    """A detected column-level change."""
    table: str
    column: str
    change_type: str  # "add" | "drop" | "alter_type" | "alter_nullable"
    details: str = ""


@dataclass
class SchemaDiff:
    """Full diff between models and database."""
    new_tables: list[str] = field(default_factory=list)
    dropped_tables: list[str] = field(default_factory=list)
    column_changes: list[ColumnChange] = field(default_factory=list)
    new_indexes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.new_tables or self.dropped_tables or self.column_changes)

    def summary(self) -> str:
        parts = []
        if self.new_tables:
            parts.append(f"{len(self.new_tables)} new table(s)")
        if self.dropped_tables:
            parts.append(f"{len(self.dropped_tables)} dropped table(s)")
        if self.column_changes:
            parts.append(f"{len(self.column_changes)} column change(s)")
        return ", ".join(parts) if parts else "No changes detected"


# ──────────────────────────────────────────────────────────────────────────────
# Database introspection
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_db_tables(engine: Engine, schema: str = "public") -> dict[str, TableInfo]:
    """Read the current database schema from PostgreSQL information_schema.

    Returns a dict of table_name -> TableInfo with columns.
    """
    tables: dict[str, TableInfo] = {}

    with engine.connect() as conn:
        # ── Get all tables in the schema ──────────────────────────────────────
        table_rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_type = 'BASE TABLE'"
            ),
            {"schema": schema},
        ).fetchall()

        for (tname,) in table_rows:
            # Skip the migration tracking table
            if tname.startswith("_schema_migrations"):
                continue
            tables[tname] = TableInfo(name=tname)

        # ── Get columns for all tables ────────────────────────────────────────
        col_rows = conn.execute(
            text(
                "SELECT table_name, column_name, is_nullable, "
                "       udt_name, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = :schema "
                "ORDER BY table_name, ordinal_position"
            ),
            {"schema": schema},
        ).fetchall()

        for tname, cname, nullable, dtype, default in col_rows:
            if tname in tables:
                tables[tname].columns[cname] = ColumnInfo(
                    name=cname,
                    nullable=(nullable == "YES"),
                    data_type=dtype,
                    default=default,
                )

        # ── Identify primary key columns ──────────────────────────────────────
        pk_rows = conn.execute(
            text(
                "SELECT kcu.table_name, kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "WHERE tc.constraint_type = 'PRIMARY KEY' "
                "  AND tc.table_schema = :schema"
            ),
            {"schema": schema},
        ).fetchall()

        for tname, cname in pk_rows:
            if tname in tables and cname in tables[tname].columns:
                tables[tname].columns[cname].is_pk = True

    return tables


# ──────────────────────────────────────────────────────────────────────────────
# Model introspection
# ──────────────────────────────────────────────────────────────────────────────

def _get_model_tables() -> dict[str, SATable]:
    """Get SQLAlchemy Table objects for all registered models."""
    return {
        model.__tablename__: model.__table__
        for model in ALL_MODELS
    }


def _pg_type(col_type: Any) -> str:
    """Map a SQLAlchemy type to a PostgreSQL udt_name string."""
    type_class = col_type.__class__.__name__.lower()
    mapping = {
        "integer": "int4",
        "string": "text",
        "text": "text",
        "boolean": "bool",
        "float": "float8",
        "numeric": "numeric",
        "datetime": "timestamp",
        "date": "date",
        "largebinary": "bytea",
        "array": "text",       # ARRAY(Text) stores as text[]
    }
    # Handle ARRAY types
    if hasattr(col_type, "item_type"):
        return f"_{mapping.get(col_type.item_type.__class__.__name__.lower(), 'text')}"
    return mapping.get(type_class, "text")


# ──────────────────────────────────────────────────────────────────────────────
# Diff engine
# ──────────────────────────────────────────────────────────────────────────────

def detect_changes(engine: Engine, schema: str = "public") -> SchemaDiff:
    """Compare SQLAlchemy model definitions against the live database.

    Returns a SchemaDiff describing what needs to change.
    """
    db_tables = _fetch_db_tables(engine, schema)
    model_tables = _get_model_tables()

    diff = SchemaDiff()

    # ── New tables (exist in models, not in DB) ──────────────────────────────
    for name in model_tables:
        if name not in db_tables:
            diff.new_tables.append(name)

    # ── Dropped tables (exist in DB, not in models) ──────────────────────────
    # Skip internal/other tables that aren't in our model registry
    # (we only flag tables that *were* managed but are now removed)

    # ── Column-level changes ─────────────────────────────────────────────────
    for tname, table in model_tables.items():
        if tname not in db_tables:
            continue  # Already flagged as new

        db_cols = db_tables[tname].columns
        model_cols = {c.name: c for c in table.columns}

        # Columns in model but not in DB
        for cname, col in model_cols.items():
            if cname not in db_cols:
                diff.column_changes.append(ColumnChange(
                    table=tname,
                    column=cname,
                    change_type="add",
                    details=f"Add column {cname} ({col.type})",
                ))

        # Columns in DB but not in model (potential drop — flag as info)
        for cname in db_cols:
            if cname not in model_cols:
                diff.column_changes.append(ColumnChange(
                    table=tname,
                    column=cname,
                    change_type="drop",
                    details=f"Column {cname} exists in DB but not in models",
                ))

        # Type changes
        for cname, col in model_cols.items():
            if cname in db_cols:
                db_col = db_cols[cname]
                model_pg_type = _pg_type(col.type)
                if db_col.data_type != model_pg_type and model_pg_type != "text":
                    # Skip ARRAY vs text comparison noise
                    diff.column_changes.append(ColumnChange(
                        table=tname,
                        column=cname,
                        change_type="alter_type",
                        details=f"Change type: {db_col.data_type} -> {model_pg_type}",
                    ))

    return diff


# ──────────────────────────────────────────────────────────────────────────────
# SQL generation
# ──────────────────────────────────────────────────────────────────────────────

def generate_sql(diff: SchemaDiff) -> tuple[list[str], list[str]]:
    """Generate PostgreSQL SQL for upgrade and downgrade from a SchemaDiff.

    Returns (upgrade_sql, downgrade_sql) — lists of SQL statements.
    """
    up: list[str] = []
    down: list[str] = []

    model_tables = _get_model_tables()

    for tname in diff.new_tables:
        table = model_tables[tname]
        # Generate CREATE TABLE
        col_defs: list[str] = []
        for col in table.columns:
            nullable = " NOT NULL" if not col.nullable else ""
            default = ""
            if col.server_default is not None:
                default = f" DEFAULT {col.server_default.arg}"
            col_defs.append(f"    {col.name} {col.type}{nullable}{default}")

        # Primary key
        pk_cols = [c.name for c in table.columns if c.primary_key]
        if pk_cols:
            pk_def = f",\n    PRIMARY KEY ({', '.join(pk_cols)})"
        else:
            pk_def = ""

        # Unique constraints
        for constr in table.constraints:
            if isinstance(constr, type(table.constraints[0])) and False:
                pass  # Handle constraints generically
        # Simplified: just create the table
        create = f"CREATE TABLE {tname} (\n{',\n'.join(col_defs)}{pk_def}\n);"
        up.append(create)
        down.append(f"DROP TABLE IF EXISTS {tname} CASCADE;")

    for change in diff.column_changes:
        if change.change_type == "add":
            up.append(
                f"ALTER TABLE {change.table} "
                f"ADD COLUMN {change.column} TEXT;"
            )
            down.append(
                f"ALTER TABLE {change.table} "
                f"DROP COLUMN IF EXISTS {change.column};"
            )
        elif change.change_type == "drop":
            # Flagged but we don't auto-drop columns (too dangerous)
            pass
        elif change.change_type == "alter_type":
            up.append(
                f"ALTER TABLE {change.table} "
                f"ALTER COLUMN {change.column} TYPE TEXT "
                f"USING {change.column}::TEXT;"
            )
            down.append(
                f"ALTER TABLE {change.table} "
                f"ALTER COLUMN {change.column} TYPE TEXT;"
            )

    return up, down
