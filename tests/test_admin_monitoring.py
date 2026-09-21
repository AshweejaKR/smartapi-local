"""Admin monitoring and destructive reset regression coverage."""
from datetime import datetime, timedelta
import sqlite3

from fastapi.testclient import TestClient
import pytest

import admin
import app as app_module
import market
import orders
from charges import DEFAULTS
from fault import active_fault, start_fault
from rate_limit import DEFAULT_LIMITS, limiter, save_limit


LOGIN = "/rest/auth/angelbroking/user/v1/loginByPassword"
PROFILE = "/rest/secure/angelbroking/user/v1/getProfile"
RMS = "/rest/secure/angelbroking/user/v1/getRMS"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "admin-monitor.db")
    monkeypatch.setenv("SMARTAPI_MARKET_FILL_DELAY_MS", "3600000")
    # Keep the background checker deterministic; monitoring itself must not fetch.
    monkeypatch.setattr(market.PROVIDER, "quote", lambda symbol: {
        "ltp": 100, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1,
    })
    with TestClient(app_module.app) as client:
        with admin.connect() as conn:
            conn.execute("UPDATE rate_limit_config SET enabled=0")
        yield client


def rows(table):
    with admin.connect() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def seed(client):
    client.post("/admin/account/DUMMY001/funds", data={"action": "add", "amount": "10000"})
    client.post("/admin/market", data={"exchange": "NSE", "symboltoken": "3045", "mode": "HIJACK", "ltp": "100", "volume": "25"})
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    with admin.connect() as conn:
        for name, status, price, day, kind in (
            ("old-filled", "FILLED", 100, yesterday, "MARKET"),
            ("today-filled", "FILLED", 100, today, "MARKET"),
            ("old-open", "OPEN", 80, yesterday, "LIMIT"),
            ("today-open", "OPEN", 90, today, "LIMIT"),
            ("today-market", "PENDING", 100, today, "MARKET"),
        ):
            stamp = day.strftime("%d-%b-%Y %H:%M:%S")
            conn.execute(
                "INSERT INTO orders (client_code, order_id, unique_order_id, order_type, "
                "product_type, quantity, transaction_type, exchange, tradingsymbol, "
                "symboltoken, status, price, filled_quantity, reserved_funds, created_at, "
                "accepted_at_ms, updated_at) VALUES (?, ?, ?, ?, 'DELIVERY', 1, 'BUY', "
                "'NSE', 'SBIN-EQ', '3045', ?, ?, ?, ?, ?, ?, ?)",
                (
                    "DUMMY001", name, name, kind, status, price,
                    int(status == "FILLED"), 0 if status == "FILLED" else price,
                    stamp, int(today.timestamp() * 1000), stamp,
                ),
            )
            orders.add_event(conn, name, status)
            if status == "FILLED":
                conn.execute(
                    "INSERT INTO trades (client_code, trade_id, order_id, exchange, "
                    "tradingsymbol, symboltoken, transaction_type, product_type, quantity, "
                    "price, trade_time) VALUES ('DUMMY001', ?, ?, 'NSE', 'SBIN-EQ', "
                    "'3045', 'BUY', 'DELIVERY', 1, ?, ?)",
                    ("T-" + name, name, price, stamp),
                )
        conn.execute(
            "INSERT INTO positions (client_code, exchange, symboltoken, product_type, "
            "tradingsymbol, net_qty, buy_qty, buy_amount, avg_price, last_price) "
            "VALUES ('DUMMY001', 'NSE', '3045', 'DELIVERY', 'SBIN-EQ', 2, 2, 200, 100, 100)"
        )
        conn.execute(
            "INSERT INTO holdings (client_code, exchange, symboltoken, tradingsymbol, "
            "quantity, average_price, last_price) "
            "VALUES ('DUMMY001', 'NSE', '3045', 'SBIN-EQ', 2, 100, 100)"
        )
    market.save_candle("NSE", "3045", today.replace(second=0, microsecond=0).isoformat(), {"open": 99, "high": 101, "low": 99, "close": 100, "volume": 5})
    with admin.connect() as conn:
        conn.execute("UPDATE accounts SET available_balance=9990, used_funds=200, realized_pnl=7, total_charges=10")
        conn.execute("UPDATE trades SET total_charges=5, brokerage=5, gross_trade_value=100, net_pnl=-5")
        conn.execute("UPDATE positions SET total_charges=10")
        conn.execute("UPDATE charge_config SET rate=5 WHERE name='brokerage'")
        conn.execute("INSERT INTO gtt_rules VALUES ('rule-1', 'DUMMY001', 'NEW', '{}', ?, ?)", (today.isoformat(), today.isoformat()))


