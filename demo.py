"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  SQLAlchemy + PostgreSQL Anti-Patterns & Fixes                              ║
║  ───────────────────────────────────────────────────────                    ║
║  Demonstrates 12 common database performance issues and their fixes:       ║
║                                                                             ║
║  1. array_position (non-GIN-safe)  →  @> operator (GIN-compatible)         ║
║  2. EXISTS in SELECT (filter in Python) → WHERE EXISTS (filter in DB)      ║
║  3. Python dedup (fetch all, dedup in app) → SQL DISTINCT ON (dedup in DB)║
║  4. N+1 nextval (one round-trip per row) → batched generate_series         ║
║  5. N+1 ORM pattern (one child query per parent) → JOIN once               ║
║  6. WHERE LOWER(col) = value (no index) → col = value (uses index)        ║
║  7. SELECT * (over-fetches all columns) → SELECT specific columns          ║
║  8. NOT IN subquery (slow, wrong with NULLs) → NOT EXISTS (correct, fast)  ║
║  9. Missing FK index (seq scan on JOIN) → FK index (index scan)            ║
║ 10. Deep OFFSET pagination (slow) → keyset/cursor pagination (fast)        ║
║ 11. Implicit type casting (index disabled) → matching types (uses index)   ║
║ 12. Single-row INSERT loop (N round-trips) → multi-row INSERT (1 trip)    ║
╚══════════════════════════════════════════════════════════════════════════════╝

REQUIREMENTS:
    pip install sqlalchemy psycopg2-binary

PREREQUISITE — PostgreSQL database:
    createdb sqlalchemy_demo
    # or: psql -c "CREATE DATABASE sqlalchemy_demo;"

USAGE:
    python demo.py              # Run all demos with default DB URL
    PG_URL="postgresql://..." python demo.py  # Custom connection
    python demo.py --seed-only  # Just seed the data, skip benchmarks
    python demo.py --bench      # Only run benchmarks (assumes seeded data)
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from sqlalchemy import (
    Column,
    Integer,
    Text,
    Table,
    MetaData,
    ForeignKey,
    UniqueConstraint,
    text,
    func,
    select,
    literal_column,
    ARRAY,
)
from sqlalchemy.engine import Engine, create_engine

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

PG_URL = os.environ.get(
    "PG_URL",
    "postgresql://postgres:postgres@localhost:5432/sqlalchemy_demo",
)

# How many rows to seed for each scenario (tweak to see timing difference)
N_PICKLISTS = 10_000        # Total picklists (used by scenarios 1, 2, 3)
N_TEAMS = 50                # Teams (for M:N join scenario 3)
TEAM_ASSIGNMENTS = 3        # Avg teams per picklist (multiplies rows)
N_BULK_ROWS = 1_000         # Rows for N+1 vs batched demo (scenario 4)
N_AUDIT_ROWS = 50_000       # Rows for missing FK index demo (scenario 9)
N_INSERT_LOOP_ROWS = 500    # Rows for single-row INSERT loop demo (scenario 12)

# ──────────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────────

metadata = MetaData()

# ── Sequence for scenario 4 ──────────────────────────────────────────────────
# We'll create this via DDL so it's visible in psql.
SEQ_NAME = "form_sequence_demo"

# ── Picklists ────────────────────────────────────────────────────────────────
picklist = Table(
    "picklist_demo",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", Text, nullable=False, unique=True),
    Column("status", Text, nullable=False, default="pending"),
    Column("codes_array", ARRAY(Text), nullable=False, server_default="{}"),
    # For GIN index — see create_indexes()
)

# ── Picklist teams (M:N join table — scenario 3) ────────────────────────────
team = Table(
    "team_demo",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
)

jt_picklist_team = Table(
    "jt_picklist_team_demo",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("picklist_id", Integer, ForeignKey("picklist_demo.id"), nullable=False),
    Column("team_id",    Integer, ForeignKey("team_demo.id"),       nullable=False),
    UniqueConstraint("picklist_id", "team_id", name="uq_picklist_team"),
)

# ── Item child table (for EXISTS scenarios) ──────────────────────────────────
pickitem = Table(
    "pickitem_demo",
    metadata,
    Column("id",           Integer,  primary_key=True),
    Column("picklist_id",  Integer,  ForeignKey("picklist_demo.id"), nullable=False),
    Column("item_code",    Text,     nullable=False),
    Column("needs_review", Integer,  nullable=False, server_default="0"),
    Column("status",       Text,     nullable=False, default="ok"),
)

# ── Audit child table (for missing FK index — scenario 9) ────────────────────
picklist_audit = Table(
    "picklist_audit_demo",
    metadata,
    Column("id",           Integer,  primary_key=True),
    Column("picklist_id",  Integer,  ForeignKey("picklist_demo.id"), nullable=False),
    # NO index on picklist_id — this is the anti-pattern
    Column("action",       Text,     nullable=False),
    Column("created_at",   Text,     nullable=False),
)

# ── Log table (for single-row INSERT demo — scenario 12) ─────────────────────
demo_log = Table(
    "demo_log_inserter",
    metadata,
    Column("id",    Integer,  primary_key=True),
    Column("value", Text,     nullable=False),
)


