"""Unmodified official SDK against a live Uvicorn server and isolated SQLite DB."""
import sqlite3
import time

import pytest
from SmartApi import SmartConnect

from charges import DEFAULTS
from conftest import Ticker
import market


ORDER = {
    "variety": "NORMAL", "exchange": "NSE", "tradingsymbol": "SBIN-EQ",
    "symboltoken": "3045", "transactiontype": "BUY", "ordertype": "MARKET",
    "producttype": "DELIVERY", "duration": "DAY", "price": "0", "quantity": "2",
}


def place(server, **changes):
    result = server.sdk.placeOrderFullResponse({**ORDER, **changes})
    assert result and result["status"], result
    return result["data"]["orderid"]


def set_charges(server, **rules):
    form = {}
    for name, _, calculation, side, basis, enabled in DEFAULTS:
        values = {"rate": 0, "calculation": calculation, "side": side,
                  "basis": basis, "enabled": "on" if enabled else ""}
        values.update(rules.get(name, {}))
        form.update({f"{name}_{key}": value for key, value in values.items()})
    assert server.http.post("/admin/charges", data=form).status_code == 303


def test_session_profile_refresh_logout(sdk_server):
    s = sdk_server
    assert s.sdk.getProfile(s.sdk.refresh_token)["data"]["clientcode"] == "DUMMY001"
    old = s.sdk.access_token
    refreshed = s.sdk.generateToken(s.sdk.refresh_token)
    assert refreshed["status"] and s.sdk.access_token != old
    old_sdk = SmartConnect(api_key="DUMMY_API_KEY", root=s.root)
    old_sdk.setAccessToken(old)
    assert old_sdk.rmsLimit()["status"] is False
    assert s.sdk.terminateSession("DUMMY001")["status"] is True
    assert s.sdk.rmsLimit()["status"] is False
    assert s.sdk.generateToken(s.sdk.refresh_token)["status"] is False


@pytest.mark.parametrize("client,password,totp,key", [
    ("MISSING", "password", "123456", "DUMMY_API_KEY"),
    ("DUMMY001", "bad-password", "123456", "DUMMY_API_KEY"),
    ("DUMMY001", "password", "999999", "DUMMY_API_KEY"),
    ("DUMMY001", "password", "123456", "BAD_KEY"),
])
def test_invalid_login(sdk_server, client, password, totp, key):
    sdk = SmartConnect(api_key=key, root=sdk_server.root)
    response = sdk.generateSession(client, password, totp)
    assert response == {"status": False, "message": "Invalid client code, password, TOTP, or API key",
                        "errorcode": "AG8001", "data": None}


def test_totp_verification_can_be_disabled_for_local_testing(sdk_server, monkeypatch):
    monkeypatch.setenv("SMARTAPI_DISABLE_TOTP", "1")
    relaxed = SmartConnect(api_key="DUMMY_API_KEY", root=sdk_server.root)
    assert relaxed.generateSession("DUMMY001", "password", "any-value")['status'] is True
    monkeypatch.setenv("SMARTAPI_DISABLE_TOTP", "0")
    strict = SmartConnect(api_key="DUMMY_API_KEY", root=sdk_server.root)
    assert strict.generateSession("DUMMY001", "password", "any-value")['status'] is False


def test_disabled_user_and_expired_tokens(sdk_server):
    s = sdk_server
    with sqlite3.connect(s.db) as conn:
        conn.execute("UPDATE sessions SET access_expires_at=0")
    assert s.sdk.rmsLimit()["status"] is False
    assert s.sdk.generateToken(s.sdk.refresh_token)["status"] is True
    assert s.sdk.rmsLimit()["status"] is True
    with sqlite3.connect(s.db) as conn:
        conn.execute("UPDATE sessions SET refresh_expires_at=0")
    assert s.sdk.generateToken(s.sdk.refresh_token)["status"] is False
    assert s.sdk.generateToken("unknown-refresh")["status"] is False
    assert s.http.post("/admin/users/DUMMY001/toggle").status_code == 303
    assert s.sdk.rmsLimit()["status"] is False
    assert s.sdk.generateSession("DUMMY001", "password", "123456")["status"] is False


