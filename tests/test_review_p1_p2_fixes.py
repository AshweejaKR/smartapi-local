from datetime import datetime
import sqlite3

from fastapi.testclient import TestClient
import pandas as pd
import pytest

import app as app_module
import fault
import market
import orders
import phase11
import portfolio
import smartapi_parity as parity


def test_order_field_validation():
    base = {
        "variety": "NORMAL", "exchange": "NSE", "tradingsymbol": "SBIN-EQ",
        "symboltoken": "3045", "transactiontype": "BUY", "ordertype": "LIMIT",
        "producttype": "DELIVERY", "duration": "DAY", "price": "100", "quantity": "1",
    }
    for field in ("variety", "producttype", "duration"):
        with pytest.raises(ValueError):
            orders.parse_order({**base, field: "INVALID"})


def test_cache_uses_fresh_value():
    cache = market.LastKnownCache()
    cache.put("x", {"ltp": 100})
    assert cache.get("x", 5) == {"ltp": 100}


@pytest.mark.parametrize("interval", ["THREE_MINUTE", "TEN_MINUTE"])
def test_resample_handles_tz_aware_index(interval):
    frame = pd.DataFrame(
        {"Open": [100] * 6, "High": [102] * 6, "Low": [99] * 6,
         "Close": [101] * 6, "Volume": [10] * 6},
        index=pd.date_range("2026-09-08 09:15", periods=6, freq="min", tz=market.IST),
    )

    class Ticker:
        def history(self, **kwargs):
            return frame

    rows = market.YahooProvider(lambda _: Ticker()).candles(
        "TEST.NS", interval, datetime(2026, 9, 8, 9, 15), datetime(2026, 9, 8, 9, 20)
    )
    assert rows and rows[0][0].endswith("+05:30")


def test_candle_timestamp_normalizes_ist():
    assert market.candle_timestamp("2026-09-08T10:00:00+05:30") == "2026-09-08T10:00:00"


def test_market_admin_switches_and_deletes_candle(tmp_path, monkeypatch):
    db = tmp_path / "admin-market.db"
    monkeypatch.setattr(app_module, "DB_PATH", db)
    with TestClient(app_module.app) as client:
        assert "<h2>NSE / NIFTYBEES-EQ</h2>" in client.get(
            "/admin/market?symbol=NSE%3A10576"
        ).text
        data = {
            "exchange": "NSE", "symboltoken": "3045", "timestamp": "2026-09-08T10:00",
            "candle_open": 100, "candle_high": 101, "candle_low": 99,
            "candle_close": 100, "candle_volume": 10,
        }
        assert client.post("/admin/market", data={**data, "action": "save_candle"}).status_code == 200
        assert client.post("/admin/market", data={
            "action": "delete_candle", "exchange": "NSE", "symboltoken": "3045",
            "timestamp": "2026-09-08T10:00:00+05:30",
        }).status_code == 200
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_override_candles").fetchone()[0] == 0


def test_gtt_id_uses_max_not_count():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE gtt_rules (id TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO gtt_rules VALUES (?)", [("1",), ("3",)])
    assert phase11.next_gtt_id(conn) == "4"


def test_api_order_status_matches_broker_values():
    expected = {
        "PENDING": "open pending", "OPEN": "open", "FILLED": "complete",
        "REJECTED": "rejected", "CANCELLED": "cancelled",
    }
    assert {key: portfolio.api_order_status(key) for key in expected} == expected


def test_health_and_slow_fault(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "health.db")
    monkeypatch.setenv("SMARTAPI_SLOW_DELAY_MS", "125")
    with TestClient(app_module.app) as client:
        assert client.get("/health").json()["data"] == {"service": "smartapi-local"}
    assert fault.slow_delay_seconds() == 0.125


def test_parity_cleanup_removes_temporary_user(tmp_path):
    db = tmp_path / "parity.db"
    with sqlite3.connect(db) as conn:
        for table in ("users", "accounts", "sessions", "trades", "positions", "holdings", "gtt_rules"):
            conn.execute(f"CREATE TABLE {table}(client_code TEXT)")
        conn.execute("CREATE TABLE orders(client_code TEXT, order_id TEXT)")
        conn.execute("CREATE TABLE order_events(order_id TEXT)")
        conn.execute("INSERT INTO users VALUES ('PARITY14TEST')")
        conn.execute("INSERT INTO accounts VALUES ('PARITY14TEST')")
    runner = parity.Parity()
    runner.db, runner.user = db, "PARITY14TEST"
    runner.cleanup_local_user()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
