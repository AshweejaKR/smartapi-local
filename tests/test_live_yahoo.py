"""Optional external-provider smoke; the deterministic suite never needs Yahoo."""
from datetime import datetime, timedelta
import os

import pytest

import market


@pytest.mark.skipif(os.getenv("SMARTAPI_LIVE_YAHOO") != "1", reason="Set SMARTAPI_LIVE_YAHOO=1 for live Yahoo verification")
def test_live_yahoo_through_official_sdk(sdk_server, monkeypatch):
    monkeypatch.setattr(market, "PROVIDER", market.YahooProvider())
    market.CACHE.clear()
    quote = sdk_server.sdk.ltpData("NSE", "SBIN-EQ", "3045")
    assert quote["status"], quote
    assert quote["data"]["ltp"] > 0
    now = datetime.now(market.IST)
    candles = sdk_server.sdk.getCandleData({
        "exchange": "NSE", "symboltoken": "3045", "interval": "ONE_DAY",
        "fromdate": (now - timedelta(days=14)).strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    })
    assert candles["status"] and candles["data"], candles
    assert all(len(row) == 6 and row[4] > 0 for row in candles["data"])
