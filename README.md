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

---

# PostgreSQL Triggers + Redis Pub/Sub Demo

A companion demo showing how to build an **event-driven pipeline** using PostgreSQL triggers, `LISTEN`/`NOTIFY`, and Redis Pub/Sub.

**File:** `pg_triggers_redis.py`

## Architecture

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

## What It Demonstrates

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Trigger: Audit Logging** | PL/pgSQL `AFTER INSERT OR UPDATE OR DELETE` | Auto-log every row change into `demo_orders_audit` with old/new JSONB snapshots |
| **Trigger: NOTIFY** | `pg_notify('demo_events', payload)` | Send JSON payload on every change to a PostgreSQL channel |
| **Listener** | `psycopg2` + `LISTEN demo_events` | Python process that listens for notifications in real-time |
| **Redis Bridge** | `redis-py` pub/sub | Forward events from PostgreSQL to a Redis channel |
| **Subscriber** | `redis-py` subscriber | Consume events from Redis (simulates downstream services) |

## Quick Start

```bash
# 1. Install dependencies
pip install psycopg2-binary redis

# 2. Make sure Redis is running
redis-server

# 3. Run the full demo
python pg_triggers_redis.py
```

The full demo will:
1. Create `demo_orders` and `demo_orders_audit` tables
2. Install trigger functions (`fn_demo_orders_audit`, `fn_demo_orders_notify`)
3. Start a Redis subscriber in a background thread
4. Start a PostgreSQL listener in a subprocess
5. Simulate 5 INSERTs, 5 UPDATEs, and 1 DELETE
6. Show the audit trail and verify Redis event delivery

### Expected Output

```
🔗  Connected to PostgreSQL …

📦  Creating schema...
📦  Creating trigger functions...
📦  Installing triggers...
  ✅  Schema + triggers ready

════════════════════════════════════════════════════════════
  🚀  Starting full pipeline demo...
════════════════════════════════════════════════════════════

📦  Simulating changes on demo_orders...

  ──  INSERT 5 orders ──
     ✅  Order #1: Alice ordered 2x Widget Alpha
     ✅  Order #2: Bob ordered 1x Gadget Beta
     ✅  Order #3: Charlie ordered 5x Widget Alpha
     ✅  Order #4: Diana ordered 3x Gadget Beta
     ✅  Order #5: Eve ordered 10x Doohickey Gamma

  ──  UPDATE statuses ──
     🔄  Order #1: status → 'confirmed'
     🔄  Order #2: status → 'shipped'
     🔄  Order #3: status → 'delivered'
     🔄  Order #4: status → 'pending'
     🔄  Order #5: status → 'confirmed'

  ──  DELETE 1 order ──
     🗑️  Order #5: deleted

  ──  Audit trail (demo_orders_audit) ──
     📝  Order #1: INSERT at 08:30:00.123
     📝  Order #2: INSERT at 08:30:00.124
     📝  Order #3: INSERT at 08:30:00.125
     📝  Order #4: INSERT at 08:30:00.126
     📝  Order #5: INSERT at 08:30:00.127
     📝  Order #1: UPDATE at 08:30:00.234
     📝  Order #2: UPDATE at 08:30:00.235
     ... (11 audit rows total)

════════════════════════════════════════════════════════════
  📊  Demo Results
════════════════════════════════════════════════════════════

  Redis events collected: 11

    •    INSERT  #1  | Order #1: Alice ordered 2x Widget Alpha
    •    INSERT  #2  | Order #2: Bob ordered 1x Gadget Beta
    •    INSERT  #3  | Order #3: Charlie ordered 5x Widget Alpha
    ... (11 events total)

  ✅  Pipeline verified: DB trigger → pg_notify → Python → Redis → subscriber
```

## Command-Line Options

| Flag | Purpose |
|------|---------|
| *(none)* | Run full end-to-end demo |
| `--setup-only` | Create schema + triggers only |
| `--listen` | Start pg listener that forwards to Redis |
| `--simulate` | Insert/update/delete demo data |
| `--subscribe` | Subscribe to Redis channel and print events |
| `--cleanup` | Drop all triggers, tables, and Redis data |
| `PG_URL=...` | Custom PostgreSQL connection string |
| `REDIS_URL=...` | Custom Redis connection string |

## Trigger Details

### `fn_demo_orders_audit` — Audit Logging

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

Fires `AFTER INSERT OR UPDATE OR DELETE` and captures before/after row state as JSONB.

### `fn_demo_orders_notify` — Real-time NOTIFY

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

Builds a JSON payload and sends it via `pg_notify()` to the `demo_events` channel.

## Running Components Separately

In production, you'd run the listener and subscriber as separate services:

```bash
# Terminal 1 — Redis subscriber (downstream service)
python pg_triggers_redis.py --subscribe

# Terminal 2 — PostgreSQL listener (event bridge)
python pg_triggers_redis.py --listen

# Terminal 3 — Simulate changes (any number of times)
python pg_triggers_redis.py --simulate
```

## Real-World Use Cases

