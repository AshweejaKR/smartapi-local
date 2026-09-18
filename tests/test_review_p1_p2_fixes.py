import sqlite3

from fastapi.testclient import TestClient

import app as app_module
import fault
import market
import phase11
import portfolio
import smartapi_parity as parity


def test_candle_timestamp_normalizes_ist():
    assert market.candle_timestamp("2026-09-08T10:00:00+05:30") == "2026-09-08T10:00:00"


def test_market_admin_switches_exchange_and_deletes_candle(tmp_path, monkeypatch):
    db = tmp_path / "admin-market.db"
    monkeypatch.setattr(app_module, "DB_PATH", db)
    with TestClient(app_module.app) as client:
        page = client.get("/admin/market?symbol=NSE%3A10576")
        assert "<h2>NSE / NIFTYBEES-EQ</h2>" in page.text

        saved = client.post("/admin/market", data={
            "action": "save_candle", "exchange": "NSE", "symboltoken": "3045",
            "timestamp": "2026-09-08T10:00", "candle_open": 100,
            "candle_high": 101, "candle_low": 99, "candle_close": 100,
            "candle_volume": 10,
        })
        assert saved.status_code == 200

        deleted = client.post("/admin/market", data={
            "action": "delete_candle", "exchange": "NSE", "symboltoken": "3045",
            "timestamp": "2026-09-08T10:00:00+05:30",
        })
        assert deleted.status_code == 200

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_override_candles").fetchone()[0] == 0


def test_gtt_id_uses_max_not_count():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE gtt_rules (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO gtt_rules(id) VALUES (?)", [("1",), ("3",)])
    assert phase11.next_gtt_id(conn) == "4"


def test_api_order_status_matches_broker_values():
    assert portfolio.api_order_status("PENDING") == "open pending"
    assert portfolio.api_order_status("OPEN") == "open"
    assert portfolio.api_order_status("FILLED") == "complete"
    assert portfolio.api_order_status("REJECTED") == "rejected"
    assert portfolio.api_order_status("CANCELLED") == "cancelled"


def test_health_has_no_stale_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "health.db")
    with TestClient(app_module.app) as client:
        data = client.get("/health").json()["data"]
    assert data == {"service": "smartapi-local"}


def test_slow_fault_uses_fixed_delay(monkeypatch):
    monkeypatch.setenv("SMARTAPI_SLOW_DELAY_MS", "125")
    assert fault.slow_delay_seconds() == 0.125


def test_parity_cleanup_removes_temporary_user(tmp_path):
    db = tmp_path / "parity.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE users(client_code TEXT)")
        conn.execute("CREATE TABLE accounts(client_code TEXT)")
        conn.execute("CREATE TABLE sessions(client_code TEXT)")
        conn.execute("CREATE TABLE trades(client_code TEXT)")
        conn.execute("CREATE TABLE orders(client_code TEXT, order_id TEXT)")
        conn.execute("CREATE TABLE positions(client_code TEXT)")
        conn.execute("CREATE TABLE holdings(client_code TEXT)")
        conn.execute("CREATE TABLE gtt_rules(client_code TEXT)")
        conn.execute("CREATE TABLE order_events(order_id TEXT)")
        conn.execute("INSERT INTO users VALUES ('PARITY14TEST')")
        conn.execute("INSERT INTO accounts VALUES ('PARITY14TEST')")

    runner = parity.Parity()
    runner.db = db
    runner.user = "PARITY14TEST"
    runner.cleanup_local_user()

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