def reset(client, action):
    response = client.post(f"/admin/resets/{action}", data={"confirm": "RESET"}, follow_redirects=False)
    assert response.status_code == 303
    assert any(row["action"] == "reset." + action for row in rows("audit_log"))


@pytest.mark.parametrize("action", list(admin.RESETS))
def test_every_reset_requires_explicit_confirmation(client, action):
    seed(client)
    before = {table: rows(table) for table in ("accounts", "orders", "trades", "positions", "holdings", "market_overrides", "charge_config", "rate_limit_config")}
    page = client.get(f"/admin/resets/{action}")
    assert page.status_code == 200 and 'name="confirm"' in page.text
    for data in ({}, {"confirm": "yes"}, {"confirm": "reset"}):
        rejected = client.post(f"/admin/resets/{action}", data=data)
        assert rejected.status_code == 400 and "Type RESET" in rejected.text
    assert before == {table: rows(table) for table in before}
    assert not any(row["action"].startswith("reset.") for row in rows("audit_log"))


def test_dashboard_monitors_rate_counters_audit_and_outage(client, monkeypatch):
    seed(client)
    save_limit("endpoint", RMS, 0, 0, 3)
    limiter.check(RMS, "DUMMY001")
    start_fault("503", 60)
    monkeypatch.setattr(market.PROVIDER, "quote", lambda symbol: pytest.fail("Dashboard fetched Yahoo"))
    page = client.get("/admin")
    assert page.status_code == 200
    for text in ("Current funds", "Market source", "Open limit orders", "Positions", "Holdings", "Trades", "Charge configuration", "Rate-limit status", "Recent audit entries", "HIJACK", "503 is active", "old-filled", "DUMMY001", "funds.add"):
        assert text in page.text
    for kind in admin.MONITORS:
        assert client.get("/admin/monitor/" + kind).status_code == 200
    open_limits = client.get("/admin/monitor/open-orders").text
    assert "old-open" in open_limits and "today-open" in open_limits
    assert "today-market" not in open_limits and "old-filled" not in open_limits
    assert "Brokerage" in client.get("/admin/monitor/trades").text
    counters = client.get("/admin/rate-limits").text
    assert "DUMMY001" in counters and "hour" in counters
    assert "fault.started" in client.get("/admin/audit").text
    assert client.get("/admin/resets").status_code == 200
    assert client.get("/admin/monitor/unknown").status_code == 404
    assert client.get("/admin/resets/unknown").status_code == 404
    assert client.post("/admin/resets/unknown", data={"confirm": "RESET"}).status_code == 404


def test_today_reset_preserves_prior_history_and_account_portfolio(client):
    seed(client)
    before = {table: rows(table) for table in ("accounts", "positions", "holdings")}
    # Both native order timestamps and imported ISO dates must be handled.
    with admin.connect() as conn:
        conn.execute("UPDATE orders SET created_at=? WHERE order_id='today-filled'", (datetime.now().isoformat(),))
        conn.execute("UPDATE trades SET trade_time=? WHERE order_id='today-filled'", (datetime.now().isoformat(),))
    reset(client, "today")
    assert {row["order_id"] for row in rows("orders")} == {"old-filled", "old-open"}
    assert [row["trade_id"] for row in rows("trades")] == ["T-old-filled"]
    assert {row["order_id"] for row in rows("order_events")} == {"old-filled", "old-open"}
    assert before == {table: rows(table) for table in before}
    with admin.connect() as conn:
        assert orders.free_cash(conn, "DUMMY001") == 9710
    app_module.init_db()
    assert len(rows("orders")) == 2


def test_open_order_reset_releases_reservations_and_prevents_fill(client):
    seed(client)
    reset(client, "open-orders")
    assert {row["order_id"] for row in rows("orders")} == {"old-filled", "today-filled"}
    assert len(rows("trades")) == 2
    orders.fill_order("today-open", 80)
    assert len(rows("trades")) == 2
    with admin.connect() as conn:
        assert orders.free_cash(conn, "DUMMY001") == 9790


def test_position_reset_clears_exposure_and_preserves_history_charges_and_reservations(client):
    seed(client)
    reset(client, "positions")
    assert rows("positions") == rows("holdings") == []
    account = rows("accounts")[0]
    assert (account["available_balance"], account["used_funds"], account["realized_pnl"], account["total_charges"]) == (9990, 0, 7, 10)
    assert len(rows("trades")) == 2 and len(rows("orders")) == 5
    with admin.connect() as conn:
        assert orders.free_cash(conn, "DUMMY001") == 9720


