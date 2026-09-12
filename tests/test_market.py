from datetime import datetime
import sqlite3

from fastapi.testclient import TestClient
import pandas as pd
import pytest

import app as app_module
import market


class FakeProvider:
    def __init__(self):
        self.fail = False
        self.quote_symbols = []

    def quote(self, yahoo_symbol):
        if self.fail:
            raise ConnectionError("Yahoo is unavailable")
        self.quote_symbols.append(yahoo_symbol)
        return {
            "timestamp": datetime.fromisoformat("2026-09-08T10:30:00+05:30"),
            "open": 100.0, "high": 112.0, "low": 98.0, "close": 99.0,
            "ltp": 110.0, "volume": 12345,
        }

    def candles(self, yahoo_symbol, interval, start, end):
        if self.fail:
            raise ConnectionError("Yahoo is unavailable")
        return [["2026-09-08T10:00:00+05:30", 100.0, 102.0, 99.0, 101.0, 500]]


def auth_headers(client):
    response = client.post(
        "/rest/auth/angelbroking/user/v1/loginByPassword",
        headers={"X-PrivateKey": "DUMMY_API_KEY"},
        json={"clientcode": "DUMMY001", "password": "password", "totp": "123456"},
    )
    return {"Authorization": f"Bearer {response.json()['data']['jwtToken']}"}


@pytest.fixture
def market_client(tmp_path, monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "market.db")
    monkeypatch.setattr(market, "PROVIDER", fake)
    market.CACHE.clear()
    with TestClient(app_module.app) as client:
        yield client, fake, auth_headers(client)


def test_market_routes_work_without_opening_admin(market_client):
    client, fake, headers = market_client
    ltp = client.post(
        "/rest/secure/angelbroking/order/v1/getLtpData",
        headers=headers,
        json={"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045"},
    )
    assert ltp.status_code == 200
    assert ltp.json()["data"] == {
        "exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045",
        "open": 100.0, "high": 112.0, "low": 98.0, "close": 99.0, "ltp": 110.0,
    }
    assert fake.quote_symbols == ["SBIN.NS"]

    quote = client.post(
        "/rest/secure/angelbroking/market/v1/quote",
        headers=headers,
        json={"mode": "FULL", "exchangeTokens": {"NSE": ["3045", "missing"]}},
    ).json()["data"]
    assert quote["fetched"][0]["tradeVolume"] == 12345
    assert quote["fetched"][0]["tradingSymbol"] == "SBIN-EQ"
    assert quote["unfetched"][0]["errorCode"] == "AB1018"

    candles = client.post(
        "/rest/secure/angelbroking/historical/v1/getCandleData",
        headers=headers,
        json={"exchange": "NSE", "symboltoken": "3045", "interval": "ONE_MINUTE",
              "fromdate": "2026-09-08 10:00", "todate": "2026-09-08 10:01"},
    )
    assert candles.json()["data"][0] == [
        "2026-09-08T10:00:00+05:30", 100.0, 102.0, 99.0, 101.0, 500
    ]


def test_last_known_cache_and_no_cache_error(market_client):
    client, fake, headers = market_client
    request = {"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045"}
    path = "/rest/secure/angelbroking/order/v1/getLtpData"
    expected = client.post(path, headers=headers, json=request).json()["data"]
    fake.fail = True
    assert client.post(path, headers=headers, json=request).json()["data"] == expected

    unavailable = client.post(
        path, headers=headers,
        json={"exchange": "NSE", "tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["errorcode"] == "AB2001"


def test_mapping_configuration_persists(tmp_path, monkeypatch):
    database = tmp_path / "mapping.db"
    monkeypatch.setattr(app_module, "DB_PATH", database)
    monkeypatch.setattr(market, "PROVIDER", FakeProvider())
    with TestClient(app_module.app):
        market.upsert_mapping("NSE", "TEST-EQ", "999999", "TEST.NS")
    with TestClient(app_module.app):
        saved = market.mapping_for("NSE", "999999", "TEST-EQ")
        assert saved["yahoo_symbol"] == "TEST.NS"
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM symbol_mappings WHERE symboltoken='999999'"
        ).fetchone()[0] == 1


def test_validation_and_authentication(market_client):
    client, _, headers = market_client
    path = "/rest/secure/angelbroking/historical/v1/getCandleData"
    assert client.post(path, json={}).status_code == 403
    invalid = client.post(
        path, headers=headers,
        json={"exchange": "NSE", "symboltoken": "3045", "interval": "BAD",
              "fromdate": "2026-09-08", "todate": "2026-09-08"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["errorcode"] == "AB1004"


def test_yahoo_provider_converts_intraday_frame():
    index = pd.DatetimeIndex([
        "2026-09-07T15:29:00+05:30", "2026-09-08T09:15:00+05:30",
        "2026-09-08T09:16:00+05:30",
    ])
    frame = pd.DataFrame(
        {"Open": [99, 101, 102], "High": [101, 104, 108], "Low": [98, 100, 101],
         "Close": [100, 103, 107], "Volume": [10, 20, 30]}, index=index,
    )

    class Ticker:
        def history(self, **kwargs):
            assert kwargs["interval"] == "1m"
            assert kwargs["auto_adjust"] is False
            return frame

    quote = market.YahooProvider(lambda symbol: Ticker()).quote("TEST.NS")
    assert quote == {
        "timestamp": index[-1].to_pydatetime(), "open": 101.0, "high": 108.0,
        "low": 100.0, "close": 100.0, "ltp": 107.0, "volume": 50,
    }
