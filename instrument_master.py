"""Angel One instrument master: daily cached refresh and (exchange, token) lookup.

The master is cached in the local SQLite database (gitignored). A failed
download or a malformed file never replaces the last valid cache.
"""
from datetime import datetime
import json
import threading
import urllib.request
from zoneinfo import ZoneInfo

from common import connect, record_audit


MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
IST = ZoneInfo("Asia/Kolkata")
FIELDS = ("exchange", "token", "symbol", "name", "instrumenttype", "expiry", "lotsize", "tick_size")
SOURCE_FIELDS = ("exch_seg", "token", "symbol", "name", "instrumenttype", "expiry", "lotsize", "tick_size")
REFRESH_LOCK = threading.Lock()
AFTER_REFRESH = []  # callbacks(conn) run inside the refresh transaction


def init_instruments():
    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS instruments ("
            "exchange TEXT NOT NULL, token TEXT NOT NULL, symbol TEXT NOT NULL, "
            "name TEXT NOT NULL, instrumenttype TEXT NOT NULL, expiry TEXT NOT NULL, "
            "lotsize TEXT NOT NULL, tick_size TEXT NOT NULL, PRIMARY KEY(exchange, token))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS instruments_symbol ON instruments(exchange, symbol)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS instrument_status ("
            "id INTEGER PRIMARY KEY CHECK(id = 1), source_url TEXT NOT NULL, "
            "last_success_at TEXT NOT NULL DEFAULT '', last_success_date TEXT NOT NULL DEFAULT '', "
            "row_count INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT NOT NULL DEFAULT '', "
            "last_error TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO instrument_status(id, source_url) VALUES (1, ?)", (MASTER_URL,)
        )


def download(url, timeout=120):
    """GET the master (Angel One can answer HEAD with 404)."""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def parse(body):
    rows = json.loads(body)
    if not isinstance(rows, list) or not rows:
        raise ValueError("instrument master must be a non-empty JSON list")
    parsed = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("exch_seg") or not row.get("token"):
            raise ValueError("instrument master row lacks exch_seg/token")
        values = tuple(str(row.get(key) if row.get(key) is not None else "").strip()
                       for key in SOURCE_FIELDS)
        parsed[values[:2]] = (values[0].upper(), *values[1:])
    return list(parsed.values())


def today():
    return datetime.now(IST).date().isoformat()


def status():
    with connect() as conn:
        row = conn.execute("SELECT * FROM instrument_status WHERE id=1").fetchone()
    return dict(row)


def refresh(url=MASTER_URL, fetch=None):
    """Download and replace the cache; returns (ok, message). Never raises."""
    if not REFRESH_LOCK.acquire(blocking=False):
        return False, "A refresh is already running."
    try:
        now = datetime.now(IST).isoformat(timespec="seconds")
        try:
            rows = parse((fetch or download)(url))
        except Exception as exc:
            # URL is public; exception text never includes credentials.
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            with connect() as conn:
                conn.execute(
                    "UPDATE instrument_status SET last_attempt_at=?, last_error=? WHERE id=1",
                    (now, error),
                )
                record_audit(conn, "instruments.refresh_failed", error)
            return False, f"Refresh failed; keeping the last valid cache. {error}"
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM instruments")
            conn.executemany(f"INSERT INTO instruments VALUES ({', '.join('?' * len(FIELDS))})", rows)
            conn.execute(
                "UPDATE instrument_status SET source_url=?, last_success_at=?, last_success_date=?, "
                "row_count=?, last_attempt_at=?, last_error='' WHERE id=1",
                (url, now, now[:10], len(rows), now),
            )
            for callback in AFTER_REFRESH:
                callback(conn)
            record_audit(conn, "instruments.refreshed", f"{len(rows)} rows")
        return True, f"Instrument master refreshed: {len(rows)} rows."
    finally:
        REFRESH_LOCK.release()


def refresh_if_stale():
    """Startup check: at most one successful download per calendar day (IST)."""
    if status()["last_success_date"] == today():
        return True, "Instrument master is current."
    return refresh()


def lookup(exchange, token):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM instruments WHERE exchange=? AND token=?",
            (str(exchange or "").upper(), str(token or "")),
        ).fetchone()
    return dict(row) if row else None


def loaded():
    with connect() as conn:
        return conn.execute("SELECT EXISTS(SELECT 1 FROM instruments)").fetchone()[0] == 1


def search(exchange, query, limit=50):
    """Exact token or symbol-prefix matches, for the admin lookup."""
    query = str(query or "").strip()
    with connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM instruments WHERE exchange=? AND (token=? OR UPPER(symbol) LIKE ?) "
            "ORDER BY symbol LIMIT ?",
            (str(exchange or "").upper(), query, query.upper().replace("%", "") + "%", limit),
        )]