# ──────────────────────────────────────────────────────────────────────────────
# DDL helpers
# ──────────────────────────────────────────────────────────────────────────────

def create_indexes(conn: Any) -> None:
    """Create a GIN index on the array column (supports @>, not array_position)."""
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_picklist_demo_codes_array_gin "
            "ON picklist_demo USING GIN (codes_array)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_pickitem_demo_picklist_id "
            "ON pickitem_demo (picklist_id)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_jt_picklist_team_demo_picklist_id "
            "ON jt_picklist_team_demo (picklist_id)"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_picklist_demo_status "
            "ON picklist_demo (status)"
        )
    )


def create_sequence(conn: Any) -> None:
    conn.execute(text(f"CREATE SEQUENCE IF NOT EXISTS {SEQ_NAME} START 1"))


def drop_all(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"DROP SEQUENCE IF EXISTS {SEQ_NAME}"))
        conn.execute(text("DROP TABLE IF EXISTS demo_log_inserter CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS picklist_audit_demo CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS jt_picklist_team_demo CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS pickitem_demo CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS picklist_demo CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS team_demo CASCADE"))


# ──────────────────────────────────────────────────────────────────────────────
# Data seeding
# ──────────────────────────────────────────────────────────────────────────────

CODES_POOL = [f"code_{i}" for i in range(200)]
STATUSES   = ["pending", "active", "completed", "archived", "review"]


def seed_data(engine: Engine) -> None:
    """Populate tables with reproducible random data."""
    rand = random.Random(42)

    with engine.begin() as conn:
        # ── Picklists ──────────────────────────────────────────────────────
        picklists = []
        for i in range(N_PICKLISTS):
            n_codes = rand.randint(1, 10)
            codes   = rand.sample(CODES_POOL, n_codes)
            picklists.append({
                "code":         f"PL-{i:06d}",
                "status":       rand.choice(STATUSES),
                "codes_array":  codes,
            })
        conn.execute(picklist.insert(), picklists)
        print(f"  ✅  Seeded {len(picklists):,} picklists")

        # ── Teams ──────────────────────────────────────────────────────────
        teams_data = [{"name": f"Team-{i}"} for i in range(N_TEAMS)]
        # Add a team with numeric name for implicit type casting demo (scenario 11)
        teams_data.append({"name": "42"})
        conn.execute(team.insert(), teams_data)
        print(f"  ✅  Seeded {len(teams_data)} teams")

        # ── Join table ─────────────────────────────────────────────────────
        all_picklist_ids = [
            row[0] for row in
            conn.execute(select(picklist.c.id)).fetchall()
        ]
        all_team_ids = [
            row[0] for row in
            conn.execute(select(team.c.id)).fetchall()
        ]
        jt_rows = []
        for pl_id in all_picklist_ids:
            k = rand.randint(1, TEAM_ASSIGNMENTS)
            for t_id in rand.sample(all_team_ids, min(k, len(all_team_ids))):
                jt_rows.append({"picklist_id": pl_id, "team_id": t_id})
        conn.execute(jt_picklist_team.insert(), jt_rows)
        print(f"  ✅  Seeded {len(jt_rows):,} team assignments")

        # ── Pick items (child table for EXISTS demo) ───────────────────────
        items = []
        for pl_id in all_picklist_ids:
            ni = rand.randint(0, 5)
            for _ in range(ni):
                items.append({
                    "picklist_id":  pl_id,
                    "item_code":    rand.choice(CODES_POOL),
                    "needs_review": rand.choice([0, 1]),
                    "status":       rand.choice(["ok", "error", "pending"]),
                })
        conn.execute(pickitem.insert(), items)
        print(f"  ✅  Seeded {len(items):,} pick items")

        # ── Audit records (for missing FK index demo — scenario 9) ────────
        audit_actions = ["create", "update", "delete", "archive", "review", "approve"]
        audit_rows = []
        for i in range(N_AUDIT_ROWS):
            audit_rows.append({
                "picklist_id": rand.choice(all_picklist_ids),
                "action":      rand.choice(audit_actions),
                "created_at":  f"2025-06-{1 + i % 30:02d} 12:00:00",
            })
        conn.execute(picklist_audit.insert(), audit_rows)
        print(f"  ✅  Seeded {len(audit_rows):,} audit records")


# ──────────────────────────────────────────────────────────────────────────────
# Timing utilities
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BenchResult:
    label: str
    duration_ms: float
    row_count: int = 0
    note: str = ""


def bench(
    label: str,
    fn: Callable[[], Sequence[Any]],
    *,
    iterations: int = 3,
    warm: bool = True,
) -> BenchResult:
    """Time a query function over *iterations* runs, discarding the first if warm."""
    times: list[float] = []
    rows: Sequence[Any] = []
    for i in range(iterations):
        start = time.perf_counter()
        rows = fn()
        elapsed = (time.perf_counter() - start) * 1000
        if warm and i == 0:
            continue  # warm-up, discard
        times.append(elapsed)
    avg = sum(times) / len(times) if times else 0.0
    return BenchResult(
        label=label,
        duration_ms=round(avg, 2),
        row_count=len(rows),
    )


