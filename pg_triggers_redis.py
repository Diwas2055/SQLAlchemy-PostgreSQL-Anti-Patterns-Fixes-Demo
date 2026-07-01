"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  PostgreSQL Triggers + Redis Pub/Sub Demo                                   ║
║  ───────────────────────────────────────────────────────                    ║
║  Demonstrates:                                                              ║
║                                                                             ║
║  1. PostgreSQL trigger functions (PL/pgSQL) that auto-log changes          ║
║     and send NOTIFY events when data is inserted/updated/deleted            ║
║                                                                             ║
║  2. PostgreSQL LISTEN/NOTIFY — a Python listener that receives              ║
║     real-time notifications from the database                               ║
║                                                                             ║
║  3. Redis Pub/Sub bridge — forwarding database events to Redis              ║
║     channels for other services to consume                                  ║
║                                                                             ║
║  4. A complete event-driven pipeline: DB trigger → pg_notify →             ║
║     Python listener → Redis channel → subscriber                            ║
║                                                                             ║
║  5. SSE (Server-Sent Events) endpoint — push pg_notify events to           ║
║     web browsers in real-time over HTTP                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝

REQUIREMENTS:
    pip install psycopg2-binary redis

    # For SSE (Server-Sent Events) mode:
    pip install fastapi uvicorn sse-starlette

    # Redis server must be running on localhost:6379 (default)
    # or set REDIS_URL environment variable

PREREQUISITE — PostgreSQL database:
    createdb sqlalchemy_demo
    # or: psql -c "CREATE DATABASE sqlalchemy_demo;"

USAGE:
    # Full demo: setup triggers, run listener, simulate changes, show Redis flow
    python pg_triggers_redis.py

    # Just setup the schema and triggers, then exit
    python pg_triggers_redis.py --setup-only

    # Run the listener (listens to pg NOTIFY and forwards to Redis)
    python pg_triggers_redis.py --listen

    # Simulate changes (insert/update/delete rows to trigger notifications)
    python pg_triggers_redis.py --simulate

    # Subscribe to Redis channel and print received events
    python pg_triggers_redis.py --subscribe

    # Clean up triggers, tables, and Redis data
    python pg_triggers_redis.py --cleanup

    # Start SSE server (pushes pg events to browsers via HTTP SSE)
    python pg_triggers_redis.py --serve

ARCHITECTURE:
    ┌──────────────┐    NOTIFY 'channel'    ┌──────────────────┐
    │  PostgreSQL   │ ──────────────────→   │  Python Listener  │
    │  (trigger fn) │                       │  (LISTEN/channel) │
    │  on INSERT/   │                       └────────┬─────────┘
    │   UPDATE/     │                                │ PUBLISH to
    │   DELETE      │                                │ Redis channel
    └──────────────┘                        ┌────────┴─────────┐
                                            │    Redis Pub/Sub  │
                                            │   (event bus)     │
                                            └────────┬─────────┘
                                                     │ SUBSCRIBE
                                            ┌────────┴─────────┐
                                            │  Other services   │
                                            │ (subscribers)     │
                                            └──────────────────┘
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

PG_URL = os.environ.get(
    "PG_URL",
    "postgresql://postgres:postgres@localhost:5432/sqlalchemy_demo",
)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# PostgreSQL channel name for LISTEN/NOTIFY
PG_CHANNEL = "demo_events"

# Redis channel name for forwarded events
REDIS_CHANNEL = "pg:demo_events"

