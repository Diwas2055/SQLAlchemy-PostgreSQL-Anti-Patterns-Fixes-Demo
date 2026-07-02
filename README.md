# SQLAlchemy + PostgreSQL — Three Demos + Migration Tool

A hands-on project with **three standalone demos** and a **database migration tool** covering performance anti-patterns, event-driven pipelines, race condition prevention, and schema management — all using SQLAlchemy with PostgreSQL.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Demo 1: 12 Performance Anti-Patterns → `demo.py`](#demo-1-12-performance-anti-patterns---demopy)
3. [Demo 2: PostgreSQL Triggers + Redis Pub/Sub → `pg_triggers_redis.py`](#demo-2-postgresql-triggers--redis-pubsub---pg_triggers_redispy)
4. [Demo 3: 5 Race Condition Patterns → `concurrency_demo.py`](#demo-3-5-race-condition-patterns---concurrency_demopy)
5. [Migration Tool: Cleo-Powered CLI → `migrations/`](#migration-tool-cleo-powered-cli---migrations)
6. [Project Structure](#project-structure)
7. [Connection String](#connection-string)

---

## Quick Start

### Prerequisites

```bash
# Python dependencies
pip install sqlalchemy psycopg2-binary

# For Demo 2 (Redis/SSE):
pip install redis fastapi uvicorn sse-starlette

# For the Migration Tool (Cleo CLI):
pip install cleo

# Create the PostgreSQL database
createdb sqlalchemy_demo
```

### Run All Demos

```bash
# Demo 1 — Performance anti-patterns (12 scenarios)
python demo.py

# Demo 2 — Event-driven pipeline (triggers → NOTIFY → Redis → SSE)
python pg_triggers_redis.py

# Demo 3 — Race condition prevention (5 scenarios with EXPLAIN ANALYZE)
python concurrency_demo.py
```

---

## Demo 1: 12 Performance Anti-Patterns — `demo.py`

A hands-on demonstration of **12 common database performance anti-patterns** and their fixes, using SQLAlchemy with PostgreSQL.

### The Twelve Anti-Patterns

| # | Anti-Pattern (BAD) | Fix (GOOD) | Theme |
|---|-------------------|------------|-------|
| 1 | `array_position()` in WHERE | `@>` GIN-compatible operator | Index-aware querying |
| 2 | `EXISTS` as SELECT column, filter in Python | `WHERE EXISTS` — filter in DB | Push work to the DB |
| 3 | Fetch JOIN duplicates, dedup with Python `set()` | `DISTINCT ON (id)` — dedup in DB | Reduce wire traffic |
| 4 | Per-row `nextval()` in loop (N round-trips) | `generate_series(1, N)` batch (1 round-trip) | Batch, don't loop |
| 5 | N+1 ORM: loop query children per parent | JOIN once | Single round-trip |
| 6 | `WHERE LOWER(col) = value` — no index | `WHERE col = value` — uses index | Index hygiene |
| 7 | `SELECT *` — fetches all columns | `SELECT col1, col2, …` — only what's needed | Minimize data transfer |
| 8 | `NOT IN (subquery)` — slow, wrong with NULLs | `NOT EXISTS` — correct and fast | Safe exclusion |
| 9 | JOIN on unindexed FK column — sequential scan | CREATE INDEX on FK column — index scan | Index foreign keys |
| 10 | `LIMIT/OFFSET` deep pagination (reads + discards N rows) | Keyset `WHERE id > last_seen` — seeks directly | Cursor pagination |
| 11 | `WHERE text_col = 123` — type coercion disables index | `WHERE text_col = '123'` — matching types uses index | Type hygiene |
| 12 | Single-row INSERT in loop (N round-trips) | Multi-row INSERT with list of dicts (1 round-trip) | Batch DML |

### Usage

```bash
python demo.py                    # Seed data + run all benchmarks
python demo.py --seed-only        # Only seed data
python demo.py --bench            # Only run benchmarks (assumes seeded data)
```

Expected output:
```
🔗  Connected to PostgreSQL …

📦  Seeding data…
  ✅  Seeded 10,000 picklists
  ✅  Seeded 51 teams
  ✅  Seeded ~30,000 team assignments
  ✅  Seeded ~25,000 pick items
  ✅  Seeded 50,000 audit records

=======================================================================
  SCENARIO 1 — array_position vs @> (GIN-safe)
=======================================================================
  Scenario                                          Rows  Time (ms)  Speedup
  ───────────────────────────────────────────────── ────── ────────── ────────
  array_position (BAD)                               500    xxx.xx ms
  @> operator (GOOD)                                 500    xx.xx ms    ~2-5×
  ... (12 scenarios total)
```

### Deep Dive: What Each Scenario Teaches

#### 1. `array_position` vs `@>` Operator (GIN-aware)

```python
# BAD — forces sequential scan
func.array_position(picklist.c.codes_array, target_code).isnot(None)

# GOOD — uses GIN index
picklist.c.codes_array.op("@>")([target_code])
```

`array_position()` is a **function call** evaluated per row — PostgreSQL cannot use a GIN index. The `@>` (contains) operator is **GIN-native** and enables index scans.

#### 2. `EXISTS` in SELECT vs WHERE

```python
# BAD — computes EXISTS for ALL rows, ships them all, Python filters
stmt = select(picklist.c.id, ..., exists_subq.label("needs_attention"))
rows = conn.execute(stmt).fetchall()
result = [r for r in rows if r.needs_attention]

# GOOD — DB filters before sending
stmt = select(picklist.c.id, ...).where(exists_subq)
result = conn.execute(stmt).fetchall()
```

The bad pattern wastes bandwidth and DB cycles computing EXISTS for rows immediately discarded.

#### 3. Python `set()` Dedup vs SQL `DISTINCT ON`

```python
# BAD — all duplicate JOIN rows shipped to Python
seen = set()
for r in conn.execute(stmt).fetchall():
    if r.id not in seen:
        seen.add(r.id)
        result.append(r)

# GOOD — dedup in PostgreSQL via DISTINCT ON (picklist.id)
stmt = stmt.distinct(picklist.c.id)  # DISTINCT ON (picklist.id)
result = conn.execute(stmt).fetchall()
```

#### 4. N+1 `nextval()` vs Batched `generate_series`

```python
# BAD — N round-trips for N sequence values
for _ in range(n):
    ids.append(conn.execute(text("SELECT nextval('seq')")).scalar_one())

# GOOD — 1 round-trip
conn.execute(text("SELECT nextval('seq') FROM generate_series(1, N)")).fetchall()
```

Each DB round-trip adds ~1-5ms overhead. Over 1,000 rows that's 1+ second of pure latency.

#### 5. N+1 ORM Pattern — Loop Querying Children per Parent

```python
# BAD — N+1 queries: 1 for parents + N for children
parents = conn.execute(select(picklist).where(...)).fetchall()
for p in parents:
    children = conn.execute(select(pickitem).where(...)).fetchall()

# GOOD — single JOIN, one round-trip
stmt = select(picklist, pickitem).join(pickitem, ...).where(...)
rows = conn.execute(stmt).fetchall()
```

The **classic N+1**: query N parent rows, then fire one child query per parent. 1 query becomes N+1. The difference grows linearly with the number of parents.

#### 6. Function in WHERE — Index Suppression

```python
# BAD — LOWER(code) wraps the column, index on 'code' is unusable
stmt = select(picklist).where(func.lower(picklist.c.code) == 'pl-000001')

# GOOD — value matches stored format, uses unique index
stmt = select(picklist).where(picklist.c.code == 'PL-000001')
```

Wrapping a column in a function (`LOWER`, `DATE`, `EXTRACT`, etc.) inside WHERE prevents PostgreSQL from using a plain index. **Fix**: match the stored format, or create a **functional index** (`CREATE INDEX ON picklist (LOWER(code))`).

#### 7. `SELECT *` — Over-fetching All Columns

```python
# BAD — fetches ALL columns including the large ARRAY codes_array
stmt = select(picklist).limit(5000)

# GOOD — fetches only what's needed
stmt = select(picklist.c.id, picklist.c.code, picklist.c.status).limit(5000)
```

`SELECT *` fetches every column — including large text/array/BLOB columns. **Fix**: always name the columns you actually need.

#### 8. `NOT IN` (subquery) vs `NOT EXISTS`

```python
# BAD — NOT IN returns ZERO rows if subquery contains NULL (wrong!)
subq = select(pickitem.c.picklist_id).where(pickitem.c.needs_review == 1)
stmt = select(picklist).where(picklist.c.id.not_in(subq))

# GOOD — NOT EXISTS is correct (NULL-safe) and faster
exists_subq = select(...).where(...).correlate(picklist).exists()
stmt = select(picklist).where(~exists_subq)
```

`NOT IN (subquery)` has **two problems**:
1. **Correctness**: If the subquery returns **any NULL**, `NOT IN` evaluates to `UNKNOWN` for every row — returning **zero rows** silently.
2. **Performance**: PostgreSQL can't optimize `NOT IN` as well as `NOT EXISTS`.

**Fix**: Always use `NOT EXISTS` for subquery exclusion checks.

#### 9. Missing Index on Foreign Key

```python
# BAD — JOIN on unindexed FK column forces sequential scan on child table
stmt = (
    select(picklist.c.id, picklist.c.code, picklist_audit.c.action)
    .select_from(picklist.join(picklist_audit, ...))
    .where(picklist.c.status.in_(["pending", "active"]))
    .limit(2000)
)
```

A foreign key column without an index is the **#1 most cited PostgreSQL performance anti-pattern**. **Fix**: Always create an index on FK columns that participate in JOINs:

```sql
CREATE INDEX ON picklist_audit_demo (picklist_id);
```

#### 10. Deep OFFSET Pagination vs Keyset/Cursor Pagination

```python
# BAD — LIMIT 20 OFFSET 9000: Postgres reads 9020 rows, discards 9000
stmt = select(picklist.c.id, picklist.c.code).order_by(picklist.c.id).limit(20).offset(9000)

# GOOD — keyset pagination: seeks directly to position
cursor_id = 9000
stmt = select(picklist.c.id, picklist.c.code).where(picklist.c.id > cursor_id).order_by(picklist.c.id).limit(20)
```

`LIMIT/OFFSET` becomes catastrophically slow at large offsets. **Fix**: Use keyset/cursor pagination for deep pages.

#### 11. Implicit Type Casting — Index Suppression

```python
# BAD — integer literal compared to text column: forces type coercion
stmt = select(team).where(team.c.name == 42)

# GOOD — text literal matches text column type, uses unique index
stmt = select(team).where(team.c.name == '42')
```

When column and literal types don't match, PostgreSQL casts the **column side** (not the literal), disabling index usage. **Fix**: Always match the literal type to the column type.

#### 12. Single-row INSERT Loop vs Multi-row INSERT

```python
# BAD — N round-trips: insert one row at a time in a loop
for i in range(500):
    conn.execute(demo_log.insert().values(value=f"item-{i}"))

# GOOD — 1 round-trip: multi-row INSERT with list of dicts
conn.execute(demo_log.insert(), [{"value": f"item-{i}"} for i in range(500)])
```

**Fix**: Use multi-row INSERT with a list of dictionaries. SQLAlchemy's `conn.execute(table.insert(), list_of_dicts)` automatically generates a single multi-row INSERT statement.

### The Common Thread

All 12 anti-patterns share the same root cause:

> **Work that can be done once in the database is moved to the application layer and repeated per-row — wasting bandwidth, CPU cycles, and database connections.**

| # | What's Wasted | Fix Strategy |
|---|--------------|--------------|
| 1 | Sequential scan instead of index scan | Use GIN-compatible `@>` operator |
| 2 | DB computes EXISTS for all rows, Python discards most | Push `EXISTS` to `WHERE` clause |
| 3 | Duplicate rows shipped over network, Python dedups | Push `DISTINCT ON` to SQL |
| 4 | N round-trips for N sequence values | Batch with `generate_series` |
| 5 | N child queries for N parents | Use `JOIN` |
| 6 | Sequential scan because function wraps column | Don't wrap columns in `WHERE` |
| 7 | Entire rows shipped, most columns unused | Select only needed columns |
| 8 | Slow/correctness trap with `NOT IN` | Use `NOT EXISTS` |
| 9 | Sequential scan on child table from JOIN on unindexed FK | CREATE INDEX ON child (fk_column) |
| 10 | DB reads + discards N rows for OFFSET N | Keyset pagination (`WHERE id > ?`) |
| 11 | Sequential scan because type mismatch disables index | Match literal type to column type |
| 12 | N round-trips for N row inserts | Multi-row INSERT with list of dicts |

---

## Demo 2: PostgreSQL Triggers + Redis Pub/Sub — `pg_triggers_redis.py`

A companion demo showing how to build an **event-driven pipeline** using PostgreSQL triggers, `LISTEN`/`NOTIFY`, and Redis Pub/Sub.

### Architecture

```
┌──────────────┐    NOTIFY 'demo_events'  ┌──────────────────┐
│  PostgreSQL   │ ←─────────────────────→ │  Python Listener  │
│  (trigger fn) │                         │  (LISTEN/forward) │
│  on INSERT/   │                         └────────┬─────────┘
│   UPDATE/     │                                  │ PUBLISH to
│   DELETE      │                                  │ Redis
└──────────────┘                           ┌────────┴─────────┐
                                           │    Redis Pub/Sub  │
                                           │   (event bus)     │
                                           └────────┬─────────┘
                                                    │ SUBSCRIBE
                                           ┌────────┴─────────┐
                                           │  Subscriber(s)   │
                                           │ (other services) │
                                           └──────────────────┘
```

### What It Demonstrates

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Trigger: Audit Logging** | PL/pgSQL `AFTER INSERT OR UPDATE OR DELETE` | Auto-log every row change into `demo_orders_audit` with old/new JSONB snapshots |
| **Trigger: NOTIFY** | `pg_notify('demo_events', payload)` | Send JSON payload on every change to a PostgreSQL channel |
| **Listener** | `psycopg2` + `LISTEN demo_events` | Python process that listens for notifications in real-time |
| **Redis Bridge** | `redis-py` pub/sub | Forward events from PostgreSQL to a Redis channel |
| **Subscriber** | `redis-py` subscriber | Consume events from Redis (simulates downstream services) |
| **SSE Server** | FastAPI + `EventSourceResponse` | Push events to web browsers in real-time over HTTP SSE |

### Usage

```bash
# Full end-to-end demo (setup triggers → simulate changes → show pipeline)
python pg_triggers_redis.py

# Individual components:
python pg_triggers_redis.py --setup-only   # Create schema + triggers
python pg_triggers_redis.py --listen        # Start pg → Redis listener
python pg_triggers_redis.py --simulate      # Insert/update/delete demo data
python pg_triggers_redis.py --subscribe     # Subscribe to Redis channel
python pg_triggers_redis.py --serve         # Start SSE server (browser dashboard)
python pg_triggers_redis.py --cleanup       # Drop all triggers and tables
```

### SSE (Server-Sent Events) — Live Browser Dashboard

The `--serve` flag starts a **FastAPI server** with two SSE implementations:

| Endpoint | Implementation | Dependency |
|----------|---------------|------------|
| `GET /events` | `StreamingResponse` (raw SSE protocol) | `fastapi` + `uvicorn` only |
| `GET /events-starlette` | `EventSourceResponse` (sse-starlette) | `fastapi` + `uvicorn` + `sse-starlette` |
| `GET /` | HTML dashboard with live event display | (same as above) |

```bash
# Terminal 1 — SSE server
python pg_triggers_redis.py --serve

# Terminal 2 — Trigger events
python pg_triggers_redis.py --simulate
```

Open **http://localhost:8765/** to see events appear in real-time.

### Trigger Details

**Audit Trigger** — fires `AFTER INSERT OR UPDATE OR DELETE`, captures before/after row state as JSONB:

```sql
CREATE OR REPLACE FUNCTION fn_demo_orders_audit()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO demo_orders_audit (order_id, action, new_data)
        VALUES (NEW.id, 'INSERT', row_to_json(NEW)::jsonb);
    ELSIF TG_OP = 'UPDATE' THEN
        INSERT INTO demo_orders_audit (order_id, action, old_data, new_data)
        VALUES (NEW.id, 'UPDATE', row_to_json(OLD)::jsonb, row_to_json(NEW)::jsonb);
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO demo_orders_audit (order_id, action, old_data)
        VALUES (OLD.id, 'DELETE', row_to_json(OLD)::jsonb);
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
```

**Notify Trigger** — sends JSON payload via `pg_notify()`:

```sql
CREATE OR REPLACE FUNCTION fn_demo_orders_notify()
RETURNS TRIGGER AS $$
DECLARE
    payload TEXT;
BEGIN
    payload := json_build_object(
        'table',   TG_TABLE_NAME,
        'action',  TG_OP,
        'id',      COALESCE(NEW.id, OLD.id),
        'time',    now()::timestamptz,
        'summary', ...
    )::text;
    PERFORM pg_notify('demo_events', payload);
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
```

### Real-World Use Cases

- **Change Data Capture (CDC)** — Track all changes for auditing or replication
- **Real-time dashboards** — Push updates to frontend via WebSocket/SSE
- **Cache invalidation** — Invalidate Redis caches when data changes
- **Event-driven microservices** — Trigger downstream workflows
- **Search index sync** — Update Elasticsearch/Meilisearch when records change

---

## Demo 3: 5 Race Condition Patterns — `concurrency_demo.py`

A demonstration of **5 race condition scenarios** and their prevention strategies, using SQLAlchemy with PostgreSQL. Each scenario runs **10 concurrent threads × 100 operations** (1,000 total) to make race conditions visible, shows a correctness column (YES/NO), and includes **EXPLAIN ANALYZE** output of the actual PostgreSQL query plans.

### The Five Scenarios

| # | Scenario | BAD (what fails) | GOOD (the fix) | Mechanism |
|---|----------|-------------------|----------------|----------|
| 1 | **Lost Update** | No locking — read-modify-write in Python without coordination | `SELECT FOR UPDATE` — pessimistic row lock serializes concurrent writers | Row-level lock |
| 2 | **Read-Modify-Write vs Atomic** | `FOR UPDATE` + Python increment (2 round-trips per operation) | `UPDATE count = count + 1` (1 round-trip, DB does increment internally) | Atomic SQL |
| 3 | **Optimistic Concurrency** | No version check — second write silently overwrites first | Version column + retry on `rowcount == 0` — detects conflicts without row locks | Version column |
| 4 | **INSERT Race** | App-level `SELECT` then `INSERT` — race window allows duplicates | `UNIQUE` constraint + `ON CONFLICT DO NOTHING` — DB atomically prevents duplicates | Upsert |
| 5 | **Distributed Lock** | No coordination — `threading.Lock` fails across servers | `pg_advisory_xact_lock()` — database mutex works across all connections | Advisory lock |

### Usage

```bash
python concurrency_demo.py                  # Full run (seed + benchmarks + EXPLAIN ANALYZE)
python concurrency_demo.py --seed-only      # Just create tables + seed data
python concurrency_demo.py --bench          # Only benchmarks (assumes seeded data)
python concurrency_demo.py --drop           # Drop all demo tables
```

### Output

```
  Running Concurrency Pattern Benchmarks...
  Threads: 10, Operations per thread: 100
  Total operations per scenario: 1,000

=======================================================================
  SCENARIO 1 — Lost Update: No Locking vs SELECT FOR UPDATE
=======================================================================
  Approach                                      Correct  Final  Expected  Time (ms)
  ───────────────────────────────────────────── ─────── ─────── ──────── ──────────
  No locking (BAD)                                    NO     47    1,000    xxx.xx ms
  SELECT FOR UPDATE (GOOD)                            YES  1,000   1,000    xxx.xx ms

  ── EXPLAIN ANALYZE: BAD — Plain SELECT (no lock) ──
    Seq Scan on concurrency_counter  (cost=0.00..1.01 rows=1 width=4)
      Filter: (id = 1)

  ── EXPLAIN ANALYZE: GOOD — SELECT FOR UPDATE (row lock) ──
    Seq Scan on concurrency_counter  (cost=0.00..1.01 rows=1 width=4)
      Filter: (id = 1)
      Lock: Row Exclusive  ← KEY DIFFERENCE
```

### How It Works

Each scenario defines a **BAD worker function** (the anti-pattern) and a **GOOD worker function** (the fix). Both are executed concurrently using `ThreadPoolExecutor` with 10 threads, each performing 100 operations. After each run:

1. The **final counter value** is compared to the expected value (1,000)
2. The **correctness** column shows YES or NO
3. The **timing** shows performance impact
4. **EXPLAIN ANALYZE** shows the actual PostgreSQL query plan

### Key Insights by Scenario

| Scenario | Key Insight |
|----------|------------|
| 1 | Both query plans look identical — but FOR UPDATE adds a`Lock: Row Exclusive` that serializes concurrent writers |
| 2 | The atomic UPDATE eliminates one round-trip. Both UPDATE plans are similar, but cutting round-trips in half is critical at high concurrency |
| 3 | The version check (`WHERE id = X AND version = Y`) is a B-tree index lookup — same cost as a normal UPDATE. When version doesn't match, `rowcount = 0` triggers a retry |
| 4 | `ON CONFLICT` adds an anti-join step that atomically checks for existing rows. No race window — the DB handles check+insert as one uninterruptible operation |
| 5 | `pg_advisory_xact_lock()` acquires a database-level mutex that works across ALL connections, processes, and servers. Unlike `FOR UPDATE`, it locks an abstract integer key |

---

## Migration Tool: Cleo-Powered CLI — `migrations/`

A **PostgreSQL-first database migration tool** with auto-detection of schema changes from SQLAlchemy model definitions. Built with [Cleo](https://github.com/python-poetry/cleo) for the CLI.

### Features

- **Auto-detect changes** — compares SQLAlchemy model definitions against `information_schema` to generate migrations
- **5 CLI commands** — `init`, `make:migration`, `migrate`, `rollback`, `status`
- **Batch tracking** — tracks applied migrations in a `_schema_migrations` table with batch numbers
- **Upgrade & Downgrade** — each migration file has both `upgrade()` and `downgrade()` functions
- **PostgreSQL-aware** — understands ARRAY types, JSONB, sequences, and PostgreSQL-specific DDL
- **TOML configuration** — all settings in `pyproject.toml`, overridable via env vars

### Architecture

```
migrations/
├── cli.py              # Cleo CLI application (5 commands)
├── config.py           # TOML + env-var configuration loader
├── models.py           # Declarative SQLAlchemy models for all project schemas
├── detector.py         # Schema diff engine (models vs information_schema)
├── writer.py           # Migration file generator (timestamped Python files)
├── manager.py          # Migration runner (tracking, execution, rollback)
├── pyproject.toml      # Tool configuration
└── versions/           # Auto-generated migration files
    ├── __init__.py
    ├── 20250101_120000_initial_schema.py
    └── ...
```

### CLI Commands

```bash
python migrations/cli.py list                          # Show all commands

python migrations/cli.py init                          # Create tracking table + all model tables

python migrations/cli.py make:migration "add_user_table"   # Auto-detect changes & generate migration
python migrations/cli.py make:migration "add_index" --no-detect  # Skip auto-detection, empty template

python migrations/cli.py migrate                       # Run all pending migrations
python migrations/cli.py migrate --target 20250101_120000  # Migrate up to specific version

python migrations/cli.py rollback                      # Roll back last batch
python migrations/cli.py rollback --steps 2            # Roll back 2 batches

python migrations/cli.py status                        # Show applied vs pending
```

### Configuration (`migrations/pyproject.toml`)

```toml
[tool.migrations]
database_url = "postgresql://postgres:postgres@localhost:5432/sqlalchemy_demo"
migrations_table = "_schema_migrations"
versions_dir = "versions"
auto_generate = true
postgres_schema = "public"
```

Configuration priority: **CLI flag** > **environment variable** > **config file** > **default**.

### How Auto-Detection Works

The `detector.py` engine:

1. Reads current database schema from PostgreSQL's `information_schema.columns`, `information_schema.tables`, and `information_schema.table_constraints`
2. Compares against SQLAlchemy `Table` objects from `models.py`
3. Detects: **new tables**, **dropped tables**, **new columns**, **dropped columns**, **type changes**
4. Generates both `upgrade()` and `downgrade()` SQL statements

### Migration File Format

Auto-generated files look like this:

```python
"""
Migration: 20250101_120000
Created:   2025-01-01 12:00:00 UTC
Description: add_user_table
"""

MIGRATION_ID = "20250101_120000"
REVISION = "base"

def upgrade(connection) -> None:
    connection.execute(
        "CREATE TABLE concurrency_users ("
        "    id SERIAL PRIMARY KEY,"
        "    email TEXT NOT NULL,"
        "    name TEXT NOT NULL,"
        "    CONSTRAINT uq_concurrency_users_email UNIQUE (email)"
        ");"
    )

def downgrade(connection) -> None:
    connection.execute("DROP TABLE IF EXISTS concurrency_users CASCADE;")
```

---

## Project Structure

```
sqlalchemy_demo/
├── demo.py                    # 12 performance anti-pattern scenarios
├── pg_triggers_redis.py       # PostgreSQL triggers + Redis pub/sub + SSE
├── concurrency_demo.py        # 5 race condition patterns + EXPLAIN ANALYZE
├── migrations/                # Cleo-powered PostgreSQL migration tool
│   ├── __init__.py
│   ├── cli.py                 # CLI application (5 commands)
│   ├── config.py              # TOML configuration loader
│   ├── models.py              # SQLAlchemy model definitions
│   ├── detector.py            # Schema diff engine
│   ├── writer.py              # Migration file generator
│   ├── manager.py             # Migration runner
│   ├── pyproject.toml         # Tool configuration
│   └── versions/              # Auto-generated migration files
├── .gitignore
└── README.md
```

---

## Connection String

All three demos and the migration tool share the same default connection:

```
postgresql://postgres:postgres@localhost:5432/sqlalchemy_demo
```

Override with the `PG_URL` environment variable:

```bash
PG_URL="postgresql://myuser:mypass@myhost:5432/mydb" python demo.py
PG_URL="postgresql://myuser:mypass@myhost:5432/mydb" python concurrency_demo.py
PG_URL="postgresql://myuser:mypass@myhost:5432/mydb" python pg_triggers_redis.py
PG_URL="postgresql://myuser:mypass@myhost:5432/mydb" python migrations/cli.py migrate
```