def test_hijack_reset_clears_all_quotes_and_candles_and_persists(client):
    seed(client)
    reset(client, "hijack")
    assert rows("market_overrides") == rows("market_override_candles") == []
    assert all(row["mode"] == "YAHOO" for row in market.list_mappings())
    assert len(rows("orders")) == 5
    app_module.init_db()
    assert rows("market_overrides") == rows("market_override_candles") == []


def test_charge_default_reset_does_not_rewrite_historical_fees(client):
    seed(client)
    before = rows("accounts"), rows("trades")
    reset(client, "charges")
    assert before == (rows("accounts"), rows("trades"))
    config = rows("charge_config")
    assert all(row["rate"] == 0 for row in config)
    assert [(row["name"], row["label"], row["calculation"], row["side"], row["basis"], row["enabled"]) for row in config] == list(DEFAULTS)


def test_rate_default_reset_removes_custom_rules_and_counters(client):
    save_limit("group", "market", 2, 3, 4)
    limiter.check("/rest/secure/angelbroking/order/v1/getLtpData", "DUMMY001")
    assert admin.limit_status()
    reset(client, "rate-limits")
    assert admin.limit_status() == []
    config = rows("rate_limit_config")
    assert {(row["scope"], row["target"], row["per_second"], row["per_minute"], row["per_hour"]) for row in config} == set(DEFAULT_LIMITS)
    assert all(row["enabled"] for row in config)


def test_full_reset_restores_seed_and_revokes_tokens_then_survives_restart(client):
    seed(client)
    tokens = client.post(LOGIN, headers={"X-PrivateKey": "DUMMY_API_KEY"}, json={"clientcode": "DUMMY001", "password": "password", "totp": "123456"}).json()["data"]
    client.post("/admin/users/add", data={"client_code": "SECOND", "password": "second-password", "api_key": "second-key", "totp": "654321", "name": "Second", "email": "second@test.test", "mobile": "9000000001"})
    market.upsert_mapping("BSE", "TEST-EQ", "1", "TEST.BO")
    market.CACHE.put("test", {"ltp": 1})
    save_limit("group", "market", 1, 2, 3)
    limiter.check("/rest/secure/angelbroking/order/v1/getLtpData", "DUMMY001")
    start_fault("503", 60)
    reset(client, "full")
    for table in ("orders", "order_events", "trades", "positions", "holdings", "sessions", "gtt_rules", "fault_events", "market_overrides", "market_override_candles"):
        assert rows(table) == []
    assert active_fault() is None and not market.CACHE.values and not limiter.events
    assert len(rows("users")) == len(rows("accounts")) == 1
    assert rows("accounts")[0]["available_balance"] == 0
    assert len(rows("symbol_mappings")) == len(market.DEFAULT_MAPPINGS)
    assert [row["action"] for row in rows("audit_log")] == ["reset.full"]
    app_module.init_db()
    assert rows("accounts")[0]["available_balance"] == 0
    assert client.get(PROFILE, headers={"Authorization": "Bearer " + tokens["jwtToken"]}).status_code == 403
    assert client.post(LOGIN, headers={"X-PrivateKey": "DUMMY_API_KEY"}, json={"clientcode": "DUMMY001", "password": "password", "totp": "123456"}).json()["status"] is True


def test_failed_full_reset_rolls_back_all_database_changes(client, monkeypatch):
    seed(client)
    before = {table: rows(table) for table in ("users", "accounts", "orders", "trades", "positions", "holdings", "market_overrides", "gtt_rules", "charge_config", "rate_limit_config")}

    def failed_audit(*args):
        raise sqlite3.OperationalError("test rollback")

    monkeypatch.setattr(admin, "record_audit", failed_audit)
    with pytest.raises(sqlite3.OperationalError, match="test rollback"):
        client.post("/admin/resets/full", data={"confirm": "RESET"})
    assert before == {table: rows(table) for table in before}


def test_withdrawal_cannot_consume_open_reservations(client):
    seed(client)
    before = rows("accounts")
    response = client.post("/admin/account/DUMMY001/funds", data={"action": "remove", "amount": "9800"})
    assert "Cannot remove more" in response.text
    assert rows("accounts") == before


def test_market_redirect_retains_selection_and_audit_escapes_html(client):
    response = client.post("/admin/market", data={"exchange": "NSE", "symboltoken": "2885", "mode": "HIJACK", "ltp": "123"}, follow_redirects=False)
    assert "symbol=NSE%3A2885&message=" in response.headers["location"]
    admin.audit("test", "<script>alert(1)</script>")
    html = client.get("/admin/audit").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