# ──────────────────────────────────────────────────────────────────────────────
# PostgreSQL Schema (created via raw DDL for trigger control)
# ──────────────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- 1. Main table: tracks orders (or any entity we want to monitor)
CREATE TABLE IF NOT EXISTS demo_orders (
    id          SERIAL PRIMARY KEY,
    customer    TEXT NOT NULL,
    product     TEXT NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Audit log table: captures every change automatically via trigger
CREATE TABLE IF NOT EXISTS demo_orders_audit (
    id          SERIAL PRIMARY KEY,
    order_id    INTEGER NOT NULL,
    action      TEXT NOT NULL,          -- INSERT / UPDATE / DELETE
    old_data    JSONB,                  -- previous row state (NULL on INSERT)
    new_data    JSONB,                  -- new row state (NULL on DELETE)
    changed_by  TEXT NOT NULL DEFAULT 'app',
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Index on audit table for faster lookups
CREATE INDEX IF NOT EXISTS ix_demo_orders_audit_order_id
    ON demo_orders_audit (order_id);
"""

# ──────────────────────────────────────────────────────────────────────────────
# Trigger Functions (PL/pgSQL)
# ──────────────────────────────────────────────────────────────────────────────

TRIGGER_FUNCTIONS_SQL = """
-- ============================================================
-- Trigger function 1: Audit logging
-- Logs every INSERT/UPDATE/DELETE on demo_orders into
-- demo_orders_audit, capturing old and new row data as JSONB.
-- ============================================================
CREATE OR REPLACE FUNCTION fn_demo_orders_audit()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO demo_orders_audit (order_id, action, new_data)
        VALUES (NEW.id, 'INSERT', row_to_json(NEW)::jsonb);
        RETURN NEW;

    ELSIF TG_OP = 'UPDATE' THEN
        INSERT INTO demo_orders_audit (order_id, action, old_data, new_data)
        VALUES (
            NEW.id,
            'UPDATE',
            row_to_json(OLD)::jsonb,
            row_to_json(NEW)::jsonb
        );
        RETURN NEW;

    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO demo_orders_audit (order_id, action, old_data)
        VALUES (OLD.id, 'DELETE', row_to_json(OLD)::jsonb);
        RETURN OLD;
    END IF;
END;
$$ LANGUAGE plpgsql;


-- ============================================================
-- Trigger function 2: NOTIFY on change
-- Sends a PostgreSQL NOTIFY event every time a row is
-- inserted, updated, or deleted on demo_orders.
--
-- The payload is a JSON string with:
--   - table:   the table name
--   - action:  INSERT / UPDATE / DELETE
--   - id:      the affected row's PK
--   - time:    ISO-8601 timestamp
--   - summary: human-readable summary of the change
-- ============================================================
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
        'summary', CASE TG_OP
            WHEN 'INSERT' THEN
                'Order #' || NEW.id || ': ' || NEW.customer ||
                ' ordered ' || NEW.quantity || 'x ' || NEW.product
            WHEN 'UPDATE' THEN
                'Order #' || NEW.id || ': status changed from "' ||
                OLD.status || '" to "' || NEW.status || '"'
            WHEN 'DELETE' THEN
                'Order #' || OLD.id || ': deleted (' ||
                OLD.customer || ', ' || OLD.quantity || 'x ' ||
                OLD.product || ')'
        END
    )::text;

    -- Send the notification on our channel
    PERFORM pg_notify('""" + PG_CHANNEL + """', payload);
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
"""

# ──────────────────────────────────────────────────────────────────────────────
# Trigger Installation
# ──────────────────────────────────────────────────────────────────────────────

INSTALL_TRIGGERS_SQL = """
-- Drop existing triggers first (idempotent)
DROP TRIGGER IF EXISTS trg_demo_orders_audit    ON demo_orders;
DROP TRIGGER IF EXISTS trg_demo_orders_notify   ON demo_orders;

-- Install audit trigger (fires AFTER the operation)
CREATE TRIGGER trg_demo_orders_audit
    AFTER INSERT OR UPDATE OR DELETE ON demo_orders
    FOR EACH ROW
    EXECUTE FUNCTION fn_demo_orders_audit();

-- Install notify trigger (fires AFTER the operation)
CREATE TRIGGER trg_demo_orders_notify
    AFTER INSERT OR UPDATE OR DELETE ON demo_orders
    FOR EACH ROW
    EXECUTE FUNCTION fn_demo_orders_notify();
"""

# ──────────────────────────────────────────────────────────────────────────────
# Cleanup
# ──────────────────────────────────────────────────────────────────────────────

CLEANUP_SQL = """
DROP TRIGGER IF EXISTS trg_demo_orders_audit    ON demo_orders;
DROP TRIGGER IF EXISTS trg_demo_orders_notify   ON demo_orders;
DROP FUNCTION IF EXISTS fn_demo_orders_audit;
DROP FUNCTION IF EXISTS fn_demo_orders_notify;
DROP TABLE IF EXISTS demo_orders_audit;
DROP TABLE IF EXISTS demo_orders;
"""


# ══════════════════════════════════════════════════════════════════════════════
# Python Implementation
# ══════════════════════════════════════════════════════════════════════════════

def get_pg_conn() -> Any:
    """Create a raw psycopg2 connection (not SQLAlchemy) for LISTEN/NOTIFY."""
    import psycopg2
    return psycopg2.connect(PG_URL)


def get_redis() -> Any:
    """Create a Redis connection."""
    import redis
    return redis.from_url(REDIS_URL)


# ──────────────────────────────────────────────────────────────────────────────
# Setup / Teardown
# ──────────────────────────────────────────────────────────────────────────────

def setup(conn: Any) -> None:
    """Create schema, trigger functions, and install triggers."""
    cur = conn.cursor()
    print("📦  Creating schema...")
    cur.execute(SCHEMA_SQL)
    print("📦  Creating trigger functions...")
    cur.execute(TRIGGER_FUNCTIONS_SQL)
    print("📦  Installing triggers...")
    cur.execute(INSTALL_TRIGGERS_SQL)
    conn.commit()
    print("  ✅  Schema + triggers ready\n")


def cleanup(conn: Any) -> None:
    """Remove all triggers, functions, and tables."""
    cur = conn.cursor()
    print("🧹  Cleaning up...")
    cur.execute(CLEANUP_SQL)
    conn.commit()
    print("  ✅  Cleanup complete\n")

    # Also clean Redis
    try:
        r = get_redis()
        r.delete(REDIS_CHANNEL)
        print("  ✅  Redis channel cleaned\n")
    except Exception:
        pass  # Redis might not be running


# ──────────────────────────────────────────────────────────────────────────────
# Simulate Changes
# ──────────────────────────────────────────────────────────────────────────────

SAMPLE_PRODUCTS = [
    ("Alice",   "Widget Alpha",  2),
    ("Bob",     "Gadget Beta",   1),
    ("Charlie", "Widget Alpha",  5),
    ("Diana",   "Gadget Beta",   3),
    ("Eve",     "Doohickey Gamma", 10),
]

STATUS_TRANSITIONS = ["pending", "confirmed", "shipped", "delivered"]


def simulate(conn: Any) -> None:
    """Insert, update, and delete rows to trigger notifications."""
    cur = conn.cursor()

    print("📦  Simulating changes on demo_orders...\n")

    # ── INSERTS ──────────────────────────────────────────────────────────
    print("  ──  INSERT 5 orders ──")
    order_ids: list[int] = []
    for customer, product, qty in SAMPLE_PRODUCTS:
        cur.execute(
            "INSERT INTO demo_orders (customer, product, quantity) "
            "VALUES (%s, %s, %s) RETURNING id",
            (customer, product, qty),
        )
        oid = cur.fetchone()[0]
        order_ids.append(oid)
        print(f"     ✅  Order #{oid}: {customer} ordered {qty}x {product}")
    conn.commit()

    print()

    # ── UPDATES ─────────────────────────────────────────────────────────
    print("  ──  UPDATE statuses ──")
    for idx, oid in enumerate(order_ids):
        new_status = STATUS_TRANSITIONS[(idx + 1) % len(STATUS_TRANSITIONS)]
        cur.execute(
            "UPDATE demo_orders SET status = %s, updated_at = NOW() "
            "WHERE id = %s",
            (new_status, oid),
        )
        print(f"     🔄  Order #{oid}: status → '{new_status}'")
    conn.commit()

    print()

    # ── DELETE ──────────────────────────────────────────────────────────
    print("  ──  DELETE 1 order ──")
    deleted_id = order_ids[-1]
    cur.execute("DELETE FROM demo_orders WHERE id = %s", (deleted_id,))
    print(f"     🗑️  Order #{deleted_id}: deleted")
    conn.commit()

    print()

    # ── Show audit trail ────────────────────────────────────────────────
    print("  ──  Audit trail (demo_orders_audit) ──")
    cur.execute(
        "SELECT order_id, action, changed_at FROM demo_orders_audit "
        "ORDER BY id"
    )
    for row in cur.fetchall():
        print(f"     📝  Order #{row[0]}: {row[1]} at {row[2].strftime('%H:%M:%S.%f')[:-3]}")

    print(f"\n  ✅  {len(order_ids) + 1} events generated — check the listener!\n")


# ──────────────────────────────────────────────────────────────────────────────
# PostgreSQL LISTEN + Redis PUBLISH
# ──────────────────────────────────────────────────────────────────────────────

def listen_and_forward() -> None:
    """Listen on PostgreSQL NOTIFY channel and forward events to Redis.

    This is the bridge: pg_notify → Python → Redis Pub/Sub.
    Uses psycopg2's asynchronous connection for non-blocking LISTEN.
    """
    import select as _select  # avoid shadowing

    print(f"👂  Listening on PostgreSQL channel: '{PG_CHANNEL}'")
    print(f"📤  Forwarding to Redis channel:     '{REDIS_CHANNEL}'")
    print("     Press Ctrl+C to stop\n")

    conn = get_pg_conn()
    conn.set_isolation_level(0)  # AUTOCOMMIT — required for LISTEN

    cur = conn.cursor()
    cur.execute(f"LISTEN {PG_CHANNEL}")
    cur.close()

    print(f"  ✅  LISTEN {PG_CHANNEL} registered")
    print("  ⏳  Waiting for notifications...\n")

    redis_ok = False
    try:
        r = get_redis()
        r.ping()
        redis_ok = True
        print(f"  ✅  Connected to Redis: {REDIS_URL}")
    except Exception as e:
        print(f"  ⚠️  Redis not available ({e}). Events will only be printed.\n")

    event_count = 0
    try:
        while True:
            if _select.select([conn], [], [], 1.0) == ([], [], []):
                continue  # timeout — loop back for clean signal handling

            conn.poll()
            while conn.notifies:
                notify = conn.notifies.pop(0)
                event_count += 1
                payload = notify.payload
                timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]

                print(f"  [{timestamp}] 📩  pg_notify received:")
                print(f"         channel: {notify.channel}")
                print(f"         pid:     {notify.pid}")
                print(f"         payload: {payload}")
                print()

                # Forward to Redis
                if redis_ok:
                    try:
                        r.publish(REDIS_CHANNEL, payload)
                        print(f"         📤  Forwarded to Redis channel '{REDIS_CHANNEL}'")
                    except Exception as e:
                        print(f"         ❌  Redis publish failed: {e}")
                print()

    except KeyboardInterrupt:
        print(f"\n  👋  Stopped. Forwarded {event_count} events.")
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Redis Subscriber
# ──────────────────────────────────────────────────────────────────────────────

def subscribe_redis() -> None:
    """Subscribe to the Redis channel and print received events.

    This simulates a downstream service consuming events from the Redis
    event bus (e.g., a notification service, analytics pipeline, etc.).
    """
    try:
        r = get_redis()
        r.ping()
    except Exception as e:
        print(f"❌  Cannot connect to Redis: {e}")
        print("    Make sure Redis is running:")
        print("    $ redis-server")
        sys.exit(1)

    pubsub = r.pubsub()
    pubsub.subscribe(REDIS_CHANNEL)

    print(f"📡  Subscribed to Redis channel: '{REDIS_CHANNEL}'")
    print("     Waiting for events... (Ctrl+C to stop)\n")

    try:
        for message in pubsub.listen():
            if message["type"] == "message":
                timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                data = json.loads(message["data"])
                print(f"  [{timestamp}] 📬  Redis event received:")
                print(f"         channel: {message['channel'].decode()}")
                print(f"         action:  {data.get('action', '?')}")
                print(f"         table:   {data.get('table', '?')}")
                print(f"         id:      {data.get('id', '?')}")
                print(f"         summary: {data.get('summary', '?')}")
                print()
    except KeyboardInterrupt:
        print("\n  👋  Unsubscribed.")
        pubsub.unsubscribe()
        r.close()

# ──────────────────────────────────────────────────────────────────────────────
# SSE Server (FastAPI + Server-Sent Events)
# ──────────────────────────────────────────────────────────────────────────────

def serve_sse() -> None:
    """
    Start an HTTP server with an SSE endpoint that streams pg_notify events
    to connected web browsers in real-time.

    Endpoints:
      GET /events  → SSE stream of pg_notify events
      GET /health  → JSON health check
      GET /        → HTML dashboard with live event display

    Architecture:
      pg_notify → asyncio.Queue → SSE /events → browser EventSource
    """
    try:
        import fastapi
        import uvicorn
        import sse_starlette
    except ImportError:
        print("❌  Missing dependencies for SSE server.")
        print("    Install them with:")
        print("    pip install fastapi uvicorn sse-starlette")
        sys.exit(1)

    import asyncio
    import select as _select
    import threading
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, HTMLResponse
    from sse_starlette.sse import EventSourceResponse

    app = FastAPI(title="PostgreSQL NOTIFY → SSE Bridge")

    # ── Shared asyncio event queue ───────────────────────────────────────
    # Bridging from psycopg2 (sync, thread-based) to asyncio (SSE).
    # We use a thread-safe queue: pg listener thread → sync Queue → asyncio Queue
    import queue as _queue
    sync_queue: _queue.Queue = _queue.Queue(maxsize=1000)  # Bounded — prevents memory exhaustion
    async_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)  # Bounded — drops oldest if burst exceeds 1000

    # ── PG listener thread ───────────────────────────────────────────────
    def pg_listener_thread() -> None:
        """Background thread: LISTEN on pg channel, push events to sync queue."""
        try:
            conn = get_pg_conn()
            conn.set_isolation_level(0)
            cur = conn.cursor()
            cur.execute(f"LISTEN {PG_CHANNEL}")
            cur.close()
            print(f"  👂  LISTEN {PG_CHANNEL} registered (thread: {threading.current_thread().name})")
        except Exception as e:
            print(f"  ❌  PG listener failed: {e}")
            return

        while True:
            try:
                if _select.select([conn], [], [], 1.0) == ([], [], []):
                    continue
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    data = json.loads(notify.payload)
                    data["_meta"] = {
                        "channel": notify.channel,
                        "pid": notify.pid,
                        "received_at": datetime.now(timezone.utc).isoformat(),
                    }
                    sync_queue.put(data)
            except Exception:
                break

    listener_thread = threading.Thread(target=pg_listener_thread, daemon=True)
    listener_thread.start()

    # ── Bridge: sync queue → asyncio queue ───────────────────────────────
    async def bridge_queues() -> None:
        """Async task: move events from sync queue to asyncio queue."""
        loop = asyncio.get_event_loop()
        dropped = 0
        while True:
            data = await loop.run_in_executor(None, sync_queue.get)
            try:
                async_queue.put_nowait(data)
            except asyncio.QueueFull:
                # Queue full — discard oldest to keep recent events
                try:
                    async_queue.get_nowait()  # drop oldest
                except asyncio.QueueEmpty:
                    pass
                async_queue.put_nowait(data)
                dropped += 1
                if dropped == 1 or dropped % 100 == 0:
                    print(f"  ⚠️  Queue full — dropped {dropped} event(s)")

    @app.on_event("startup")
    async def startup() -> None:
        asyncio.create_task(bridge_queues())
        print("  ✅  SSE server ready")

    # ── SSE endpoint: GET /events (StreamingResponse — pure FastAPI, no extra deps) ───
    @app.get("/events")
    async def sse_events_streaming(request: Request):
        """SSE endpoint using StreamingResponse (pure FastAPI, no sse-starlette needed).

        SSE format is plain HTTP with:
          - Content-Type: text/event-stream
          - Cache-Control: no-cache
          - Connection: keep-alive

        Each message follows the SSE protocol:
          event: <event_type>\n
          data: <json_payload>\n
          \n
        """
        client_host = request.client.host if request.client else "unknown"
        print(f"  🌐  SSE client connected (StreamingResponse): {client_host}")

        async def event_stream():
            try:
                while True:
                    if await request.is_disconnected():
                        print(f"  🌐  SSE client disconnected: {client_host}")
                        break
                    try:
                        data = await asyncio.wait_for(
                            async_queue.get(), timeout=30.0
                        )
                        # SSE protocol: event + data lines, terminated by blank line
                        yield f"event: pg_notify\n"
                        yield f"data: {json.dumps(data)}\n"
                        yield "\n"
                    except asyncio.TimeoutError:
                        # Heartbeat keeps the connection alive
                        yield f"event: heartbeat\n"
                        yield "data: \n"
                        yield "\n"
            except asyncio.CancelledError:
                pass

        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    # ── SSE endpoint: GET /events-starlette (EventSourceResponse — sse-starlette) ──
    @app.get("/events-starlette")
    async def sse_events_starlette(request: Request):
        """SSE endpoint using sse-starlette's EventSourceResponse.

        This provides a higher-level abstraction with built-in:
          - Reconnection handling (last-event-id)
          - Proper ping/heartbeat intervals
          - Connection state management
        """
        client_host = request.client.host if request.client else "unknown"
        print(f"  🌐  SSE client connected (EventSourceResponse): {client_host}")

        async def event_generator():
            try:
                while True:
                    if await request.is_disconnected():
                        print(f"  🌐  SSE client disconnected: {client_host}")
                        break
                    try:
                        data = await asyncio.wait_for(
                            async_queue.get(), timeout=30.0
                        )
                        yield {
                            "event": "pg_notify",
                            "data": json.dumps(data),
                        }
                    except asyncio.TimeoutError:
                        yield {"event": "heartbeat", "data": ""}
            except asyncio.CancelledError:
                pass

        return EventSourceResponse(event_generator())

    # ── Health check ─────────────────────────────────────────────────────
    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "channel": PG_CHANNEL,
            "listener_alive": listener_thread.is_alive(),
            "queue_size": async_queue.qsize(),
        })

    # ── HTML dashboard ───────────────────────────────────────────────────
    @app.get("/")
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PostgreSQL NOTIFY → SSE Live Dashboard</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #0d1117; color: #c9d1d9; padding: 2rem; }}
        h1 {{ color: #58a6ff; margin-bottom: 0.5rem; }}
        .subtitle {{ color: #8b949e; margin-bottom: 2rem; }}
        .stats {{ display: flex; gap: 1rem; margin-bottom: 2rem; }}
        .stat {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px;
                 padding: 1rem 1.5rem; }}
        .stat-label {{ font-size: 0.8rem; color: #8b949e; text-transform: uppercase; }}
        .stat-value {{ font-size: 1.5rem; font-weight: bold; color: #58a6ff; }}
        #events {{ list-style: none; }}
        .event {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px;
                  padding: 1rem; margin-bottom: 0.5rem;
                  animation: slideIn 0.3s ease-out; }}
        @keyframes slideIn {{ from {{ opacity: 0; transform: translateY(-10px); }}
                              to {{ opacity: 1; transform: translateY(0); }} }}
        .event-time {{ font-size: 0.8rem; color: #8b949e; }}
        .event-action {{ display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px;
                        font-size: 0.75rem; font-weight: bold; text-transform: uppercase; }}
        .action-INSERT {{ background: #238636; color: #fff; }}
        .action-UPDATE {{ background: #1f6feb; color: #fff; }}
        .action-DELETE {{ background: #da3633; color: #fff; }}
        .event-id {{ color: #f0883e; }}
        .event-summary {{ margin-top: 0.3rem; color: #c9d1d9; }}
        .status-connected {{ color: #3fb950; }}
        .status-disconnected {{ color: #da3633; }}
        #connection-status {{ margin-bottom: 1rem; padding: 0.5rem 1rem;
                             border-radius: 6px; background: #161b22;
                             border: 1px solid #30363d; display: inline-block; }}
    </style>
</head>
<body>
    <h1>📡 PostgreSQL → SSE Live Dashboard</h1>
    <p class="subtitle">Listening on channel: <code>{PG_CHANNEL}</code></p>
    <p class="subtitle" style="font-size:0.85rem; margin-top:0.25rem;">
        <a href="/" style="color:#58a6ff;">SSE (StreamingResponse)</a>
        &nbsp;|&nbsp;
        <a href="/?starlette=1" style="color:#58a6ff;">SSE (EventSourceResponse)</a>
    </p>

    <div class="stats">
        <div class="stat">
            <div class="stat-label">Events Received</div>
            <div class="stat-value" id="event-count">0</div>
        </div>
        <div class="stat">
            <div class="stat-label">Connection</div>
            <div class="stat-value" id="connection-status-dot">●</div>
        </div>
    </div>

    <div id="connection-status">🔴 Disconnected</div>
    <ul id="events"></ul>

    <script>
        const eventCount = document.getElementById('event-count');
        const eventsList = document.getElementById('events');
        const connStatus = document.getElementById('connection-status');
        const connDot = document.getElementById('connection-status-dot');
        let count = 0;

        // Choose SSE backend via URL param: ?starlette=1 uses EventSourceResponse
        const params = new URLSearchParams(window.location.search);
        const eventUrl = params.get('starlette') === '1' ? '/events-starlette' : '/events';
        const evtSource = new EventSource(eventUrl);

        evtSource.addEventListener('pg_notify', (e) => {{
            const data = JSON.parse(e.data);
            count++;
            eventCount.textContent = count;

            const li = document.createElement('li');
            li.className = 'event';
            li.innerHTML = `
                <span class="event-time">${{data._meta?.received_at?.slice(11, 23) || '??'}}</span>
                <span class="event-action action-${{data.action}}">${{data.action}}</span>
                <span class="event-id">#${{data.id}}</span>
                <div class="event-summary">${{data.summary || ''}}</div>
            `;
            eventsList.prepend(li);

            // Keep max 50 events
            while (eventsList.children.length > 50) {{
                eventsList.removeChild(eventsList.lastChild);
            }}
        }});

        evtSource.onopen = () => {{
            connStatus.textContent = '🟢 Connected';
            connStatus.className = 'status-connected';
            connDot.textContent = '●';
            connDot.style.color = '#3fb950';
        }};

        evtSource.onerror = () => {{
            connStatus.textContent = '🔴 Disconnected (reconnecting...)';
            connStatus.className = 'status-disconnected';
            connDot.textContent = '●';
            connDot.style.color = '#da3633';
        }};
    </script>
</body>
</html>
""")

    # ── Start server ─────────────────────────────────────────────────────
    port = int(os.environ.get("SSE_PORT", "8765"))
    host = os.environ.get("SSE_HOST", "0.0.0.0")

    print()
    print("═" * 60)
    print("  🌐  SSE Server Starting")
    print("═" * 60)
    print(f"     PG channel:  {PG_CHANNEL}")
    print(f"     Listen on:   http://{host}:{port}")
    print(f"     SSE:         http://{host}:{port}/events")
    print(f"     Dashboard:   http://{host}:{port}/")
    print(f"     Health:      http://{host}:{port}/health")
    print("     Press Ctrl+C to stop\n")

    uvicorn.run(app, host=host, port=port, log_level="info")