def print_benchmark(results: list[BenchResult], title: str) -> None:
    """Pretty-print benchmark results with a visual comparison bar."""
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print(f"{'=' * 72}")
    print(f"  {'Scenario':<50} {'Rows':>6}  {'Time (ms)':>10}  {'Speedup':>8}  {'Bar':>8}")
    print(f"  {'─' * 50}  {'─' * 6}  {'─' * 10}  {'─' * 8}  {'─' * 8}")
    if not results:
        return
    baseline = max(r.duration_ms for r in results) or 1
    for r in results:
        bar_len = int((r.duration_ms / baseline) * 20)
        bar = "▓" * bar_len + "░" * (20 - bar_len)
        speedup = ""
        if r.duration_ms > 0:
            ratio = baseline / r.duration_ms
            if ratio > 1.05:
                speedup = f"{ratio:.1f}×"
        print(
            f"  {r.label:<50} {r.row_count:>6}  {r.duration_ms:>8.2f} ms"
            f"  {speedup:>8}  {bar:>8}"
        )
        if r.note:
            print(f"  {'':>2}└─ {r.note}")
    print()


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 1 — array_position vs @> operator
# ──────────────────────────────────────────────────────────────────────────────

def demo_array_position(engine: Engine) -> None:
    """Compare array_position() (non-GIN-safe) with @> (GIN-compatible)."""
    # Pick a code that is actually present in the data — query for one at random
    with engine.connect() as conn:
        row = conn.execute(
            select(func.unnest(picklist.c.codes_array)).limit(1)
        ).first()
        target_code = row[0] if row else random.Random(42).choice(CODES_POOL)

    # ── BAD: func.array_position in WHERE ──────────────────────────────────
    # PostgreSQL cannot use a GIN index here because array_position() is a
    # function call evaluated per row — it forces a sequential scan.
    def bad_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = select(picklist.c.id, picklist.c.code).where(
                func.array_position(picklist.c.codes_array, target_code).isnot(None)
            ).limit(500)
            return conn.execute(stmt).fetchall()

    # ── GOOD: @> operator ──────────────────────────────────────────────────
    # The @> (contains) operator is GIN-indexable. PostgreSQL can use the
    # GIN index on codes_array for a bitmap index scan.
    def good_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = select(picklist.c.id, picklist.c.code).where(
                picklist.c.codes_array.op("@>")([target_code])
            ).limit(500)
            return conn.execute(stmt).fetchall()

    bad_r  = bench("array_position (BAD)",  bad_query,  iterations=4)
    good_r = bench("@> operator (GOOD)",    good_query, iterations=4)
    print_benchmark([bad_r, good_r], "SCENARIO 1 — array_position vs @> (GIN-safe)")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 2 — EXISTS in SELECT vs WHERE
# ──────────────────────────────────────────────────────────────────────────────

def demo_exists_in_where(engine: Engine) -> None:
    """Compare EXISTS as a SELECT column (filtered in Python) vs WHERE clause.

    NOTE: Both queries use .exists() (not scalar_subquery) because a picklist
    can have MULTIPLE matching pickitems. Using scalar_subquery would crash:
    "more than one row returned by a subquery used as an expression".
    Both queries also apply the same LIMIT for a fair comparison.
    """

    # Shared EXISTS subquery — correlated to the outer picklist
    exists_subq = (
        select(literal_column("1"))
        .select_from(pickitem)
        .where(
            (pickitem.c.picklist_id == picklist.c.id)
            & (pickitem.c.needs_review == 1)
        )
        .correlate(picklist)
        .exists()
    )

    ROWS = 2000

    # ── BAD: EXISTS fetched as a column for every row, filtered in Python ──
    # The DB computes EXISTS for ALL rows up to the LIMIT, ships every row
    # to Python, then Python discards rows where it's False.
    # This wastes bandwidth and DB cycles on rows that are thrown away.
    def bad_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = select(
                picklist.c.id,
                picklist.c.code,
                exists_subq.label("needs_attention"),
            ).limit(ROWS)
            all_rows = conn.execute(stmt).fetchall()
            # Python-side filter after fetching
            return [r for r in all_rows if r._mapping["needs_attention"] is True]

    # ── GOOD: EXISTS moved to WHERE ────────────────────────────────────────
    # The DB applies the filter BEFORE returning results. Only matching rows
    # are transmitted. Far less data crosses the wire.
    def good_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = (
                select(picklist.c.id, picklist.c.code)
                .where(exists_subq)
                .limit(ROWS)
            )
            return conn.execute(stmt).fetchall()

    bad_r  = bench("EXISTS in SELECT — Python filter (BAD)",  bad_query,  iterations=4)
    good_r = bench("EXISTS in WHERE  — DB filter (GOOD)",     good_query, iterations=4)
    print_benchmark([bad_r, good_r], "SCENARIO 2 — EXISTS in SELECT column vs WHERE clause")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 3 — Python dedup vs SQL DISTINCT
# ──────────────────────────────────────────────────────────────────────────────

