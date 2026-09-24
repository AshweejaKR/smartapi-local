"""Small SQLite-backed temporary SmartAPI fault simulator."""
import logging
import os
import threading
import time

from common import connect, fail


CURRENT = None
LOCK = threading.Lock()
LOGGER = logging.getLogger("smartapi.faults")
LOGGER.setLevel(logging.INFO)

MODE_ALIASES = {
    "api_unavailable": "unavailable", "unavailable": "unavailable",
    "slow": "slow", "timeout": "timeout", "http500": "500", "500": "500",
    "http_500": "500", "http503": "503", "503": "503", "http_503": "503",
    "auth": "auth", "token": "auth", "token_failure": "auth",
}


def init_faults():
    global CURRENT
    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fault_state ("
            "id INTEGER PRIMARY KEY CHECK(id = 1), mode TEXT NOT NULL DEFAULT '', "
            "started_at REAL, ends_at REAL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fault_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, "
            "started_at REAL NOT NULL, ended_at REAL)"
        )
        conn.execute("INSERT OR IGNORE INTO fault_state(id, mode) VALUES (1, '')")
        row = _state(conn)
        now = time.time()
        if row and row["mode"] and row["ends_at"] > now:
            CURRENT = dict(row)
        else:
            if row and row["mode"]:
                _finish(conn, row, now, "expired")
            CURRENT = None


def normalize_mode(mode):
    return MODE_ALIASES.get(str(mode or "").strip().lower())


def slow_delay_seconds():
    try:
        return max(0.0, float(os.getenv("SMARTAPI_SLOW_DELAY_MS", "250")) / 1000)
    except ValueError:
        return 0.25


def _finish(conn, row, ended_at, reason):
    conn.execute("UPDATE fault_state SET mode='', started_at=NULL, ends_at=NULL WHERE id=1")
    conn.execute(
        "UPDATE fault_events SET ended_at=? WHERE id=? AND ended_at IS NULL",
        (ended_at, row["event_id"]),
    )
    LOGGER.info(
        "fault end mode=%s started_at=%.3f ended_at=%.3f reason=%s",
        row["mode"], row["started_at"], ended_at, reason,
    )


def _state(conn):
    return conn.execute(
        "SELECT fault_state.mode, fault_state.started_at, fault_state.ends_at, "
        "(SELECT id FROM fault_events WHERE mode=fault_state.mode "
        "AND started_at=fault_state.started_at ORDER BY id DESC LIMIT 1) AS event_id "
        "FROM fault_state WHERE id=1"
    ).fetchone()


def active_fault():
    global CURRENT
    now = time.time()
    with LOCK:
        if not CURRENT:
            return None
        if CURRENT["ends_at"] <= now:
            with connect() as conn:
                _finish(conn, CURRENT, now, "expired")
            CURRENT = None
            return None
        return dict(CURRENT)


def start_fault(mode, duration):
    global CURRENT
    mode = normalize_mode(mode)
    duration = float(duration)
    if mode is None or duration <= 0 or duration != duration:
        raise ValueError("invalid fault mode or duration")
    now = time.time()
    with LOCK, connect() as conn:
        if CURRENT:
            _finish(conn, CURRENT, now, "replaced")
        ends_at = now + duration
        conn.execute(
            "UPDATE fault_state SET mode=?, started_at=?, ends_at=? WHERE id=1",
            (mode, now, ends_at),
        )
        event_id = conn.execute(
            "INSERT INTO fault_events(mode, started_at) VALUES (?, ?)", (mode, now)
        ).lastrowid
        CURRENT = {"mode": mode, "started_at": now, "ends_at": ends_at, "event_id": event_id}
        LOGGER.info("fault start mode=%s started_at=%.3f ends_at=%.3f", mode, now, ends_at)
    return dict(CURRENT)


def clear_fault():
    global CURRENT
    with LOCK:
        if CURRENT:
            with connect() as conn:
                _finish(conn, CURRENT, time.time(), "cleared")
            CURRENT = None


FAULT_RESPONSES = {
    "auth": ("Invalid or expired token", "AG8001", 401),
    "500": ("Internal server error", "AB1000", 500),
    "timeout": ("Request timed out", "AB1000", 504),
}


def fault_response(mode):
    return fail(*FAULT_RESPONSES.get(mode, ("Service unavailable", "AB1000", 503)))