def test_funds_rms_and_user_isolation(sdk_server):
    s = sdk_server
    assert s.sdk.rmsLimit()["data"]["availablecash"] == 100000
    for action, amount in (("add", "123.45"), ("remove", "23.45")):
        assert s.http.post("/admin/account/DUMMY001/funds", data={
            "action": action, "amount": amount}).status_code == 303
    assert s.sdk.rmsLimit()["data"]["availablecash"] == 100100
    assert s.http.post("/admin/users/add", data={
        "client_code": "SECOND", "password": "second-password", "api_key": "SECOND_KEY",
        "totp": "654321", "name": "Second", "email": "second@example.test", "mobile": "9000000001",
    }).status_code == 303
    other = SmartConnect(api_key="SECOND_KEY", root=s.root)
    assert other.generateSession("SECOND", "second-password", "654321")["status"]
    order = place(s, ordertype="LIMIT", price="90")
    assert other.rmsLimit()["data"]["availablecash"] == 0
    for call in (other.orderBook, other.tradeBook, other.position, other.holding):
        assert call()["data"] == []
    assert other.cancelOrder(order, "NORMAL")["status"] is False
    assert other.modifyOrder({"orderid": order, "quantity": 9})["status"] is False
    assert s.wait_order(order, "OPEN")["quantity"] == 2


def test_yahoo_hijack_quotes_and_candles(sdk_server):
    s = sdk_server
    assert s.sdk.ltpData("NSE", "SBIN-EQ", "3045")["data"]["ltp"] == 100
    assert "SBIN.NS" in Ticker.symbols
    full = s.sdk.getMarketData("FULL", {"NSE": ["3045", "missing"]})["data"]
    assert full["fetched"][0]["tradeVolume"] == 500
    assert full["unfetched"][0]["errorCode"] == "AB1018"
    params = {"exchange": "NSE", "symboltoken": "3045", "interval": "ONE_MINUTE",
              "fromdate": "2026-09-08 10:00", "todate": "2026-09-08 10:01"}
    assert s.sdk.getCandleData(params)["data"] == [
        ["2026-09-08T10:00:00+05:30", 98., 101., 97., 99., 200],
        ["2026-09-08T10:01:00+05:30", 99., 102., 98., 100., 300],
    ]
    s.hijack(120, open=110, high=125, low=105, close=115)
    assert s.sdk.getMarketData("FULL", {"NSE": ["3045"]})["data"]["fetched"][0]["tradeVolume"] == 1234
    assert s.sdk.ltpData("NSE", "SBIN-EQ", "3045")["data"]["ltp"] == 120
    assert s.http.post("/admin/market", data={
        "action": "save_candle", "exchange": "NSE", "symboltoken": "3045",
        "timestamp": "2026-09-08T10:00", "candle_open": 110, "candle_high": 125,
        "candle_low": 105, "candle_close": 120, "candle_volume": 75,
    }).status_code == 303
    assert s.sdk.getCandleData(params)["data"] == [
        ["2026-09-08T10:00:00+05:30", 110., 125., 105., 120., 75]]
    assert s.http.post("/admin/market", data={
        "exchange": "NSE", "symboltoken": "3045", "mode": "YAHOO"}).status_code == 303
    assert s.sdk.ltpData("NSE", "SBIN-EQ", "3045")["data"]["ltp"] == 100
    Ticker.fail = True
    assert s.sdk.ltpData("NSE", "SBIN-EQ", "3045")["data"]["ltp"] == 100
    assert s.sdk.ltpData("NSE", "RELIANCE-EQ", "2885")["status"] is False
    assert s.sdk.getCandleData({**params, "interval": "INVALID"})["status"] is False