def demo_distinct_vs_python_dedup(engine: Engine) -> None:
    """Compare Python-side dedup (set) with SQL DISTINCT ON.

    The JOIN on jt_picklist_team multiplies rows — one per team assignment
    per picklist. The bad pattern fetches ALL duplicated rows into Python
    and uses a seen_ids set to deduplicate. The good pattern uses
    DISTINCT ON (picklist.id) to let PostgreSQL do the dedup before
    transmission — and returns the same logical result (one row per picklist).

    NOTE: We use DISTINCT ON (picklist.id) rather than plain DISTINCT because
    plain DISTINCT applies to ALL selected columns, which would produce
    different results (one row per team per picklist) than the Python dedup.
    DISTINCT ON (picklist.id) produces exactly one row per picklist,
    matching the bad query's behavior.
    """

    ROWS = 2000

    # ── BAD: Fetch all, dedup in Python with a set ─────────────────────────
    def bad_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = (
                select(
                    picklist.c.id,
                    picklist.c.code,
                    picklist.c.status,
                    team.c.name.label("team_name"),
                )
                .select_from(
                    picklist.join(jt_picklist_team)
                    .join(team)
                )
                .where(picklist.c.status.in_(["pending", "active"]))
                .limit(ROWS)
            )
            rows = conn.execute(stmt).fetchall()
            # Python-side dedup — all duplicated rows already shipped
            seen: set[int] = set()
            result: list[Any] = []
            for r in rows:
                if r._mapping["id"] not in seen:
                    seen.add(r._mapping["id"])
                    result.append(r)
            return result

    # ── GOOD: DISTINCT ON (picklist.id) in SQL ─────────────────────────────
    # PostgreSQL deduplicates on picklist.id before sending results.
    # Less data over the wire, no Python overhead, same logical result.
    def good_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = (
                select(
                    picklist.c.id,
                    picklist.c.code,
                    picklist.c.status,
                    team.c.name.label("team_name"),
                )
                .select_from(
                    picklist.join(jt_picklist_team)
                    .join(team)
                )
                .where(picklist.c.status.in_(["pending", "active"]))
                .distinct(picklist.c.id)  # DISTINCT ON (picklist.id)
                .limit(ROWS)
            )
            return conn.execute(stmt).fetchall()

    bad_r  = bench("Python set() dedup (BAD)",  bad_query,  iterations=4)
    good_r = bench("SQL DISTINCT ON (GOOD)",     good_query, iterations=4)
    print_benchmark([bad_r, good_r], "SCENARIO 3 — Python dedup vs SQL DISTINCT ON (picklist.id)")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 4 — N+1 nextval vs batched sequence generation
# ──────────────────────────────────────────────────────────────────────────────

def demo_batched_sequence(engine: Engine) -> None:
    """Compare N+1 nextval() calls with a single batched generate_series call.

    The bad pattern calls SELECT nextval(...) once per row — N round-trips
    to the database just for UID generation. The good pattern uses
    SELECT nextval(...) FROM generate_series(1, N) to get all IDs in one
    round-trip.
    """

    n = N_BULK_ROWS

    # ── BAD: Per-row nextval in a loop ─────────────────────────────────────
    def bad_query() -> list[int]:
        with engine.connect() as conn:
            ids: list[int] = []
            for _ in range(n):
                row = conn.execute(
                    text(f"SELECT nextval('{SEQ_NAME}')")
                ).scalar_one()
                ids.append(row)
            return ids

    # ── GOOD: Batched nextval via generate_series ──────────────────────────
    def good_query() -> list[int]:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT nextval('{SEQ_NAME}') "
                    f"FROM generate_series(1, {n})"
                )
            ).fetchall()
            return [r[0] for r in rows]

    bad_r  = bench("N+1 nextval() in loop (BAD)",  bad_query,  iterations=4)
    good_r = bench("generate_series batch (GOOD)",  good_query, iterations=4)
    print_benchmark([bad_r, good_r], f"SCENARIO 4 — N+1 nextval (n={n}) vs batched generate_series")



# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 5 — N+1 ORM Pattern
# ──────────────────────────────────────────────────────────────────────────────

def demo_n_plus_one_orm(engine: Engine) -> None:
    """Compare N+1 ORM pattern (loop query children per parent) with a single JOIN.

    The classic N+1: query N parent rows, then for EACH parent, fire another query
    to fetch children. That is 1 + N queries instead of 1. The fix is a JOIN that
    fetches everything in a single round-trip.
    """

    # ── BAD: N+1 queries — fetch parents, then loop to fetch children ──────
    def bad_query() -> list[Any]:
        with engine.connect() as conn:
            parents = conn.execute(
                select(picklist.c.id, picklist.c.code)
                .where(picklist.c.status == "pending")
            ).fetchall()
            result: list[Any] = []
            for p in parents:
                children = conn.execute(
                    select(pickitem.c.id, pickitem.c.item_code)
                    .where(pickitem.c.picklist_id == p._mapping["id"])
                ).fetchall()
                for c in children:
                    result.append((
                        p._mapping["id"],
                        p._mapping["code"],
                        c._mapping["item_code"],
                    ))
            return result

    # ── GOOD: single JOIN — one query, one round-trip ──────────────────────
    def good_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = (
                select(
                    picklist.c.id,
                    picklist.c.code,
                    pickitem.c.item_code,
                )
                .select_from(
                    picklist.join(
                        pickitem,
                        picklist.c.id == pickitem.c.picklist_id,
                    )
                )
                .where(picklist.c.status == "pending")
            )
            return conn.execute(stmt).fetchall()

    bad_r  = bench("N+1 — loop per parent (BAD)",   bad_query,  iterations=4)
    good_r = bench("JOIN — single query (GOOD)",    good_query, iterations=4)
    print_benchmark([bad_r, good_r], "SCENARIO 5 — N+1 ORM Pattern vs JOIN")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 6 — Function in WHERE (index suppression)
