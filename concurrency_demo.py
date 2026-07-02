"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  SQLAlchemy + PostgreSQL Concurrency Patterns                               ║
║  ───────────────────────────────────────────────────────                    ║
║  Demonstrates 5 race condition scenarios and their fixes:                  ║
║                                                                             ║
║  1. Lost Update (no locking)  ->  SELECT FOR UPDATE (pessimistic lock)      ║
║  2. Read-Modify-Write  ->  Atomic SQL UPDATE                                ║
║  3. No conflict detection  ->  Optimistic Concurrency (version column)      ║
║  4. INSERT race (duplicate)  ->  UNIQUE constraint + Upsert                 ║
║  5. Single-process lock  ->  PostgreSQL Advisory Lock                       ║
╚══════════════════════════════════════════════════════════════════════════════╝

REQUIREMENTS:
    pip install sqlalchemy psycopg2-binary

PREREQUISITE - PostgreSQL database:
    createdb sqlalchemy_demo
    # or: psql -c "CREATE DATABASE sqlalchemy_demo;"

USAGE:
    python concurrency_demo.py              # Run all demos with default DB URL
    PG_URL="postgresql://..." python concurrency_demo.py  # Custom connection
    python concurrency_demo.py --seed-only  # Just seed the data, skip benchmarks
    python concurrency_demo.py --bench      # Only run benchmarks (assumes seeded data)
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import (
    Column,
    Integer,
    Text,
    Table,
    MetaData,
    UniqueConstraint,
    text,
    func,
    select,
)
from sqlalchemy.engine import Engine, create_engine

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

PG_URL = os.environ.get(
    "PG_URL",
    "postgresql://postgres:postgres@localhost:5432/sqlalchemy_demo",
)

# Concurrency settings
N_THREADS = 10          # Number of concurrent threads
N_INCREMENTS = 100      # Operations per thread (scenarios 1, 2, 3, 5)
N_INSERT_THREADS = 8    # Threads for scenario 4 (INSERT race)

TARGET_ID = 1               # Counter row ID used by all increment scenarios
ADVISORY_LOCK_KEY = 12345   # Arbitrary key for PostgreSQL advisory lock

# ──────────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────────

metadata = MetaData()

# ── Counter table (scenarios 1, 2, 3, 5) ─────────────────────────────────────
counter = Table(
    "concurrency_counter",
    metadata,
    Column("id",      Integer, primary_key=True),
    Column("label",   Text,    nullable=False),
    Column("count",   Integer, nullable=False, server_default="0"),
    Column("version", Integer, nullable=False, server_default="1"),
)

# ── Users table (scenario 4 - INSERT race + upsert) ──────────────────────────
users = Table(
    "concurrency_users",
    metadata,
    Column("id",    Integer, primary_key=True),
    Column("email", Text,    nullable=False),
    Column("name",  Text,    nullable=False),
    UniqueConstraint("email", name="uq_concurrency_users_email"),
)

# ──────────────────────────────────────────────────────────────────────────────
# DDL helpers
# ──────────────────────────────────────────────────────────────────────────────