def test_market_default_delay_latest_price_and_delivery(sdk_server):
    s = sdk_server
    s.hijack(100)
    order = place(s)
    with sqlite3.connect(s.db) as conn:
        accepted = conn.execute("SELECT accepted_at_ms FROM orders WHERE order_id=?", (order,)).fetchone()[0]
    assert s.wait_order(order, "PENDING")["filledshares"] == 0
    assert s.sdk.rmsLimit()["data"]["availablecash"] == 99800
    s.hijack(105)
    # Verify pending during the delay, and completion only after 1000 ms.
    while time.time() * 1000 < accepted + 800:
        assert s.wait_order(order, "PENDING")["filledshares"] == 0
        time.sleep(.05)
    filled = s.wait_order(order)
    assert time.time() * 1000 >= accepted + 1000
    assert filled["averageprice"] == 105 and filled["filledshares"] == 2
    assert s.sdk.tradeBook()["data"][0]["fillprice"] == 105
    assert s.sdk.position()["data"][0]["netqty"] == 2
    assert s.sdk.holding()["data"][0]["quantity"] == 2
    assert s.sdk.allholding()["data"]["totalholding"]["totalinvvalue"] == 210
    assert s.sdk.rmsLimit()["data"]["availablecash"] == 99790


@pytest.mark.parametrize("side,waiting,crossing", [("BUY", 110, 99), ("SELL", 90, 101)])
def test_limit_waits_crosses_and_fills_at_limit(sdk_server, side, waiting, crossing):
    s = sdk_server
    s.hijack(waiting)
    order = place(s, ordertype="LIMIT", price="100", transactiontype=side, producttype="INTRADAY")
    time.sleep(.25)
    assert s.wait_order(order, "OPEN")["filledshares"] == 0
    assert s.sdk.tradeBook()["data"] == []
    s.hijack(crossing)
    assert s.wait_order(order)["averageprice"] == 100
    assert s.sdk.position()["data"][0]["netqty"] == (2 if side == "BUY" else -2)
    assert s.sdk.holding()["data"] == []


def test_modify_and_cancel_never_fill(sdk_server):
    s = sdk_server
    s.hijack(100)
    pending = place(s)
    limit = place(s, ordertype="LIMIT", price="90")
    assert s.sdk.modifyOrder({"orderid": limit, "quantity": 3, "price": 95})["status"]
    assert s.sdk.rmsLimit()["data"]["availablecash"] == 99515
    for order in (pending, limit):
        assert s.sdk.cancelOrder(order, "NORMAL")["status"]
        assert s.sdk.cancelOrder(order, "NORMAL")["status"] is False
        assert s.sdk.modifyOrder({"orderid": order, "quantity": 4})["status"] is False
    s.hijack(80)
    time.sleep(1.15)
    assert all(row["status"] == "cancelled" for row in s.sdk.orderBook()["data"])
    assert s.sdk.tradeBook()["data"] == []
    assert s.sdk.rmsLimit()["data"]["availablecash"] == 100000
    assert s.sdk.cancelOrder("unknown", "NORMAL")["status"] is False
    assert s.sdk.modifyOrder({"orderid": "unknown", "quantity": 4})["status"] is False


def test_modify_crossing_order_fills(sdk_server):
    s = sdk_server
    s.hijack(100)
    order = place(s, ordertype="LIMIT", price="90")
    assert s.sdk.modifyOrder({"orderid": order, "price": 105, "quantity": 3})["status"]
    row = s.wait_order(order)
    assert row["averageprice"] == 105 and row["filledshares"] == 3


def test_charge_breakup_cash_and_realized_pnl(sdk_server):
    s = sdk_server
    set_charges(s, brokerage={"rate": 2}, exchange_charge={"rate": 1},
                sebi_charge={"rate": .1}, gst={"rate": 18}, stamp_duty={"rate": .5},
                stt_ctt={"rate": 1}, other_charge={"rate": .25, "enabled": "on"})
    s.hijack(100)
    buy = place(s, ordertype="LIMIT", price=100)
    s.wait_order(buy)
    trade = s.sdk.tradeBook()["data"][0]
    for field, expected in {"gross_trade_value": 200, "brokerage": 2, "exchange_charge": 2,
                            "sebi_charge": .2, "gst": .76, "stamp_duty": 1,
                            "stt_ctt": 0, "other_charge": .25, "total_charges": 6.21,
                            "gross_pnl": 0, "net_pnl": -6.21}.items():
        assert trade[field] == expected, field
    s.hijack(110)
    sell = place(s, ordertype="LIMIT", price=110, transactiontype="SELL")
    s.wait_order(sell)
    trade = s.sdk.tradeBook()["data"][0]
    assert trade["total_charges"] == 7.67
    assert trade["gross_pnl"] == 20 and trade["net_pnl"] == 12.33
    rms = s.sdk.rmsLimit()["data"]
    assert rms["gross_pnl"] == 20 and rms["total_charges"] == 13.88
    assert rms["net_pnl"] == 6.12 and rms["availablecash"] == 100006.12
    assert s.sdk.position()["data"][0]["netqty"] == 0
    assert s.sdk.holding()["data"][0]["quantity"] == 0