# ──────────────────────────────────────────────────────────────────────────────

def demo_function_in_where(engine: Engine) -> None:
    """Compare WHERE func(col) = value (cannot use index) with col = value.

    Wrapping a column in a function (LOWER, DATE, CAST, etc.) inside WHERE
    prevents PostgreSQL from using a plain index on that column. The DB is
    forced to scan every row and apply the function. The fix: either match
    the stored format directly, or create a functional index.
    """

    # We know PL-000001 exists because we seeded it
    target_lower = "pl-000001"
    target_upper = "PL-000001"

    # ── BAD: LOWER(col) wraps the column — index on 'code' is unusable ────
    def bad_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = select(picklist.c.id, picklist.c.code).where(
                func.lower(picklist.c.code) == target_lower
            )
            return conn.execute(stmt).fetchall()

    # ── GOOD: col = value directly — uses the unique index on 'code' ──────
    def good_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = select(picklist.c.id, picklist.c.code).where(
                picklist.c.code == target_upper
            )
            return conn.execute(stmt).fetchall()

    bad_r  = bench("LOWER(code)=value — seq scan (BAD)",  bad_query,  iterations=4)
    good_r = bench("code=value — index scan (GOOD)",      good_query, iterations=4)
    print_benchmark([bad_r, good_r], "SCENARIO 6 — Function in WHERE (index suppression)")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 7 — SELECT * vs Specific Columns
# ──────────────────────────────────────────────────────────────────────────────

def demo_select_star(engine: Engine) -> None:
    """Compare SELECT * (over-fetches all columns) with SELECT specific columns.

    SELECT * fetches ALL columns from the table — including large ones like
    codes_array (ARRAY of text). This wastes I/O, network bandwidth, and
    memory in the application. The fix: list only the columns you actually need.
    """

    N_ROWS = 5000

    # ── BAD: SELECT * — fetches codes_array (large ARRAY) and all other cols ──
    def bad_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = select(picklist).limit(N_ROWS)
            return conn.execute(stmt).fetchall()

    # ── GOOD: SELECT only id, code, status — skip the heavy ARRAY column ──
    def good_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = select(
                picklist.c.id,
                picklist.c.code,
                picklist.c.status,
            ).limit(N_ROWS)
            return conn.execute(stmt).fetchall()

    bad_r  = bench("SELECT * — all columns (BAD)",    bad_query,  iterations=4)
    good_r = bench("SELECT id,code,status (GOOD)",    good_query, iterations=4)
    print_benchmark([bad_r, good_r], "SCENARIO 7 — SELECT * vs Specific Columns")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 8 — NOT IN (subquery) vs NOT EXISTS
# ──────────────────────────────────────────────────────────────────────────────

def demo_not_in_vs_not_exists(engine: Engine) -> None:
    """Compare NOT IN (subquery) with NOT EXISTS (correlated subquery).

    NOT IN (subquery) has two problems:
      1. If the subquery returns ANY NULL values, NOT IN evaluates to
         UNKNOWN for EVERY row — returning ZERO rows (silently wrong).
      2. PostgreSQL often cannot optimize NOT IN as well as NOT EXISTS,
         leading to slower sequential scans.

    NOT EXISTS (correlated subquery) is both correct (NULL-safe) and
    typically faster (semi-join / anti-join optimization).
    """

    ROWS = 2000

    # ── BAD: NOT IN (subquery) ─────────────────────────────────────────────
    def bad_query() -> list[Any]:
        with engine.connect() as conn:
            subq = select(pickitem.c.picklist_id).where(
                pickitem.c.needs_review == 1
            )
            stmt = select(picklist.c.id, picklist.c.code).where(
                picklist.c.id.not_in(subq)
            ).limit(ROWS)
            return conn.execute(stmt).fetchall()

    # ── GOOD: NOT EXISTS (correlated) ──────────────────────────────────────
    def good_query() -> list[Any]:
        with engine.connect() as conn:
            exists_subq = (
                select(literal_column("1"))
                .select_from(pickitem)
                .where(
                    (pickitem.c.picklist_id == picklist.c.id)
                    & (pickitem.c.needs_review == 1)
                )
                .correlate(picklist)
                .exists()
            )
            stmt = select(picklist.c.id, picklist.c.code).where(
                ~exists_subq  # NOT EXISTS
            ).limit(ROWS)
            return conn.execute(stmt).fetchall()

    bad_r  = bench("NOT IN subquery (BAD)",           bad_query,  iterations=4)
    good_r = bench("NOT EXISTS correlated (GOOD)",    good_query, iterations=4)
    print_benchmark([bad_r, good_r], "SCENARIO 8 — NOT IN vs NOT EXISTS")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 9 — Missing Index on Foreign Key
