from datetime import datetime

import pandas as pd
import pytest

import market
import orders


def test_order_field_validation():
    base = {
        "variety": "NORMAL", "exchange": "NSE", "tradingsymbol": "SBIN-EQ",
        "symboltoken": "3045", "transactiontype": "BUY", "ordertype": "LIMIT",
        "producttype": "DELIVERY", "duration": "DAY", "price": "100", "quantity": "1",
    }
    for field in ("variety", "producttype", "duration"):
        with pytest.raises(ValueError):
            orders.parse_order({**base, field: "INVALID"})


def test_cache_uses_fresh_value_before_refetch():
    cache = market.LastKnownCache()
    cache.put("x", {"ltp": 100})
    assert cache.get("x", 5) == {"ltp": 100}


@pytest.mark.parametrize("interval", ["THREE_MINUTE", "TEN_MINUTE"])
def test_resample_handles_tz_aware_yahoo_index(interval):
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
