# SQLAlchemy + PostgreSQL — Anti-Patterns & Fixes Demo

A hands-on demonstration of **12 common database performance anti-patterns** and their fixes, using SQLAlchemy with PostgreSQL.

## The Twelve Anti-Patterns

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

## Quick Start

```bash
# 1. Prerequisites
pip install sqlalchemy psycopg2-binary

# 2. Create the PostgreSQL database
createdb sqlalchemy_demo

# 3. Run the demo (seeds data + runs benchmarks)
python demo.py
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
📦  Seeding complete.

=======================================================================
  SCENARIO 1 — array_position vs @> (GIN-safe)
=======================================================================
  Scenario                                          Rows  Time (ms)  Speedup
  ───────────────────────────────────────────────── ────── ────────── ────────
  array_position (BAD)                               500    xxx.xx ms
  @> operator (GOOD)                                 500    xx.xx ms    ~2-5×

  ... (results continue for all 12 scenarios)
```

## Command-Line Options

| Flag | Purpose |
|------|---------|
| *(none)* | Seed data + run all benchmarks |
| `--seed-only` | Only seed data, skip benchmarks |
| `--bench` | Only run benchmarks (assumes data already seeded) |
| `--drop` | Drop all demo tables and exit |
| `PG_URL=...` env var | Custom PostgreSQL connection string |

## Project Structure

```
sqlalchemy_demo/
├── demo.py       # Single-file demo — all 12 scenarios
└── README.md     # This file
```

## Deep Dive: What Each Scenario Teaches

### 1. `array_position` vs `@>` Operator (GIN-aware)

```python
# BAD — forces sequential scan
func.array_position(picklist.c.codes_array, target_code).isnot(None)

# GOOD — uses GIN index
picklist.c.codes_array.op("@>")([target_code])
```

`array_position()` is a **function call** evaluated per row — PostgreSQL cannot use a GIN index. The `@>` (contains) operator is **GIN-native** and enables index scans.

### 2. `EXISTS` in SELECT vs WHERE

```python
# BAD — computes EXISTS for ALL rows, ships them all, Python filters
stmt = select(picklist.c.id, ..., exists_subq.label("needs_attention"))
rows = conn.execute(stmt).fetchall()
result = [r for r in rows if r.needs_attention]

# GOOD — DB filters before sending
stmt = select(picklist.c.id, ...).where(exists_subq)
result = conn.execute(stmt).fetchall()
```

The bad pattern wastes bandwidth and DB cycles computing EXISTS for rows that are immediately discarded.

### 3. Python `set()` Dedup vs SQL `DISTINCT ON`

```python
# BAD — all duplicate JOIN rows shipped to Python
seen = set()
for r in conn.execute(stmt).fetchall():
    if r.id not in seen:
        seen.add(r.id)
        result.append(r)

# GOOD — dedup in PostgreSQL via DISTINCT ON (picklist.id)
# (plain DISTINCT would dedup on ALL columns, different result!)
stmt = stmt.distinct(picklist.c.id)  # DISTINCT ON (picklist.id)
result = conn.execute(stmt).fetchall()
```

### 4. N+1 `nextval()` vs Batched `generate_series`

```python
# BAD — N round-trips for N sequence values
for _ in range(n):
    ids.append(conn.execute(text("SELECT nextval('seq')")).scalar_one())

# GOOD — 1 round-trip
conn.execute(text("SELECT nextval('seq') FROM generate_series(1, N)")).fetchall()
```

Each DB round-trip adds ~1-5ms overhead. Over 1,000 rows that's 1+ second of pure latency.

### 5. N+1 ORM Pattern — Loop Querying Children per Parent

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

### 6. Function in WHERE — Index Suppression

```python
# BAD — LOWER(code) wraps the column, index on 'code' is unusable
stmt = select(picklist).where(func.lower(picklist.c.code) == 'pl-000001')

# GOOD — value matches stored format, uses unique index
stmt = select(picklist).where(picklist.c.code == 'PL-000001')
```

Wrapping a column in a function (`LOWER`, `DATE`, `EXTRACT`, etc.) inside WHERE prevents PostgreSQL from using a plain index. The DB must scan every row and apply the function. **Fix**: match the stored format, or create a **functional index** (`CREATE INDEX ON picklist (LOWER(code))`).

### 7. `SELECT *` — Over-fetching All Columns

```python
# BAD — fetches ALL columns including the large ARRAY codes_array
stmt = select(picklist).limit(5000)

# GOOD — fetches only what's needed
stmt = select(picklist.c.id, picklist.c.code, picklist.c.status).limit(5000)
```

`SELECT *` fetches every column — including large text/array/BLOB columns. This wastes I/O on disk, network bandwidth to the application, and memory for deserialization. **Fix**: always name the columns you actually need.

### 8. `NOT IN` (subquery) vs `NOT EXISTS`

