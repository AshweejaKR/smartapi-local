"""Shared SQLite connection and SmartAPI response envelopes."""
from pathlib import Path
import sqlite3

from fastapi.responses import JSONResponse


DB_PATH = None


def init_common(path):
    global DB_PATH
    DB_PATH = Path(path)


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
