"""Shared SQLite connection, audit rows and SmartAPI response envelopes."""
from datetime import datetime
from pathlib import Path
import sqlite3

from fastapi.responses import JSONResponse


DB_PATH = None
OWNED_TABLES = ("orders", "trades", "positions", "holdings", "gtt_rules")


def init_common(path):
    global DB_PATH
    DB_PATH = Path(path)


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def record_audit(conn, action, detail, client_code=""):
    """Callers pass redacted details only: never headers, bodies or tokens."""
    conn.execute(
        "INSERT INTO audit_log(created_at, action, detail, client_code) VALUES (?, ?, ?, ?)",
        (datetime.now().isoformat(timespec="seconds"), action, detail, client_code),
    )


def delete_client(conn, client_code):
    """Delete a client, its sessions, balance and all owned activity."""
    conn.execute(
        "DELETE FROM order_events WHERE order_id IN "
        "(SELECT order_id FROM orders WHERE client_code=?)", (client_code,)
    )
    for table in ("sessions", "accounts", *OWNED_TABLES, "users"):
        conn.execute(f"DELETE FROM {table} WHERE client_code=?", (client_code,))


def ok(data=None, message="SUCCESS"):
    return {"status": True, "message": message, "errorcode": "", "data": data}


def fail(message, errorcode="AB1004", status_code=400, data=None):
    return JSONResponse(
        status_code=status_code,
        content={"status": False, "message": message, "errorcode": errorcode, "data": data},
    )


def failed(message, status_code=401, data=None):
    """SmartAPI authentication failure envelope."""
    return fail(message, "AG8001", status_code, data)