def drop_all(engine: Engine) -> None:
    """Drop all demo tables."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS concurrency_counter CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS concurrency_users CASCADE"))


def seed_data(engine: Engine) -> None:
    """Populate tables with initial data for all scenarios."""
    with engine.begin() as conn:
        # Counter row for scenarios 1, 2, 3, 5
        conn.execute(counter.insert().values(label="view_count", count=0, version=1))
        print(f"  Seeded counter row (id={TARGET_ID}): count=0, version=1")

        # Existing users for scenario 4
        conn.execute(users.insert().values(email="alice@example.com", name="Alice"))
        conn.execute(users.insert().values(email="bob@example.com", name="Bob"))
        print(f"  Seeded 2 users for upsert scenario")


# ──────────────────────────────────────────────────────────────────────────────
# Benchmark utilities (concurrency-adapted)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ConcurrencyBench:
    """Result of a single concurrent benchmark run."""
    label: str
    duration_ms: float
    final_count: int = 0
    expected_count: int = 0
    note: str = ""

    @property
    def correct(self) -> bool:
        return self.final_count == self.expected_count


def _run_concurrent(
    engine: Engine,
    worker_fn: Callable[[Engine], None],
    n_threads: int,
    n_ops: int,
) -> None:
    """Run worker_fn concurrently using n_threads, each executed n_ops times.

    Each invocation of worker_fn gets its own database connection and transaction.
    """
    def _runner(_tid: int) -> None:
        for _ in range(n_ops):
            worker_fn(engine)

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(_runner, i) for i in range(n_threads)]
        for f in as_completed(futures):
            f.result()  # Re-raise any exception from the worker


def _reset_counter(engine: Engine) -> None:
    """Reset the counter row to initial state."""
    with engine.begin() as conn:
        conn.execute(
            counter.update()
            .where(counter.c.id == TARGET_ID)
            .values(count=0, version=1)
        )


def _read_counter(engine: Engine) -> int:
    """Read the current counter value."""
    with engine.connect() as conn:
        return conn.execute(
            select(counter.c.count).where(counter.c.id == TARGET_ID)
        ).scalar() or 0


def print_benchmark(results: list[ConcurrencyBench], title: str) -> None:
    """Pretty-print concurrency benchmark results."""
    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print(f"{'=' * 72}")
    print(f"  {'Approach':<45} {'Correct':>7}  {'Final':>6}  {'Expected':>8}  {'Time (ms)':>10}")
    print(f"  {'─' * 45}  {'─' * 7}  {'─' * 6}  {'─' * 8}  {'─' * 10}")
    for r in results:
        correct_str = "YES" if r.correct else "NO"
        print(
            f"  {r.label:<45} {correct_str:>7}  {r.final_count:>6}  "
            f"{r.expected_count:>8}  {r.duration_ms:>8.2f} ms"
        )
        if r.note:
            print(f"  {'':>2}└─ {r.note}")
    print()


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 1 — Lost Update (No Locking) vs SELECT FOR UPDATE
# ──────────────────────────────────────────────────────────────────────────────

def demo_lost_update(engine: Engine) -> None:
    """Compare no-locking (lost updates) with SELECT FOR UPDATE.

    The BAD pattern reads the counter value, increments in Python, and writes it
    back - without any locking. Between the read and write, another transaction
    modifies the same row, causing the first transaction's update to overwrite
    the second's - silently losing an increment.

    The GOOD pattern uses SELECT ... FOR UPDATE to acquire a row-level lock
    before the read. The lock is held until the transaction commits, making
    other writers wait - no lost updates.
    """

    expected = N_THREADS * N_INCREMENTS  # Total increments expected

    # ── BAD: No locking - read-modify-write in Python ────────────────────────
    def _bad_worker(engine: Engine) -> None:
        with engine.begin() as conn:
            row = conn.execute(
                select(counter.c.count).where(counter.c.id == TARGET_ID)
            ).scalar()
            new_count = row + 1
            conn.execute(
                counter.update().where(counter.c.id == TARGET_ID).values(count=new_count)
            )

    _reset_counter(engine)
    start = time.perf_counter()
    _run_concurrent(engine, _bad_worker, N_THREADS, N_INCREMENTS)
    bad_duration = (time.perf_counter() - start) * 1000
    bad_final = _read_counter(engine)

    # ── GOOD: SELECT FOR UPDATE - pessimistic row lock ───────────────────────
    def _good_worker(engine: Engine) -> None:
        with engine.begin() as conn:
            row = conn.execute(
                select(counter.c.count)
                .where(counter.c.id == TARGET_ID)
                .with_for_update()
            ).scalar()
            new_count = row + 1
            conn.execute(
                counter.update().where(counter.c.id == TARGET_ID).values(count=new_count)
            )

    _reset_counter(engine)
    start = time.perf_counter()
    _run_concurrent(engine, _good_worker, N_THREADS, N_INCREMENTS)
    good_duration = (time.perf_counter() - start) * 1000
    good_final = _read_counter(engine)

    bad_r = ConcurrencyBench(
        label="No locking (BAD)",
        duration_ms=bad_duration,
        final_count=bad_final,
        expected_count=expected,
        note=f"Lost {expected - bad_final} increments — race window between read & write",
    )
    good_r = ConcurrencyBench(
        label="SELECT FOR UPDATE (GOOD)",
        duration_ms=good_duration,
        final_count=good_final,
        expected_count=expected,
        note="No lost updates — FOR UPDATE serializes concurrent readers",
    )
    print_benchmark([bad_r, good_r], "SCENARIO 1 — Lost Update: No Locking vs SELECT FOR UPDATE")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 2 — Read-Modify-Write vs Atomic SQL UPDATE
# ──────────────────────────────────────────────────────────────────────────────

def demo_atomic_update(engine: Engine) -> None:
    """Compare read-modify-write (with FOR UPDATE) with atomic SQL UPDATE.

    Both approaches prevent lost updates. The difference is efficiency:
      - BAD: Two round-trips (SELECT ... FOR UPDATE + UPDATE) per operation.
      - GOOD: One round-trip (UPDATE count = count + 1). The DB does the
              increment atomically - no separate read needed.

    The atomic approach is faster because it eliminates one round-trip and
    avoids transferring the value to the application and back.
    """

    expected = N_THREADS * N_INCREMENTS

    # ── BAD: FOR UPDATE + Python increment (two statements) ──────────────────
    def _bad_worker(engine: Engine) -> None:
        with engine.begin() as conn:
            row = conn.execute(
                select(counter.c.count)
                .where(counter.c.id == TARGET_ID)
                .with_for_update()
            ).scalar()
            new_count = row + 1
            conn.execute(
                counter.update().where(counter.c.id == TARGET_ID).values(count=new_count)
            )

    _reset_counter(engine)
    start = time.perf_counter()
    _run_concurrent(engine, _bad_worker, N_THREADS, N_INCREMENTS)
    bad_duration = (time.perf_counter() - start) * 1000
    bad_final = _read_counter(engine)

    # ── GOOD: Atomic SQL UPDATE (one statement) ──────────────────────────────
    def _good_worker(engine: Engine) -> None:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE concurrency_counter SET count = count + 1 WHERE id = :id"),
                {"id": TARGET_ID},
            )

    _reset_counter(engine)
    start = time.perf_counter()
    _run_concurrent(engine, _good_worker, N_THREADS, N_INCREMENTS)
    good_duration = (time.perf_counter() - start) * 1000
    good_final = _read_counter(engine)

    bad_r = ConcurrencyBench(
        label="FOR UPDATE + Python inc (BAD)",
        duration_ms=bad_duration,
        final_count=bad_final,
        expected_count=expected,
        note="Correct but 2 round-trips per operation",
    )
    good_r = ConcurrencyBench(
        label="Atomic UPDATE count=count+1 (GOOD)",
        duration_ms=good_duration,
        final_count=good_final,
        expected_count=expected,
        note="1 round-trip — DB does increment internally",
    )
    print_benchmark([bad_r, good_r], "SCENARIO 2 — Read-Modify-Write vs Atomic SQL UPDATE")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 3 — Optimistic Concurrency Control (Version Column)
# ──────────────────────────────────────────────────────────────────────────────

def demo_optimistic_cc(engine: Engine) -> None:
    """Compare no conflict detection with version-column optimistic locking.

    The BAD pattern has no version check — two concurrent readers see the same
    state, both write back, and the second silently overwrites the first.

    The GOOD pattern uses a version column. Each UPDATE includes
    WHERE version = :old_version. If the row changed between read and write,
    the UPDATE affects zero rows (rowcount == 0), signaling a conflict.
    The application re-reads, re-computes, and retries.

    Optimistic concurrency is ideal for read-heavy, low-contention workloads —
    no locks are held during the application's processing time.
    """

    expected = N_THREADS * N_INCREMENTS

    # ── BAD: No version check — write overwrites silently ────────────────────
    def _bad_worker(engine: Engine) -> None:
        with engine.begin() as conn:
            row = conn.execute(
                select(counter.c.count).where(counter.c.id == TARGET_ID)
            ).scalar()
            new_count = row + 1
            conn.execute(
                counter.update().where(counter.c.id == TARGET_ID).values(count=new_count)
            )

    _reset_counter(engine)
    start = time.perf_counter()
    _run_concurrent(engine, _bad_worker, N_THREADS, N_INCREMENTS)
    bad_duration = (time.perf_counter() - start) * 1000
    bad_final = _read_counter(engine)

    # ── GOOD: Version column — detect and retry on conflict ──────────────────
    def _good_worker(engine: Engine) -> None:
        with engine.begin() as conn:
            while True:
                row = conn.execute(
                    select(counter.c.count, counter.c.version)
                    .where(counter.c.id == TARGET_ID)
                ).first()
                cur_count = row._mapping["count"]
                cur_version = row._mapping["version"]

                result = conn.execute(
                    counter.update()
                    .where(counter.c.id == TARGET_ID)
                    .where(counter.c.version == cur_version)
                    .values(count=cur_count + 1, version=cur_version + 1)
                )
                if result.rowcount > 0:
                    break  # Update succeeded — exit retry loop

    _reset_counter(engine)
    start = time.perf_counter()
    _run_concurrent(engine, _good_worker, N_THREADS, N_INCREMENTS)
    good_duration = (time.perf_counter() - start) * 1000
    good_final = _read_counter(engine)

    bad_r = ConcurrencyBench(
        label="No version check (BAD)",
        duration_ms=bad_duration,
        final_count=bad_final,
        expected_count=expected,
        note=f"Lost {expected - bad_final} increments — silent overwrite",
    )
    good_r = ConcurrencyBench(
        label="Version column + retry (GOOD)",
        duration_ms=good_duration,
        final_count=good_final,
        expected_count=expected,
        note="Correct count; retries add overhead under high contention",
    )
    print_benchmark([bad_r, good_r], "SCENARIO 3 — Optimistic Concurrency Control (Version Column)")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 4 — INSERT Race: Unique Constraint + Upsert
# ──────────────────────────────────────────────────────────────────────────────

def demo_insert_race(engine: Engine) -> None:
    """Compare app-level existence check with UNIQUE constraint + upsert.

    The BAD pattern does an application-level check (SELECT first, then INSERT
    if not found). Between the SELECT and INSERT, another request can also pass
    the check, so both INSERT the same record. Without a UNIQUE constraint,
    duplicates are created silently.

    The GOOD pattern uses a database UNIQUE constraint with PostgreSQL's
    ON CONFLICT DO NOTHING. The database atomically checks for duplicates —
    no race window exists. The INSERT either succeeds or is silently ignored.
    """

    TEST_EMAIL = "race-test@example.com"

    # ── BAD: Drop constraint, use app-level check ────────────────────────────
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE concurrency_users "
            "DROP CONSTRAINT IF EXISTS uq_concurrency_users_email"
        ))
        conn.execute(users.delete().where(users.c.email == TEST_EMAIL))

    def _bad_worker(engine: Engine) -> None:
        with engine.begin() as conn:
            row = conn.execute(
                select(users.c.id).where(users.c.email == TEST_EMAIL)
            ).first()
            if row is None:
                conn.execute(
                    users.insert().values(email=TEST_EMAIL, name="Race User")
                )

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=N_INSERT_THREADS) as pool:
        futures = [pool.submit(_bad_worker, engine) for _ in range(N_INSERT_THREADS)]
        for f in as_completed(futures):
            # Swallow exceptions in BAD demo (may get IntegrityError if
            # constraint somehow persists; that's fine)
            try:
                f.result()
            except Exception:
                pass
    bad_duration = (time.perf_counter() - start) * 1000

    with engine.connect() as conn:
        bad_count = conn.execute(
            select(func.count()).select_from(users).where(users.c.email == TEST_EMAIL)
        ).scalar() or 0

    # ── GOOD: Ensure unique constraint, use ON CONFLICT DO NOTHING ───────────
    with engine.begin() as conn:
        # Remove all rows with the test email (including duplicates from BAD)
        conn.execute(users.delete().where(users.c.email == TEST_EMAIL))
        # Re-add the unique constraint
        conn.execute(text(
            "ALTER TABLE concurrency_users "
            "ADD CONSTRAINT uq_concurrency_users_email UNIQUE (email)"
        ))

    def _good_worker(engine: Engine) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO concurrency_users (email, name) "
                    "VALUES (:email, :name) "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {"email": TEST_EMAIL, "name": "Race User"},
            )

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=N_INSERT_THREADS) as pool:
        futures = [pool.submit(_good_worker, engine) for _ in range(N_INSERT_THREADS)]
        for f in as_completed(futures):
            f.result()
    good_duration = (time.perf_counter() - start) * 1000

    with engine.connect() as conn:
        good_count = conn.execute(
            select(func.count()).select_from(users).where(users.c.email == TEST_EMAIL)
        ).scalar() or 0

    bad_r = ConcurrencyBench(
        label="App-level check, no constraint (BAD)",
        duration_ms=bad_duration,
        final_count=bad_count,
        expected_count=1,
        note=f"Created {bad_count} row(s) — {bad_count - 1} duplicate(s) from race",
    )
    good_r = ConcurrencyBench(
        label="UNIQUE + ON CONFLICT DO NOTHING (GOOD)",
        duration_ms=good_duration,
        final_count=good_count,
        expected_count=1,
        note="Exactly 1 row — duplicates silently ignored by DB",
    )
    print_benchmark([bad_r, good_r], "SCENARIO 4 — INSERT Race: App-level Check vs Upsert")


# ──────────────────────────────────────────────────────────────────────────────
# SCENARIO 5 — Distributed Lock: No Coordination vs Advisory Lock
# ──────────────────────────────────────────────────────────────────────────────

def demo_advisory_lock(engine: Engine) -> None:
    """Compare no coordination with PostgreSQL advisory locks.

    The BAD pattern has no coordination between database connections.
    A threading.Lock would work within one Python process but is useless
    in multi-server deployments where each server has its own lock instance.

    The GOOD pattern uses pg_advisory_xact_lock() — a database-level mutex
    that works across ALL connections to the database, regardless of which
    process or server they originate from. The lock is automatically released
    when the transaction commits.

    Advisory locks are ideal for coordinating access to shared resources
    that are not database rows (filesystem, external API rate limits, etc.).
    """

    expected = N_THREADS * N_INCREMENTS

    # ── BAD: No database-level coordination ──────────────────────────────────
    def _bad_worker(engine: Engine) -> None:
        with engine.begin() as conn:
            row = conn.execute(
                select(counter.c.count).where(counter.c.id == TARGET_ID)
            ).scalar()
            new_count = row + 1
            conn.execute(
                counter.update().where(counter.c.id == TARGET_ID).values(count=new_count)
            )

    _reset_counter(engine)
    start = time.perf_counter()
    _run_concurrent(engine, _bad_worker, N_THREADS, N_INCREMENTS)
    bad_duration = (time.perf_counter() - start) * 1000
    bad_final = _read_counter(engine)

    # ── GOOD: PostgreSQL advisory lock — coordinates across all connections ──
    def _good_worker(engine: Engine) -> None:
        with engine.begin() as conn:
            # Acquire transaction-level advisory lock — blocks others with same key
            conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": ADVISORY_LOCK_KEY},
            )
            row = conn.execute(
                select(counter.c.count).where(counter.c.id == TARGET_ID)
            ).scalar()
            new_count = row + 1
            conn.execute(
                counter.update().where(counter.c.id == TARGET_ID).values(count=new_count)
            )
            # Lock auto-released on COMMIT

    _reset_counter(engine)
    start = time.perf_counter()
    _run_concurrent(engine, _good_worker, N_THREADS, N_INCREMENTS)
    good_duration = (time.perf_counter() - start) * 1000
    good_final = _read_counter(engine)

    bad_r = ConcurrencyBench(
        label="No coordination (BAD)",
        duration_ms=bad_duration,
        final_count=bad_final,
        expected_count=expected,
        note=f"Lost {expected - bad_final} — threading.Lock would fail across servers",
    )
    good_r = ConcurrencyBench(
        label="pg_advisory_xact_lock (GOOD)",
        duration_ms=good_duration,
        final_count=good_final,
        expected_count=expected,
        note="Lock works across ALL connections — even from different processes",
    )
    print_benchmark([bad_r, good_r], "SCENARIO 5 — Distributed Lock: No Coordination vs Advisory Lock")


# ──────────────────────────────────────────────────────────────────────────────
# Summary output
# ──────────────────────────────────────────────────────────────────────────────

def print_summary() -> None:
    print("""
