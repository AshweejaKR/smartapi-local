"""REST-only HIJACK controls under /local/v1/market; Yahoo and Angel One are mocked."""
from contextlib import contextmanager
from datetime import datetime
import sqlite3

from fastapi.testclient import TestClient
import pytest
import yaml

import angelone_proxy
import app as app_module
from conftest import auth_headers
import market
from test_data_sources import FakeBroker, write_config


pytestmark = pytest.mark.smoke

BASE = "/local/v1/market"
LTP = "/rest/secure/angelbroking/order/v1/getLtpData"
QUOTE = "/rest/secure/angelbroking/market/v1/quote"
CANDLES = "/rest/secure/angelbroking/historical/v1/getCandleData"
SBIN = {"exchange": "NSE", "symboltoken": "3045"}
RANGE = {"interval": "ONE_MINUTE", "fromdate": "2026-09-08 10:00", "todate": "2026-09-08 10:05"}


class FakeYahoo:
    def __init__(self):
        self.price, self.quotes, self.candle_calls = 50.0, 0, 0

    def quote(self, symbol):
        self.quotes += 1
        return {"timestamp": datetime.now(market.IST), "open": 49.0, "high": 51.0,
                "low": 48.0, "close": 49.5, "ltp": self.price, "volume": 10}

    def candles(self, symbol, interval, start, end):
        self.candle_calls += 1
        return [["2026-09-08T10:00:00+05:30", 1.0, 2.0, 0.5, 1.5, 9]]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Start the server for a market source; yields (client, headers, yahoo, broker)."""
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "controls.db")
    yahoo = FakeYahoo()
    monkeypatch.setattr(market, "PROVIDER", yahoo)
    market.CACHE.clear()

    @contextmanager
    def start(source="yahoo"):
        broker = FakeBroker()
        monkeypatch.setattr(angelone_proxy, "send", broker)
        monkeypatch.setenv("SMARTAPI_CONFIG_FILE", str(write_config(tmp_path, market_source=source, order=None, account=None)))
        with TestClient(app_module.app) as client:
            yield client, auth_headers(client), yahoo, broker

    yield start
    market.CACHE.clear()


def post(client, headers, path, **body):
    return client.post(f"{BASE}/{path}", headers=headers, json=body)


def data(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] is True and body["errorcode"] == ""
    return body["data"]


def error(response, message, code="AB1004", status=400):
    assert response.status_code == status, response.text
    assert response.json() == {"status": False, "message": message, "errorcode": code, "data": None}


def test_controls_require_a_local_session(env):
    with env() as (client, headers, _, _):
        for method, path in [("get", "state"), ("post", "hijack"), ("post", "ltp/set"), ("post", "ltp/step"),
                             ("post", "ltp/percent"), ("post", "clear"), ("post", "candles"), ("get", "candles"),
                             ("post", "candles/delete"), ("post", "candles/clear")]:
            for auth in ({}, {"Authorization": "Bearer not-a-token"}):
                response = getattr(client, method)(f"{BASE}/{path}", headers=auth)
                error(response, "Invalid or expired token", "AG8001", 403)


def test_invalid_payloads_are_rejected_without_changes(env):
    with env() as (client, headers, _, _):
        error(post(client, headers, "hijack", ltp=100), "exchange and symboltoken are required")
        error(post(client, headers, "hijack", exchange="NSE", symboltoken="3 045", ltp=100),
              "exchange and symboltoken are required")
        error(post(client, headers, "hijack", exchange="NSE", symboltoken="11536", ltp=100),
              "Failed to get symbol details", "AB1018")  # in the master, not a verified Yahoo symbol
        for bad in (0, -1, "abc", "nan", "inf", True, None):
            response = post(client, headers, "hijack", **SBIN, ltp=bad)
            assert response.status_code == 400 and response.json()["message"].startswith("ltp must")
        error(post(client, headers, "hijack", **SBIN, ltp=100, high=-2), "high must be greater than zero")
        for bad in (-1, 1.5, "x", True):
            error(post(client, headers, "hijack", **SBIN, ltp=100, volume=bad), "volume must be a non-negative whole number")
        error(post(client, headers, "ltp/step", **SBIN, step=1), "HIJACK is not enabled for this instrument")
        error(post(client, headers, "ltp/set", **SBIN, ltp=1), "HIJACK is not enabled for this instrument")
        candle = {**SBIN, "timestamp": "2026-09-08T10:00", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 5}
        error(post(client, headers, "candles", **{**candle, "timestamp": "yesterday"}), "timestamp must be an ISO-8601 timestamp")
        error(post(client, headers, "candles", **{**candle, "low": 13}), "Candle must satisfy low <= open, close <= high")
        error(post(client, headers, "candles", **{**candle, "volume": None}), "volume must be a non-negative whole number")
        error(post(client, headers, "candles/delete", **candle), "Candle not found", status=404)
        error(client.get(f"{BASE}/candles", headers=headers, params={**SBIN, "fromdate": "2026-09-09", "todate": "2026-09-08"}),
              "fromdate must not be after todate")
        state = data(client.get(f"{BASE}/state", headers=headers, params=SBIN))
        assert state["hijack"] is False and state["overridden_candles"] == 0 and state["tradingsymbol"] == "SBIN-EQ"


def test_set_step_percent_and_nonpositive_results(env):
    with env() as (client, headers, _, _):
        state = data(post(client, headers, "hijack", **SBIN, ltp="100", open=90, high=110, volume=7))
        assert state["source"] == "HIJACK" and state["provider"] == "yahoo"
        assert state["quote"] == {"ltp": 100.0, "open": 90.0, "high": 110.0, "low": 100.0, "close": 100.0, "volume": 7}
        assert data(post(client, headers, "ltp/set", **SBIN, ltp=105))["quote"]["ltp"] == 105
        assert data(post(client, headers, "ltp/step", **SBIN, step=-5.55))["quote"]["ltp"] == 99.45
        state = data(post(client, headers, "ltp/percent", **SBIN, percent=10))
        assert state["quote"]["ltp"] == 109.395
        assert state["override"] == {"ltp": 109.395, "open": 90.0, "high": 110.0, "low": None, "close": None, "volume": 7}
        error(post(client, headers, "ltp/step", **SBIN, step=-109.395), "Resulting LTP must be greater than zero")
        error(post(client, headers, "ltp/percent", **SBIN, percent=-100), "Resulting LTP must be greater than zero")
        error(post(client, headers, "ltp/step", **SBIN, step="inf"), "step must be finite")
        assert data(client.get(f"{BASE}/state", headers=headers, params=SBIN))["quote"]["ltp"] == 109.395


def test_hijack_drives_ltp_and_quote_and_clear_restores_yahoo(env):
    with env() as (client, headers, yahoo, _):
        request = {**SBIN, "tradingsymbol": "SBIN-EQ"}
        assert client.post(LTP, headers=headers, json=request).json()["data"]["ltp"] == 50.0
        assert any(key[1:3] == ("NSE", "3045") for key in market.CACHE.values)
        data(post(client, headers, "hijack", **SBIN, ltp=123.5))
        assert not any(key[1:3] == ("NSE", "3045") for key in market.CACHE.values)  # cache cleared
        assert client.post(LTP, headers=headers, json=request).json()["data"] == {
            "exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045",
            "open": 123.5, "high": 123.5, "low": 123.5, "close": 123.5, "ltp": 123.5}
        quote = client.post(QUOTE, headers=headers, json={"mode": "FULL", "exchangeTokens": {"NSE": ["3045", "2885"]}}).json()
        rows = {row["symbolToken"]: row for row in quote["data"]["fetched"]}
        assert rows["3045"]["ltp"] == 123.5 and rows["2885"]["ltp"] == 50.0
        calls = yahoo.quotes
        yahoo.price = 51.0
        data(post(client, headers, "clear", **SBIN))
        assert client.post(LTP, headers=headers, json=request).json()["data"]["ltp"] == 51.0
        assert yahoo.quotes == calls + 1  # provider fetched again, not served from a stale cache


def test_hijack_overrides_dummy_source_for_any_instrument(env):
    with env(None) as (client, headers, _, _):
        weird = {"exchange": "XYZ", "symboltoken": "ABC-1"}
        data(post(client, headers, "hijack", **weird, ltp=7.25))
        response = client.post(LTP, headers=headers, json={**weird, "tradingsymbol": "ANY"}).json()
        assert response["data"]["ltp"] == 7.25
        other = client.post(LTP, headers=headers, json={**SBIN, "tradingsymbol": "SBIN-EQ"}).json()
        assert other["data"]["ltp"] == 100.05  # unhijacked instruments keep the dummy price
        candles = client.post(CANDLES, headers=headers, json={**weird, **RANGE}).json()["data"]
        assert candles == [["2026-09-08T10:00:00+05:30", 7.25, 7.25, 7.25, 7.25, 0]]


def test_hijack_overrides_angel_one_without_touching_the_broker(env):
    with env("angelone") as (client, headers, _, broker):
        error(post(client, headers, "hijack", exchange="NSE", symboltoken="424242", ltp=1),
              "Failed to get symbol details", "AB1018")  # not in the instrument master
        state = data(post(client, headers, "hijack", exchange="NSE", symboltoken="11536", ltp=3000))
        assert state["tradingsymbol"] == "TCS-EQ" and state["provider"] == "angelone"
        tcs = {"exchange": "NSE", "symboltoken": "11536", "tradingsymbol": "TCS-EQ"}
        assert client.post(LTP, headers=headers, json=tcs).json()["data"]["ltp"] == 3000
        assert client.post(LTP, headers=headers, json={**SBIN, "tradingsymbol": "SBIN-EQ"}).json()["data"]["ltp"] == 123.45
        quote = client.post(QUOTE, headers=headers, json={"mode": "LTP", "exchangeTokens": {"NSE": ["3045", "11536"]}}).json()
        assert quote["data"]["fetched"][-1] == {"exchange": "NSE", "tradingSymbol": "TCS-EQ", "symbolToken": "11536", "ltp": 3000}
        hijacked_only = client.post(QUOTE, headers=headers, json={"mode": "LTP", "exchangeTokens": {"NSE": ["11536"]}}).json()
        assert [row["ltp"] for row in hijacked_only["data"]["fetched"]] == [3000]
        assert client.post(CANDLES, headers=headers, json={**tcs, **RANGE}).json()["data"][0][4] == 3000
        forwarded = [call for call in broker.calls if call[1] != angelone_proxy.LOGIN_PATH]
        assert [(call[1], call[2].get("symboltoken") or call[2].get("exchangeTokens")) for call in forwarded] == [
            (LTP, "3045"), (QUOTE, {"NSE": ["3045"]})]  # hijacked tokens never reach the broker


def test_candles_create_list_delete_range_and_clear(env):
    with env() as (client, headers, yahoo, _):
        data(post(client, headers, "hijack", **SBIN, ltp=100, volume=3))
        candle = {**SBIN, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 5}
        saved = data(post(client, headers, "candles", **candle, timestamp="2026-09-08T04:31:45+00:00"))
        assert saved["candle"] == ["2026-09-08T10:01:00+05:30", 10.0, 12.0, 9.0, 11.0, 5]  # IST minute
        data(post(client, headers, "candles", **{**candle, "close": 12}, timestamp="2026-09-08T10:01:30"))  # replace
        data(post(client, headers, "candles", **candle, timestamp="2026-09-08T11:00"))
        listed = data(client.get(f"{BASE}/candles", headers=headers, params={**SBIN, "fromdate": "2026-09-08T10:00", "todate": "2026-09-08T10:30"}))
        assert listed["candles"] == [["2026-09-08T10:01:00+05:30", 10.0, 12.0, 9.0, 12.0, 5]]
        assert listed["overridden_candles"] == 2
        body = {**SBIN, **RANGE}
        assert client.post(CANDLES, headers=headers, json=body).json()["data"] == listed["candles"]
        empty = {**body, "fromdate": "2026-09-09 09:15", "todate": "2026-09-09 09:20"}
        assert client.post(CANDLES, headers=headers, json=empty).json()["data"] == [
            ["2026-09-09T09:15:00+05:30", 100.0, 100.0, 100.0, 100.0, 3]]  # synthesized from HIJACK
        assert yahoo.candle_calls == 0  # never merged with provider candles

        data(post(client, headers, "clear", **SBIN))  # saved candles stay but stop applying
        assert client.post(CANDLES, headers=headers, json=body).json()["data"] == [
            ["2026-09-08T10:00:00+05:30", 1.0, 2.0, 0.5, 1.5, 9]]
        assert data(client.get(f"{BASE}/candles", headers=headers, params=SBIN))["overridden_candles"] == 2

        data(post(client, headers, "candles/delete", **SBIN, timestamp="2026-09-08T10:01:00+05:30"))
        assert data(post(client, headers, "candles/clear", **SBIN))["deleted"] == 1
        assert data(client.get(f"{BASE}/candles", headers=headers, params=SBIN))["candles"] == []


def test_controls_persist_across_restart(env):
    with env() as (client, headers, _, _):
        data(post(client, headers, "hijack", **SBIN, ltp=100, close=95))
        data(post(client, headers, "ltp/step", **SBIN, step=2.5))
        data(post(client, headers, "candles", **SBIN, timestamp="2026-09-08T10:02", open=1, high=1, low=1, close=1, volume=0))
    with env() as (client, headers, _, _):
        state = data(client.get(f"{BASE}/state", headers=headers, params=SBIN))
        assert state["hijack"] and state["quote"]["ltp"] == 102.5 and state["quote"]["close"] == 95
        assert state["overridden_candles"] == 1
        page = client.get("/admin/market").text
        assert "Active hijacks (1)" in page and "102.5" in page


def test_mutations_are_audited_without_tokens(env, tmp_path):
    with env() as (client, headers, _, _):
        for path, body in [("hijack", {"ltp": 100}), ("ltp/set", {"ltp": 101}), ("ltp/step", {"step": 1}),
                           ("ltp/percent", {"percent": 1}),
                           ("candles", {"timestamp": "2026-09-08T10:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}),
                           ("candles/delete", {"timestamp": "2026-09-08T10:00"}), ("candles/clear", {}), ("clear", {})]:
            data(post(client, headers, path, **SBIN, **body))
        data(client.get(f"{BASE}/state", headers=headers, params=SBIN))  # reads are not audited
    with sqlite3.connect(tmp_path / "controls.db") as conn:
        rows = conn.execute("SELECT action, detail, client_code FROM audit_log WHERE action LIKE 'market.%'").fetchall()
    assert [row[0] for row in rows] == [
        "market.hijack_enabled", "market.ltp_set", "market.ltp_step", "market.ltp_percent",
        "market.candle_saved", "market.candle_deleted", "market.candles_cleared", "market.hijack_cleared"]
    assert {row[2] for row in rows} == {"DUMMY001"}
    token = headers["Authorization"].removeprefix("Bearer ")
    text = repr(rows)
    assert token not in text and "Bearer" not in text and "Authorization" not in text
    assert rows[0][1] == "NSE:3045 ltp=100.0"
    saved = yaml.safe_load((tmp_path / "source.yaml").read_text(encoding="utf-8"))
    assert saved["market_data_source"] == "yahoo"  # controls never touch the configuration