def test_insufficient_cash_at_acceptance_and_fill(sdk_server):
    s = sdk_server
    assert s.http.post("/admin/account/DUMMY001/funds", data={
        "action": "remove", "amount": 99900}).status_code == 303
    s.hijack(100)
    assert s.sdk.placeOrderFullResponse(ORDER)["status"] is False
    assert s.sdk.orderBook()["data"][0]["status"] == "rejected"
    order = place(s, quantity=1)
    s.hijack(101)
    assert s.wait_order(order, "REJECTED")["text"] == "Insufficient funds"
    time.sleep(.2)
    assert s.sdk.tradeBook()["data"] == []
    assert s.sdk.position()["data"] == []
    assert s.sdk.rmsLimit()["data"]["availablecash"] == 100
    set_charges(s, brokerage={"rate": 1})
    s.hijack(100)
    assert s.sdk.placeOrderFullResponse({**ORDER, "quantity": 1})["status"] is False


@pytest.mark.parametrize("field", ["per_second", "per_minute", "per_hour"])
def test_sdk_rate_limits_runtime_and_restart(sdk_server, field):
    s = sdk_server
    form = {"scope": "endpoint", "target": "/rest/secure/angelbroking/user/v1/getRMS",
            "enabled": "on", "per_second": 0, "per_minute": 0, "per_hour": 0, field: 1}
    assert s.http.post("/admin/rate-limits", data=form).status_code == 303
    assert s.sdk.rmsLimit()["status"] is True
    assert s.sdk.rmsLimit()["errorcode"] == "AB1004"
    assert s.http.get("/admin/rate-limits").status_code == 200
    s.restart()
    assert s.sdk.rmsLimit()["status"] is True
    assert s.sdk.rmsLimit()["status"] is False
    assert s.http.post("/admin/rate-limits", data={**form, field: 2}).status_code == 303
    assert s.sdk.rmsLimit()["status"] is True
    assert s.sdk.rmsLimit()["status"] is True
    assert s.sdk.rmsLimit()["status"] is False


@pytest.mark.parametrize("mode,status", [("unavailable", 503), ("500", 500), ("503", 503),
                                        ("timeout", 504), ("auth", 401), ("slow", 200)])
def test_sdk_faults_and_automatic_recovery(sdk_server, mode, status):
    s = sdk_server
    assert s.http.post("/admin/faults", data={"mode": mode, "duration": .35}).status_code == 303
    assert s.http.get("/admin").status_code == 200
    assert s.http.get("/health").status_code == 200
    started = time.monotonic()
    response = s.sdk.rmsLimit()
    assert response["status"] is (status == 200)
    if mode == "slow":
        assert time.monotonic() - started >= .2
    time.sleep(max(0, .4 - (time.monotonic() - started)))
    assert s.sdk.rmsLimit()["status"] is True
    with sqlite3.connect(s.db) as conn:
        assert conn.execute("SELECT ended_at FROM fault_events").fetchone()[0] is not None


@pytest.mark.parametrize("changes", [
    {"quantity": 0}, {"quantity": -1}, {"quantity": 1.5}, {"price": "NaN", "ordertype": "LIMIT"},
    {"price": "Infinity", "ordertype": "LIMIT"}, {"triggerprice": "NaN"},
    {"disclosedquantity": -1}, {"ordertype": "UNKNOWN"}, {"transactiontype": "UNKNOWN"},
])
def test_invalid_orders_return_envelope_without_mutation(sdk_server, changes):
    result = sdk_server.sdk.placeOrderFullResponse({**ORDER, **changes})
    assert result and result["status"] is False
    assert result["errorcode"]
    assert sdk_server.sdk.orderBook()["data"] == []