╔══════════════════════════════════════════════════════════════════════════════╗
║  SUMMARY -- Concurrency Pattern Takeaways                                   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  Race conditions happen when multiple transactions read and write the        ║
║  same data without proper coordination. Each scenario shows a different      ║
║  strategy for preventing or detecting conflicts.                             ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 1. SELECT FOR UPDATE (pessimistic locking)                           │    ║
║  │    Best for: High-contention writes on the same rows.                │    ║
║  │    Cost: Other writers block; careful with long transactions.         │    ║
║  │    Syntax: SELECT ... WHERE id = X FOR UPDATE                        │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 2. Atomic SQL UPDATE (single-statement)                              │    ║
║  │    Best for: Simple counter-like operations.                         │    ║
║  │    Cost: Only works for simple expressions; no app logic.             │    ║
║  │    Syntax: UPDATE t SET count = count + 1 WHERE id = X               │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 3. Optimistic Concurrency (version column)                           │    ║
║  │    Best for: Read-heavy, low-contention workloads.                   │    ║
║  │    Cost: Retries add overhead under contention; no locks held.        │    ║
║  │    Syntax: UPDATE t SET x = :x, version = version + 1                │    ║
║  │            WHERE id = :id AND version = :old_version                 │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 4. UNIQUE constraint + Upsert                                       │    ║
║  │    Best for: Preventing duplicate INSERTs gracefully.                │    ║
║  │    Cost: Constraint violation errors; ON CONFLICT is PostgreSQL-only. │    ║
║  │    Syntax: INSERT ... ON CONFLICT (col) DO NOTHING                   │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  ┌──────────────────────────────────────────────────────────────────────┐    ║
║  │ 5. PostgreSQL Advisory Locks                                        │    ║
║  │    Best for: Coordinating access to non-row resources across         │    ║
║  │    multiple processes/servers.                                       │    ║
║  │    Cost: Application-level lock management; no row-level semantics.   │    ║
║  │    Syntax: SELECT pg_advisory_xact_lock(key)                         │    ║
║  └──────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
║  Guiding principle:                                                        ║
║                                                                              ║
║    The database is the single source of truth. Any assumption that           ║
║    application-level locks (threading.Lock, asyncio.Lock) will               ║
║    protect against concurrent database access is flawed in a                 ║
║    distributed system. Always use database-level mechanisms to               ║
║    protect database state.                                                   ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
""")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="SQLAlchemy + PostgreSQL concurrency patterns demo"
    )
    parser.add_argument("--seed-only", action="store_true",
                        help="Only seed data, skip benchmarks")
    parser.add_argument("--bench", action="store_true",
                        help="Only run benchmarks (assumes seeded data)")
    parser.add_argument("--drop", action="store_true",
                        help="Drop all demo tables and exit")
    args = parser.parse_args()

    engine = create_engine(PG_URL, echo=False, pool_pre_ping=True)

    # ── Validate we can reach PostgreSQL ────────────────────────────────────
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar()
        print(f"  Connected to PostgreSQL\n    {version}\n")
    except Exception as e:
        print(f"  Cannot connect to PostgreSQL at:\n    {PG_URL}")
        print(f"    Error: {e}")
        print("\n    Make sure PostgreSQL is running and the database exists:")
        print(f"    $ createdb sqlalchemy_demo")
        raise SystemExit(1)

    # ── Drop only mode ──────────────────────────────────────────────────────
    if args.drop:
        drop_all(engine)
        print("  Dropped all demo tables.")
        return

    # ── Seed ────────────────────────────────────────────────────────────────
    if not args.bench:
        drop_all(engine)
        metadata.create_all(engine)
        print("  Seeding data...")
        seed_data(engine)
        print("  Seeding complete.\n")

    # ── Run benchmarks ──────────────────────────────────────────────────────
    if not args.seed_only:
        if args.bench:
            with engine.connect() as conn:
                try:
                    conn.execute(text("SELECT 1 FROM concurrency_counter LIMIT 1"))
                except Exception:
                    print("  Tables don't exist. Run without --bench first to seed data.")
                    print("   $ python concurrency_demo.py  # seeds data + runs benchmarks")
                    raise SystemExit(1)

        print("  Running Concurrency Pattern Benchmarks...")
        print(f"  Threads: {N_THREADS}, Operations per thread: {N_INCREMENTS}")
        print(f"  Total operations per scenario: {N_THREADS * N_INCREMENTS:,}\n")

        demo_lost_update(engine)
        demo_atomic_update(engine)
        demo_optimistic_cc(engine)
        demo_insert_race(engine)
        demo_advisory_lock(engine)

        print_summary()

    engine.dispose()


if __name__ == "__main__":
    main()