```python
# BAD — NOT IN returns ZERO rows if subquery contains NULL (wrong!)
# and is typically slower
subq = select(pickitem.c.picklist_id).where(pickitem.c.needs_review == 1)
stmt = select(picklist).where(picklist.c.id.not_in(subq))

# GOOD — NOT EXISTS is correct (NULL-safe) and faster
exists_subq = select(...).where(...).correlate(picklist).exists()
stmt = select(picklist).where(~exists_subq)
```

`NOT IN (subquery)` has **two problems**:
1. **Correctness**: If the subquery returns **any NULL**, `NOT IN` evaluates to `UNKNOWN` for every row — returning **zero rows** silently.
2. **Performance**: PostgreSQL can't optimize `NOT IN` as well as `NOT EXISTS`, often falling back to sequential scans.

**Fix**: Always use `NOT EXISTS` for subquery exclusion checks.

### 9. Missing Index on Foreign Key — Sequential Scan vs Index Scan

```python
# BAD — JOIN on unindexed FK column forces sequential scan on child table
stmt = (
    select(picklist.c.id, picklist.c.code, picklist_audit.c.action)
    .select_from(picklist.join(picklist_audit, picklist.c.id == picklist_audit.c.picklist_id))
    .where(picklist.c.status.in_(["pending", "active"]))
    .limit(2000)
)

# GOOD — same JOIN after CREATE INDEX ON picklist_audit_demo (picklist_id)
# The index enables an index scan instead of a sequential scan
```

A foreign key column without an index is the **#1 most cited PostgreSQL performance anti-pattern** (11/14 authoritative sources). When joining on an unindexed FK, PostgreSQL must perform a sequential scan on the child table. Adding a simple B-tree index on the FK column enables index scans — typically **10-100× faster** for joined queries.

**Fix**: Always create an index on foreign key columns that participate in JOINs:
```sql
CREATE INDEX ON picklist_audit_demo (picklist_id);
```

### 10. Deep OFFSET Pagination vs Keyset/Cursor Pagination

```python
# BAD — LIMIT 20 OFFSET 9000: Postgres reads 9020 rows, discards 9000
stmt = (
    select(picklist.c.id, picklist.c.code)
    .order_by(picklist.c.id)
    .limit(20)
    .offset(9000)
)

# GOOD — keyset pagination: seeks directly to position
cursor_id = 9000  # last seen ID from previous page
stmt = (
    select(picklist.c.id, picklist.c.code)
    .where(picklist.c.id > cursor_id)
    .order_by(picklist.c.id)
    .limit(20)
)
```

`LIMIT/OFFSET` pagination becomes catastrophically slow at large offsets because PostgreSQL must scan and discard N skipped rows (mentioned by 9/14 sources). Keyset pagination (also called "cursor" or "seek" pagination) uses `WHERE id > last_seen` to seek directly to the position, reading only the rows returned.

**Fix**: Use keyset/cursor pagination for deep pages. The application tracks the last seen primary key and paginates forward from it.

### 11. Implicit Type Casting — Index Suppression

```python
# BAD — integer literal compared to text column: forces type coercion
# PostgreSQL casts name::int for every row, disabling the unique index
stmt = select(team).where(team.c.name == 42)

# GOOD — text literal matches text column type, uses unique index
stmt = select(team).where(team.c.name == '42')
```

When column and literal types don't match, PostgreSQL casts the **column side** (not the literal), which disables index usage (mentioned by 8/14 sources). In the bad case, `WHERE text_col = 123` forces PostgreSQL to cast every row's text to integer — a sequential scan that also risks type conversion errors.

**Fix**: Always match the literal type to the column type in WHERE clauses. Use `'123'` for text columns, `123` for integer columns.

### 12. Single-row INSERT Loop vs Multi-row INSERT

```python
# BAD — N round-trips: insert one row at a time in a loop
for i in range(500):
    conn.execute(demo_log.insert().values(value=f"item-{i}"))

# GOOD — 1 round-trip: multi-row INSERT with list of dicts
conn.execute(
    demo_log.insert(),
    [{"value": f"item-{i}"} for i in range(500)],
)
```

Inserting rows one at a time in a loop creates N round-trips to the database (mentioned by 7/14 sources). A single multi-row INSERT sends all rows in one round-trip. The difference is dominated by network round-trip latency — on a local connection each round-trip adds ~1-5ms, but across a network this can be 10-100ms per trip.

**Fix**: Use multi-row INSERT with a list of dictionaries. SQLAlchemy's `conn.execute(table.insert(), list_of_dicts)` automatically generates a single multi-row INSERT statement.

## The Common Thread

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

## Connection String

The default connection URL is:
```
postgresql://postgres:postgres@localhost:5432/sqlalchemy_demo
```

Override with the `PG_URL` environment variable:
```bash
PG_URL="postgresql://myuser:mypass@myhost:5432/mydb" python demo.py
```