# ──────────────────────────────────────────────────────────────────────────────

def demo_missing_fk_index(engine: Engine) -> None:
    """Compare JOIN without FK index (seq scan) with FK index (index scan).

    The #1 most cited PostgreSQL performance anti-pattern: a foreign key
    column without an index forces a sequential scan on the child table
    when joining. Adding an index on the FK column enables an index scan,
    typically 10-100× faster for joined queries.
    """

    ROWS = 2000

    # Ensure the index does NOT exist for the BAD query
    with engine.begin() as conn:
        conn.execute(
            text("DROP INDEX IF EXISTS ix_picklist_audit_demo_picklist_id")
        )

    # ── BAD: JOIN on unindexed FK column — forces seq scan ───────────────
    def bad_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = (
                select(
                    picklist.c.id,
                    picklist.c.code,
                    picklist_audit.c.action,
                )
                .select_from(
                    picklist.join(
                        picklist_audit,
                        picklist.c.id == picklist_audit.c.picklist_id,
                    )
                )
                .where(picklist.c.status.in_(["pending", "active"]))
                .limit(ROWS)
            )
            return conn.execute(stmt).fetchall()

    # Create the FK index for the GOOD query
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_picklist_audit_demo_picklist_id "
                "ON picklist_audit_demo (picklist_id)"
            )
        )

    # ── GOOD: Same JOIN, now with FK index — uses index scan ────────────
    def good_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = (
                select(
                    picklist.c.id,
                    picklist.c.code,
                    picklist_audit.c.action,
                )
                .select_from(
                    picklist.join(
                        picklist_audit,
                        picklist.c.id == picklist_audit.c.picklist_id,
                    )
                )
                .where(picklist.c.status.in_(["pending", "active"]))
                .limit(ROWS)
            )
            return conn.execute(stmt).fetchall()

    bad_r  = bench("JOIN without FK index — seq scan (BAD)",  bad_query,  iterations=4)
    good_r = bench("JOIN with FK index — index scan (GOOD)",  good_query, iterations=4)
    print_benchmark([bad_r, good_r], "SCENARIO 9 — Missing Index on Foreign Key")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 10 — Deep OFFSET Pagination vs Keyset/Cursor Pagination
# ──────────────────────────────────────────────────────────────────────────────