# ──────────────────────────────────────────────────────────────────────────────
# Full Demo Runner (updated with SSE)
# ──────────────────────────────────────────────────────────────────────────────

def run_full_demo_with_sse() -> None:
    """
    Run the complete pipeline including SSE:
      1. Setup schema + triggers
      2. Start Redis subscriber in background
      3. Start PostgreSQL listener in background
      4. Start SSE server in background
      5. Simulate changes
      6. Show results
    """
    import multiprocessing
    import threading

    conn = get_pg_conn()
    setup(conn)
    conn.close()

    print("═" * 60)
    print("  🚀  Starting full pipeline demo (with SSE)...")
    print("═" * 60)
    print()

    # ── Start Redis subscriber in a daemon thread ───────────────────────
    redis_events: list[dict[str, Any]] = []
    redis_ready = threading.Event()

    def redis_collector() -> None:
        try:
            r = get_redis()
            r.ping()
        except Exception:
            redis_ready.set()
            return
        pubsub = r.pubsub()
        pubsub.subscribe(REDIS_CHANNEL)
        redis_ready.set()
        for message in pubsub.listen():
            if message["type"] == "message":
                data = json.loads(message["data"])
                redis_events.append(data)

    sub_thread = threading.Thread(target=redis_collector, daemon=True)
    sub_thread.start()
    redis_ready.wait(timeout=3)

    # ── Start pg listener in a separate process ─────────────────────────
    listener_proc: multiprocessing.Process | None = None

    def start_listener() -> None:
        nonlocal listener_proc
        listener_proc = multiprocessing.Process(
            target=listen_and_forward, daemon=True
        )
        listener_proc.start()
        time.sleep(0.5)

    start_listener()

    # ── Start SSE server in a separate process ──────────────────────────
    sse_proc: multiprocessing.Process | None = None

    def start_sse() -> None:
        nonlocal sse_proc
        try:
            import fastapi  # noqa: F401
        except ImportError:
            print("  ⚠️  SSE dependencies not installed, skipping SSE server")
            return
        sse_proc = multiprocessing.Process(
            target=serve_sse, daemon=True
        )
        sse_proc.start()
        time.sleep(1.5)  # Wait for server to start

    start_sse()

    # ── Simulate changes ────────────────────────────────────────────────
    conn_sim = get_pg_conn()
    simulate(conn_sim)
    conn_sim.close()

    print("  ⏳  Waiting for events to propagate...")
    time.sleep(2)

    # ── Stop processes ──────────────────────────────────────────────────
    if sse_proc and sse_proc.is_alive():
        sse_proc.terminate()
        sse_proc.join(timeout=2)

    if listener_proc and listener_proc.is_alive():
        listener_proc.terminate()
        listener_proc.join(timeout=2)

    # ── Show results ────────────────────────────────────────────────────
    print()
    print("═" * 60)
    print("  📊  Demo Results")
    print("═" * 60)
    print()
    print(f"  Redis events collected: {len(redis_events)}")
    print()
    for evt in redis_events:
        print(f"    • {evt.get('action', '?'):>8}  #{evt.get('id', '?')}  "
              f"| {evt.get('summary', '?')}")
    print()
    print("  ✅  Pipeline verified: DB trigger → pg_notify → Python → Redis + SSE")
    print()