- **Change Data Capture (CDC)** — Track all changes to critical tables for auditing or replication
- **Real-time dashboards** — Push order/delivery updates to frontend via WebSocket
- **Cache invalidation** — Invalidate Redis/Memcached caches when underlying data changes
- **Event-driven microservices** — Trigger downstream workflows (invoicing, shipping, notifications)
- **Search index sync** — Update Elasticsearch/Meilisearch when records change

## Project Structure

```
sqlalchemy_demo/
├── demo.py                # 12 performance anti-pattern scenarios
├── pg_triggers_redis.py   # PostgreSQL triggers + Redis pub/sub demo
└── README.md              # This file
```

---

## SSE (Server-Sent Events) — Live Browser Updates

The `--serve` flag starts a **FastAPI server** with **two SSE implementations** that push `pg_notify` events to web browsers in real-time:

| Endpoint | Implementation | Dependency |
|----------|---------------|------------|
| `GET /events` | `StreamingResponse` (raw SSE protocol) | `fastapi` + `uvicorn` only |
| `GET /events-starlette` | `EventSourceResponse` (sse-starlette) | `fastapi` + `uvicorn` + `sse-starlette` |
| `GET /` | HTML dashboard with live event display | (same as above) |
| `GET /health` | JSON health check | (same as above) |

### Quick Start

```bash
# 1. Install SSE dependencies
pip install fastapi uvicorn sse-starlette

# 2. Make sure setup is done (triggers installed)
python pg_triggers_redis.py --setup-only

# 3. Start the SSE server
python pg_triggers_redis.py --serve
```

Then open **http://localhost:8765/** in your browser to see the live dashboard.

### Trigger events in another terminal:

```bash
python pg_triggers_redis.py --simulate
```

Each INSERT/UPDATE/DELETE will appear in the browser dashboard in real-time.

### Architecture

```
┌──────────────┐   NOTIFY 'demo_events'   ┌──────────────────┐
│  PostgreSQL   │ ───────────────────────→ │  FastAPI Server   │
│  (trigger fn) │                          │  (background      │
│  on INSERT/   │                          │   LISTEN thread)  │
│   UPDATE/     │                          │                   │
│   DELETE      │                          │  ┌─────────────┐  │
└──────────────┘                           │  │ asyncio.Queue │  │
                                           │  └──────┬──────┘  │
                                           │         │         │
                                           │  ┌──────┴──────┐  │
                                           │  │  /events     │  │
                                           │  │ Streaming    │──│──→ Browser A
                                           │  │ Response     │  │
                                           │  ├──────────────┤  │
                                           │  │ /events-     │  │
                                           │  │ starlette    │──│──→ Browser B
                                           │  │ EventSource  │  │
                                           │  │ Response     │  │
                                           │  ├──────────────┤  │
                                           │  │ / (dashboard)│──│──→ Browser C
                                           │  │ HTML + JS    │  │
                                           └──────────────────┘
```

### Two SSE Implementations

#### Approach 1: `StreamingResponse` (pure FastAPI — no extra dependencies)

```python
from fastapi.responses import StreamingResponse

@app.get("/events")
async def sse_events(request: Request):
    async def event_stream():
        while True:
            if await request.is_disconnected():
                break
            data = await async_queue.get()
            yield f"event: pg_notify\n"
            yield f"data: {json.dumps(data)}\n"
            yield "\n"  # blank line = message delimiter

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
```

The SSE protocol is **plain HTTP**. Each message is:

```
event: pg_notify
data: {"action": "INSERT", "id": 1, "summary": "..."}

```

A blank line (`\n`) separates messages. The `StreamingResponse` keeps the connection open and streams data as it becomes available.

#### Approach 2: `EventSourceResponse` (sse-starlette)

```python
from sse_starlette.sse import EventSourceResponse

@app.get("/events-starlette")
async def sse_events(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            data = await async_queue.get()
            yield {
                "event": "pg_notify",
                "data": json.dumps(data),
            }

    return EventSourceResponse(event_generator())
```

`EventSourceResponse` is a higher-level abstraction that handles:
- Proper `Last-Event-ID` tracking for reconnection
- Automatic ping/heartbeat intervals
- Connection state management
- Graceful cleanup on disconnect

### Browser Client (JavaScript)

```javascript
const evtSource = new EventSource('/events');

evtSource.addEventListener('pg_notify', (e) => {
    const data = JSON.parse(e.data);
    console.log(`${data.action} #${data.id}: ${data.summary}`);
});

evtSource.onerror = () => {
    console.log('Disconnected — browser auto-reconnects');
};
```

The browser `EventSource` API **auto-reconnects** on connection loss, sending the last received event ID so the server can resume from where it left off.

### Run It

```bash
# Terminal 1 — SSE server
python pg_triggers_redis.py --serve

# Terminal 2 — Trigger events
python pg_triggers_redis.py --simulate
```

Open http://localhost:8765/ for the live dashboard, or connect directly to `/events` or `/events-starlette` with any SSE client.

## Project Structure

```
sqlalchemy_demo/
├── demo.py                # 12 performance anti-pattern scenarios
├── pg_triggers_redis.py   # PostgreSQL triggers + Redis pub/sub + SSE
└── README.md              # This file
```