def demo_deep_offset(engine: Engine) -> None:
    """Compare deep OFFSET pagination with keyset/cursor pagination.

    LIMIT/OFFSET becomes catastrophically slow at large offsets because
    PostgreSQL must scan AND discard N skipped rows. Keyset pagination
    (WHERE id > last_seen) seeks directly to the position, reading only
    the rows returned. The difference grows with the offset depth.
    """

    LIMIT_ROWS = 20
    # Use ~90% of picklists to demonstrate a deep offset
    offset_val = max(1, N_PICKLISTS * 9 // 10)

    # Pre-compute the keyset cursor outside the timed functions (in a real
    # app the client tracks the last seen ID from the previous page).
    with engine.connect() as conn:
        cursor_row = conn.execute(
            select(picklist.c.id)
            .order_by(picklist.c.id)
            .limit(1)
            .offset(offset_val)
        ).first()
    cursor_id = cursor_row[0] if cursor_row else 0

    # ── BAD: OFFSET-based pagination — reads + discards N rows ─────────
    def bad_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = (
                select(picklist.c.id, picklist.c.code)
                .order_by(picklist.c.id)
                .limit(LIMIT_ROWS)
                .offset(offset_val)
            )
            return conn.execute(stmt).fetchall()

    # ── GOOD: Keyset pagination — seeks directly ────────────────────────
    def good_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = (
                select(picklist.c.id, picklist.c.code)
                .where(picklist.c.id > cursor_id)
                .order_by(picklist.c.id)
                .limit(LIMIT_ROWS)
            )
            return conn.execute(stmt).fetchall()

    bad_r  = bench(f"OFFSET {offset_val} — deep pagination (BAD)",  bad_query,  iterations=4)
    good_r = bench("WHERE id > last_seen — keyset (GOOD)",          good_query, iterations=4)
    print_benchmark([bad_r, good_r], f"SCENARIO 10 — Deep OFFSET (offset={offset_val}) vs Keyset Pagination")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 11 — Implicit Type Casting
# ──────────────────────────────────────────────────────────────────────────────

def demo_implicit_type_cast(engine: Engine) -> None:
    """Compare implicit type casting (seq scan) with matching types (index scan).

    When a column type differs from the literal type in WHERE, PostgreSQL
    casts the COLUMN side (not the literal), disabling index usage.
    E.g., WHERE text_col = 123 forces casting every row's text to int.
    The fix: match the literal type to the column type.
    """

    # ── BAD: text column compared to integer — forces column type cast ──
    def bad_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = select(team.c.id, team.c.name).where(
                team.c.name == 42  # int — PostgreSQL must cast name::int
            )
            return conn.execute(stmt).fetchall()

    # ── GOOD: text column compared to text — uses unique index ──────────
    def good_query() -> list[Any]:
        with engine.connect() as conn:
            stmt = select(team.c.id, team.c.name).where(
                team.c.name == "42"  # text — matches column type, uses index
            )
            return conn.execute(stmt).fetchall()

    bad_r  = bench("WHERE text_col = 42 — type coercion (BAD)",    bad_query,  iterations=4)
    good_r = bench("WHERE text_col = '42' — correct type (GOOD)",  good_query, iterations=4)
    print_benchmark([bad_r, good_r], "SCENARIO 11 — Implicit Type Casting (index suppression)")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 12 — Single-row INSERT Loop vs Multi-row INSERT
# ──────────────────────────────────────────────────────────────────────────────

def demo_single_row_insert(engine: Engine) -> None:
    """Compare single-row INSERT loop (N round-trips) with multi-row INSERT.

    Inserting rows one at a time in a loop creates N round-trips to the
    database. A single multi-row INSERT (or bulk insert with a list of
    dicts) sends all rows in ONE round-trip. The difference is dominated
    by network round-trip latency.
    """

    n = N_INSERT_LOOP_ROWS

    # ── BAD: Single-row INSERT in a loop — N round-trips ────────────────
    def bad_query() -> list[Any]:
        with engine.begin() as conn:
            rows: list[int] = []
            for i in range(n):
                result = conn.execute(
                    demo_log.insert().values(value=f"loop-{i}")
                )
                rows.append(result.inserted_primary_key[0])
            return rows

    # ── GOOD: Multi-row INSERT — 1 round-trip ───────────────────────────
    def good_query() -> list[Any]:
        with engine.begin() as conn:
            conn.execute(
                demo_log.insert(),
                [{"value": f"bulk-{i}"} for i in range(n)],
            )
            # Return a sentinel list so BenchResult.row_count is accurate
            return [1] * n

    bad_r  = bench(f"Single-row INSERT loop × {n} (BAD)",   bad_query,  iterations=4)
    good_r = bench(f"Multi-row INSERT × {n} (GOOD)",        good_query, iterations=4)
    print_benchmark([bad_r, good_r], f"SCENARIO 12 — Single-row INSERT Loop (n={n}) vs Multi-row INSERT")


# ──────────────────────────────────────────────────────────────────────────────
# Summary output
# ──────────────────────────────────────────────────────────────────────────────

def print_summary() -> None:
    print(f"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  SUMMARY — Why these patterns matter                                         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  All eight bugs share the same anti-pattern:                                  ║
║                                                                              ║
║     Work that can be done ONCE in the database is moved to the               ║
║     application layer and repeated PER-ROW — wasting bandwidth,              ║
║     CPU cycles, and database connections.                                    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 1. array_position (BAD) vs @> (GOOD)                                 │    ║
║  │    array_position() is a function call — cannot use GIN index.        │    ║
║  │    @> is an operator the GIN index understands natively.              │    ║
║  │    Fix: Use codes_array @> ARRAY['target'] instead.                   │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 2. EXISTS in SELECT column (BAD) vs WHERE (GOOD)                     │    ║
║  │    Computing EXISTS for EVERY row and shipping ALL rows to Python     │    ║
║  │    when most are discarded is wasteful.                               │    ║
║  │    Fix: Move the EXISTS to the WHERE clause — DB filters first.      │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 3. Python set() dedup (BAD) vs SQL DISTINCT (GOOD)                   │    ║
║  │    Fetching duplicated rows from a JOIN and deduplicating in Python   │    ║
║  │    ships duplicate data over the network for no reason.               │    ║
║  │    Fix: .distinct() on picklist.c.id — DB deduplicates before send.  │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 4. N+1 nextval() loop (BAD) vs generate_series batch (GOOD)          │    ║
║  │    N round-trips for N sequence values. Each trip adds ~1-5ms        │    ║
║  │    overhead. generate_series does it in 1 round-trip.                 │    ║
║  │    Fix: SELECT nextval(seq) FROM generate_series(1, N).              │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 5. N+1 ORM loop (BAD) vs JOIN (GOOD)                                │    ║
║  │    The classic N+1: 1 query for parents + N queries for children     │    ║
║  │    = N+1 round-trips. A JOIN does it in 1 query.                     │    ║
║  │    Fix: Use a JOIN (or eager-loading in the ORM).                     │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 6. WHERE func(col)=value (BAD) vs col=value (GOOD)                   │    ║
║  │    Wrapping a column in LOWER/DATE/etc. prevents index usage.        │    ║
║  │    The DB must scan every row and apply the function.                 │    ║
║  │    Fix: Match the stored value directly, or use a functional index.  │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 7. SELECT * (BAD) vs specific columns (GOOD)                         │    ║
║  │    SELECT * fetches ALL columns — including large text/array ones.   │    ║
║  │    This wastes I/O, bandwidth, and application memory.                │    ║
║  │    Fix: Always list the exact columns you need.                      │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 8. NOT IN subquery (BAD) vs NOT EXISTS (GOOD)                        │    ║
║  │    NOT IN returns ZERO rows if subquery contains NULL (silently      │    ║
║  │    wrong!). It is also typically slower than NOT EXISTS.              │    ║
║  │    Fix: Always use NOT EXISTS for subquery exclusion checks.         │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 9. Missing FK index (BAD) vs FK index (GOOD)                         │    ║
║  │    Joining on a foreign key column without an index forces a         │    ║
║  │    sequential scan on the child table. An index on the FK column     │    ║
║  │    enables index scans — typically 10-100× faster.                    │    ║
║  │    Fix: CREATE INDEX ON child_table (fk_column).                     │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 10. Deep OFFSET pagination (BAD) vs Keyset pagination (GOOD)         │    ║
║  │    OFFSET 100000 + LIMIT 20 forces Postgres to read and discard      │    ║
║  │    100,020 rows. Keyset (WHERE id > last_seen) seeks directly.       │    ║
║  │    Fix: Use keyset/cursor pagination for deep pages.                 │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 11. Implicit type casting (BAD) vs matching types (GOOD)             │    ║
║  │    WHERE text_col = 123 forces PostgreSQL to cast text_col::int      │    ║
║  │    for every row, disabling the index. Match the literal type        │    ║
║  │    to the column type (WHERE text_col = '123') to use the index.    │    ║
║  │    Fix: Always use the correct type in WHERE clauses.                │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 12. Single-row INSERT loop (BAD) vs Multi-row INSERT (GOOD)          │    ║
║  │    Inserting N rows one at a time = N round-trips. A multi-row       │    ║
║  │    INSERT with a list of dicts sends all rows in one round-trip.     │    ║
║  │    Fix: Use conn.execute(table.insert(), list_of_dicts).             │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  Mental model (Scenario 4 — sequence generation):                            ║
║                                                                              ║
║    Approach          │  Queries  │  Round-trips                              ║
║    ──────────────────┼───────────┼───────────                                ║
║    N+1 (old)         │    N+1    │    N+1   ← Per-row overhead adds up      ║
║    Batched (fix)     │     1     │     1    ← One shot, done                ║
║                                                                              ║
║  The database does the SAME amount of work either way — the difference       ║
║  is how many times your application has to wait for a response.              ║
║  The same principle applies to ALL twelve scenarios:                         ║
║  push work to the DB, do it once, reduce round-trips.                        ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
""")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="SQLAlchemy + PostgreSQL anti-patterns demo"
    )
    parser.add_argument("--seed-only", action="store_true",
                        help="Only seed data, skip benchmarks")
    parser.add_argument("--bench", action="store_true",
                        help="Only run benchmarks (assumes seeded data)")
    parser.add_argument("--drop", action="store_true",
                        help="Drop all demo tables and exit")
    args = parser.parse_args()

    engine = create_engine(PG_URL, echo=False, pool_pre_ping=True)

    # ── Validate we can reach PostgreSQL ───────────────────────────────────
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar()
        print(f"🔗  Connected to PostgreSQL\n    {version}\n")
    except Exception as e:
        print(f"❌  Cannot connect to PostgreSQL at:\n    {PG_URL}")
        print(f"    Error: {e}")
        print("\n    Make sure PostgreSQL is running and the database exists:")
        print(f"    $ createdb sqlalchemy_demo")
        raise SystemExit(1)

    # ── Drop only mode ─────────────────────────────────────────────────────
    if args.drop:
        drop_all(engine)
        print("🧹  Dropped all demo tables and sequences.")
        return

    # ── Seed ───────────────────────────────────────────────────────────────
    if not args.bench:
        drop_all(engine)
        metadata.create_all(engine)
        with engine.begin() as conn:
            create_sequence(conn)
            create_indexes(conn)
        print("📦  Seeding data…")
        seed_data(engine)
        print("📦  Seeding complete.\n")

    # ── Run benchmarks ─────────────────────────────────────────────────────
    if not args.seed_only:
        # Ensure indexes exist even in --bench mode
        if args.bench:
            with engine.connect() as conn:
                try:
                    conn.execute(text("SELECT 1 FROM picklist_demo LIMIT 1"))
                except Exception:
                    print("❌  Tables don't exist. Run without --bench first to seed data.")
                    print("   $ python demo.py   # seeds data + runs benchmarks")
                    raise SystemExit(1)
            with engine.begin() as conn:
                create_indexes(conn)
        demo_array_position(engine)
        demo_exists_in_where(engine)
        demo_distinct_vs_python_dedup(engine)
        demo_batched_sequence(engine)
        demo_n_plus_one_orm(engine)
        demo_function_in_where(engine)
        demo_select_star(engine)
        demo_not_in_vs_not_exists(engine)
        demo_missing_fk_index(engine)
        demo_deep_offset(engine)
        demo_implicit_type_cast(engine)
        demo_single_row_insert(engine)
        print_summary()

    engine.dispose()


if __name__ == "__main__":
    main()