# ──────────────────────────────────────────────────────────────────────────────
# Standalone Demo Runner
# ──────────────────────────────────────────────────────────────────────────────

def run_full_demo() -> None:
    """
    Run the complete pipeline:
      1. Setup schema + triggers
      2. Start Redis subscriber in background
      3. Start PostgreSQL listener in background
      4. Simulate changes
      5. Show audit trail
    """
    import multiprocessing
    import threading

    conn = get_pg_conn()
    setup(conn)
    conn.close()

    print("═" * 60)
    print("  🚀  Starting full pipeline demo...")
    print("═" * 60)
    print()

    # ── Start Redis subscriber in a daemon thread ───────────────────────
    redis_events: list[dict[str, Any]] = []
    redis_ready = threading.Event()

    def redis_collector() -> None:
        """Subscribe to Redis and collect events for display."""
        try:
            r = get_redis()
            r.ping()
        except Exception:
            redis_ready.set()
            return

        pubsub = r.pubsub()
        pubsub.subscribe(REDIS_CHANNEL)
        redis_ready.set()

        for message in pubsub.listen():
            if message["type"] == "message":
                data = json.loads(message["data"])
                redis_events.append(data)

    sub_thread = threading.Thread(target=redis_collector, daemon=True)
    sub_thread.start()
    redis_ready.wait(timeout=3)

    # ── Start pg listener in a separate process ─────────────────────────
    listener_proc: multiprocessing.Process | None = None

    def start_listener() -> None:
        nonlocal listener_proc
        listener_proc = multiprocessing.Process(
            target=listen_and_forward, daemon=True
        )
        listener_proc.start()
        # Give listener time to register LISTEN
        time.sleep(0.5)

    start_listener()

    # ── Simulate changes ────────────────────────────────────────────────
    conn_sim = get_pg_conn()
    simulate(conn_sim)
    conn_sim.close()

    # Wait for events to propagate
    print("  ⏳  Waiting for events to propagate...")
    time.sleep(1.5)

    # ── Stop listener ───────────────────────────────────────────────────
    if listener_proc and listener_proc.is_alive():
        listener_proc.terminate()
        listener_proc.join(timeout=2)

    # ── Show results ────────────────────────────────────────────────────
    print()
    print("═" * 60)
    print("  📊  Demo Results")
    print("═" * 60)
    print()
    print(f"  Redis events collected: {len(redis_events)}")
    print()
    for evt in redis_events:
        print(f"    • {evt.get('action', '?'):>8}  #{evt.get('id', '?')}  "
              f"| {evt.get('summary', '?')}")
    print()
    print("  ✅  Pipeline verified: DB trigger → pg_notify → "
          "Python → Redis → subscriber")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="PostgreSQL Triggers + Redis Pub/Sub Demo"
    )
    parser.add_argument("--setup-only", action="store_true",
                        help="Create schema, trigger functions, install triggers")
    parser.add_argument("--listen", action="store_true",
                        help="Listen on pg NOTIFY and forward to Redis")
    parser.add_argument("--simulate", action="store_true",
                        help="Insert/update/delete rows to trigger notifications")
    parser.add_argument("--subscribe", action="store_true",
                        help="Subscribe to Redis channel and print events")
    parser.add_argument("--cleanup", action="store_true",
                        help="Remove all triggers, functions, tables, Redis data")
    parser.add_argument("--serve", action="store_true",
                        help="Start SSE server (pushes pg events to browsers via HTTP SSE)")
    args = parser.parse_args()

    # If no args, run full demo
    full_demo = not (args.setup_only or args.listen or args.simulate
                     or args.subscribe or args.cleanup or args.serve)

    # ── Validate PostgreSQL connection ──────────────────────────────────
    try:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT version()")
        version = cur.fetchone()[0]
        print(f"🔗  Connected to PostgreSQL\n    {version}\n")
        conn.close()
    except Exception as e:
        print(f"❌  Cannot connect to PostgreSQL at:\n    {PG_URL}")
        print(f"    Error: {e}")
        print("\n    Make sure PostgreSQL is running and the database exists:")
        print("    $ createdb sqlalchemy_demo")
        sys.exit(1)

    if full_demo:
        run_full_demo_with_sse()
        return

    if args.cleanup:
        conn = get_pg_conn()
        cleanup(conn)
        conn.close()
        return

    if args.setup_only:
        conn = get_pg_conn()
        setup(conn)
        conn.close()
        print("  ✅  Setup complete. Run with --listen to start the listener.\n")
        return

    if args.listen:
        listen_and_forward()
        return

    if args.simulate:
        conn = get_pg_conn()
        simulate(conn)
        conn.close()
        return

    if args.subscribe:
        subscribe_redis()
        return

    if args.serve:
        serve_sse()
        return


if __name__ == "__main__":
    main()
