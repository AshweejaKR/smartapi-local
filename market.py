"""SmartAPI market data with Yahoo and per-symbol HIJACK support."""
from collections import OrderedDict
import asyncio
from copy import deepcopy
import csv
import json
from datetime import datetime, timedelta
import math
import os
from pathlib import Path
import threading
import time
from zoneinfo import ZoneInfo

from fastapi import Request
import yfinance as yf

from auth import active_session, payload
from common import connect, fail, failed, ok
import instrument_master
from server_config import effective_source


IST = ZoneInfo("Asia/Kolkata")
# Tracked catalog of Phase 1 VERIFIED Yahoo symbols, keyed by exchange and Angel
# trading symbol. Tokens come from the current instrument master, never the catalog.
CATALOG_PATH = Path(__file__).parent / "yahoo_catalog.csv"
YAHOO_EXCHANGES = {"NSE", "BSE"}
# Rows seeded automatically before Phase 2; removed once so only VERIFIED
# catalog rows are mapped automatically.
LEGACY_SEEDS = (
    ("NSE", "SBIN-EQ", "3045", "SBIN.NS"),
    ("NSE", "RELIANCE-EQ", "2885", "RELIANCE.NS"),
    ("NSE", "NIFTYBEES-EQ", "10576", "NIFTYBEES.NS"),
    ("BSE", "NIFTYBEES", "590103", "NIFTYBEES.BO"),
    ("NSE", "NIFTY", "99926000", "^NSEI"),
)
INTERVALS = {
    "ONE_MINUTE": ("1m", None, timedelta(minutes=1)),
    "THREE_MINUTE": ("1m", "3min", timedelta(minutes=3)),
    "FIVE_MINUTE": ("5m", None, timedelta(minutes=5)),
    "TEN_MINUTE": ("5m", "10min", timedelta(minutes=10)),
    "FIFTEEN_MINUTE": ("15m", None, timedelta(minutes=15)),
    "THIRTY_MINUTE": ("30m", None, timedelta(minutes=30)),
    "ONE_HOUR": ("60m", None, timedelta(hours=1)),
    "ONE_DAY": ("1d", None, timedelta(days=1)),
}


class MarketDataError(Exception):
    def __init__(self, message, errorcode="AB2001", status_code=400):
        super().__init__(message)
        self.errorcode = errorcode
        self.status_code = status_code


class LastKnownCache:
    def __init__(self, limit=128):
        self.limit = limit
        self.values = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key, max_age=None):
        with self.lock:
            saved_at, value = self.values[key]
            if max_age is not None and time.monotonic() - saved_at > max_age:
                raise KeyError(key)
            self.values.move_to_end(key)
            return deepcopy(value)

    def put(self, key, value):
        with self.lock:
            self.values[key] = (time.monotonic(), deepcopy(value))
            self.values.move_to_end(key)
            while len(self.values) > self.limit:
                self.values.popitem(last=False)

    def clear(self):
        with self.lock:
            self.values.clear()

    def drop(self, exchange, symboltoken):
        """Forget cached quotes and candles for one instrument."""
        target = (str(exchange).upper(), str(symboltoken))
        with self.lock:
            for key in [key for key in self.values if tuple(key[1:3]) == target]:
                del self.values[key]


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Yahoo returned a non-finite value")
    return round(result, 4)


class YahooProvider:
    def __init__(self, ticker_factory=None):
        self.ticker_factory = ticker_factory or yf.Ticker

    def history(self, yahoo_symbol, **kwargs):
        frame = self.ticker_factory(yahoo_symbol).history(
            auto_adjust=False, actions=False, timeout=8, raise_errors=True, **kwargs
        )
        if frame is None or frame.empty:
            raise ValueError(f"No Yahoo data for {yahoo_symbol}")
        return frame

    def quote(self, yahoo_symbol):
        frame = self.history(yahoo_symbol, period="5d", interval="1m")
        frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
        if frame.empty:
            raise ValueError(f"No Yahoo prices for {yahoo_symbol}")
        timestamp = frame.index[-1]
        day = frame[frame.index.date == timestamp.date()]
        prior = frame[frame.index.date < timestamp.date()]
        close = prior.iloc[-1]["Close"] if not prior.empty else day.iloc[-1]["Close"]
        return {
            "timestamp": timestamp.to_pydatetime(),
            "open": number(day.iloc[0]["Open"]),
            "high": number(day["High"].max()),
            "low": number(day["Low"].min()),
            "close": number(close),
            "ltp": number(day.iloc[-1]["Close"]),
            "volume": int(day["Volume"].fillna(0).sum()),
        }

    def candles(self, yahoo_symbol, interval, start, end):
        yahoo_interval, resample, step = INTERVALS[interval]
        frame = self.history(
            yahoo_symbol, start=start, end=end + step, interval=yahoo_interval
        )
        if resample:
            origin = start.replace(tzinfo=frame.index.tz) if frame.index.tz is not None and start.tzinfo is None else start
            frame = frame.resample(resample, origin=origin).agg(
                {"Open": "first", "High": "max", "Low": "min",
                 "Close": "last", "Volume": "sum"}
            )
        frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
        rows = []
        for timestamp, row in frame.iterrows():
            stamp = timestamp.to_pydatetime()
            stamp = stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)
            rows.append([
                stamp.isoformat(), number(row["Open"]), number(row["High"]),
                number(row["Low"]), number(row["Close"]),
                int(0 if math.isnan(float(row["Volume"])) else row["Volume"]),
            ])
        if not rows:
            raise ValueError(f"No Yahoo candles for {yahoo_symbol}")
        return rows


class DummyProvider:
    """Fixed offline data for fully local runs."""
    price = 100.05

    def quote(self):
        return {
            "timestamp": datetime.now(IST), "open": self.price, "high": self.price,
            "low": self.price, "close": self.price, "ltp": self.price, "volume": 0,
        }

    def candles(self, interval, end):
        step = INTERVALS[interval][2]
        end = end.replace(tzinfo=IST)
        return [[
            (end - step * (24 - index)).isoformat(), self.price, self.price,
            self.price, self.price, 0,
        ] for index in range(25)]


PROVIDER = YahooProvider()
DUMMY_PROVIDER = DummyProvider()
CACHE = LastKnownCache()


def init_market():
    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS symbol_mappings ("
            "exchange TEXT NOT NULL, tradingsymbol TEXT NOT NULL, "
            "symboltoken TEXT NOT NULL, yahoo_symbol TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 1, "
            "PRIMARY KEY(exchange, symboltoken), UNIQUE(exchange, tradingsymbol))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS market_overrides ("
            "exchange TEXT NOT NULL, symboltoken TEXT NOT NULL, "
            "mode TEXT NOT NULL DEFAULT 'YAHOO', ltp REAL, volume INTEGER, "
            "open REAL, high REAL, low REAL, close REAL, "
            "PRIMARY KEY(exchange, symboltoken))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS market_override_candles ("
            "exchange TEXT NOT NULL, symboltoken TEXT NOT NULL, timestamp TEXT NOT NULL, "
            "open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, "
            "close REAL NOT NULL, volume INTEGER NOT NULL, "
            "PRIMARY KEY(exchange, symboltoken, timestamp))"
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(symbol_mappings)")}
        if "origin" not in columns:
            conn.execute(
                "ALTER TABLE symbol_mappings ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'"
            )
            conn.executemany(
                "DELETE FROM symbol_mappings WHERE exchange=? AND tradingsymbol=? "
                "AND symboltoken=? AND yahoo_symbol=?", LEGACY_SEEDS,
            )
        sync_catalog_mappings(conn)
    if sync_catalog_mappings not in instrument_master.AFTER_REFRESH:
        instrument_master.AFTER_REFRESH.append(sync_catalog_mappings)


def load_catalog(path=None):
    """(exchange, tradingsymbol, yahoo_symbol) rows; cash NSE/BSE only."""
    path = Path(path or CATALOG_PATH)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            (row["exchange"].strip().upper(), row["tradingsymbol"].strip(), row["yahoo_symbol"].strip())
            for row in csv.DictReader(handle)
            if row.get("exchange", "").strip().upper() in YAHOO_EXCHANGES
            and row.get("tradingsymbol", "").strip() and row.get("yahoo_symbol", "").strip()
        ]


def sync_catalog_mappings(conn):
    """Rebuild catalog mappings with the current master token for each symbol.

    Unknown or ambiguous symbols stay unmapped. Manual mappings win on conflict.
    """
    conn.execute("DELETE FROM symbol_mappings WHERE origin='catalog'")
    for exchange, symbol, yahoo_symbol in load_catalog():
        tokens = conn.execute(
            "SELECT token, symbol FROM instruments WHERE exchange=? AND symbol=?",
            (exchange, symbol),
        ).fetchall()
        if len(tokens) == 1:
            conn.execute(
                "INSERT OR IGNORE INTO symbol_mappings "
                "(exchange, tradingsymbol, symboltoken, yahoo_symbol, origin) "
                "VALUES (?, ?, ?, ?, 'catalog')",
                (exchange, tokens[0]["symbol"], tokens[0]["token"], yahoo_symbol),
            )


def upsert_mapping(exchange, tradingsymbol, symboltoken, yahoo_symbol, enabled=True):
    """Create or update a mapping without requiring the Admin UI."""
    values = (exchange.upper(), tradingsymbol.upper(), str(symboltoken), yahoo_symbol)
    if not all(values):
        raise ValueError("All symbol mapping fields are required")
    with connect() as conn:
        conn.execute(
            "INSERT INTO symbol_mappings "
            "(exchange, tradingsymbol, symboltoken, yahoo_symbol, enabled) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(exchange, symboltoken) DO UPDATE SET "
            "tradingsymbol=excluded.tradingsymbol, yahoo_symbol=excluded.yahoo_symbol, "
            "enabled=excluded.enabled, origin='manual'",
            (*values, int(enabled)),
        )


def mapping_for(exchange, symboltoken, tradingsymbol=None):
    if not exchange or not symboltoken:
        return None
    sql = (
        "SELECT * FROM symbol_mappings WHERE exchange=? AND symboltoken=? "
        "AND enabled=1"
    )
    params = [str(exchange).upper(), str(symboltoken)]
    if tradingsymbol:
        sql += " AND UPPER(tradingsymbol)=?"
        params.append(str(tradingsymbol).upper())
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def list_mappings():
    with connect() as conn:
        return conn.execute(
            "SELECT m.*, COALESCE(o.mode, 'YAHOO') AS mode, "
            "o.ltp, o.volume, o.open, o.high, o.low, o.close "
            "FROM symbol_mappings m LEFT JOIN market_overrides o "
            "ON o.exchange=m.exchange AND o.symboltoken=m.symboltoken "
            "WHERE m.enabled=1 ORDER BY m.exchange, m.tradingsymbol"
        ).fetchall()


def override_for(exchange, symboltoken):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM market_overrides WHERE exchange=? AND symboltoken=?",
            (str(exchange).upper(), str(symboltoken)),
        ).fetchone()


def hijack_for(exchange, symboltoken):
    """The active HIJACK row for one instrument, or None."""
    row = override_for(exchange, symboltoken)
    return row if row is not None and row["mode"] == "HIJACK" else None


def save_override(exchange, symboltoken, values):
    """Enable or update HIJACK; callers validate values (positive, finite)."""
    exchange, symboltoken = str(exchange).upper(), str(symboltoken)
    with connect() as conn:
        conn.execute(
            "INSERT INTO market_overrides "
            "(exchange, symboltoken, mode, ltp, volume, open, high, low, close) "
            "VALUES (?, ?, 'HIJACK', ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(exchange, symboltoken) DO UPDATE SET mode='HIJACK', "
            "ltp=excluded.ltp, volume=excluded.volume, open=excluded.open, "
            "high=excluded.high, low=excluded.low, close=excluded.close",
            (exchange, symboltoken, values.get("ltp"), values.get("volume"), values.get("open"),
             values.get("high"), values.get("low"), values.get("close")),
        )
    CACHE.drop(exchange, symboltoken)


def clear_override(exchange, symboltoken):
    """Return to the configured provider; saved candles stay but are ignored."""
    with connect() as conn:
        conn.execute("DELETE FROM market_overrides WHERE exchange=? AND symboltoken=?",
                     (str(exchange).upper(), str(symboltoken)))
    CACHE.drop(exchange, symboltoken)


def list_hijacks():
    """Active hijacks with their saved-candle counts, for the read-only admin view."""
    with connect() as conn:
        return conn.execute(
            "SELECT o.*, COALESCE(i.symbol, m.tradingsymbol, '') AS tradingsymbol, "
            "(SELECT COUNT(*) FROM market_override_candles c WHERE c.exchange=o.exchange "
            "AND c.symboltoken=o.symboltoken) AS candles FROM market_overrides o "
            "LEFT JOIN instruments i ON i.exchange=o.exchange AND i.token=o.symboltoken "
            "LEFT JOIN symbol_mappings m ON m.exchange=o.exchange AND m.symboltoken=o.symboltoken "
            "WHERE o.mode='HIJACK' ORDER BY o.exchange, o.symboltoken"
        ).fetchall()


def candle_timestamp(value):
    stamp = datetime.fromisoformat(str(value))
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(IST).replace(tzinfo=None)
    return stamp.replace(second=0, microsecond=0).isoformat()


def save_candle(exchange, symboltoken, timestamp, values):
    timestamp = candle_timestamp(timestamp)
    CACHE.drop(exchange, symboltoken)
    with connect() as conn:
        conn.execute(
            "INSERT INTO market_override_candles "
            "(exchange, symboltoken, timestamp, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(exchange, symboltoken, timestamp) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, "
            "close=excluded.close, volume=excluded.volume",
            (str(exchange).upper(), str(symboltoken), timestamp,
             values["open"], values["high"], values["low"], values["close"],
             values["volume"]),
        )


def delete_candle(exchange, symboltoken, timestamp):
    """Delete one saved candle; returns the number of rows removed."""
    timestamp = candle_timestamp(timestamp)
    CACHE.drop(exchange, symboltoken)
    with connect() as conn:
        return conn.execute(
            "DELETE FROM market_override_candles WHERE exchange=? AND symboltoken=? "
            "AND timestamp=?",
            (str(exchange).upper(), str(symboltoken), timestamp),
        ).rowcount


def delete_candles(exchange, symboltoken):
    CACHE.drop(exchange, symboltoken)
    with connect() as conn:
        return conn.execute(
            "DELETE FROM market_override_candles WHERE exchange=? AND symboltoken=?",
            (str(exchange).upper(), str(symboltoken)),
        ).rowcount


def override_candles(exchange, symboltoken, start, end):
    with connect() as conn:
        rows = conn.execute(
            "SELECT timestamp, open, high, low, close, volume "
            "FROM market_override_candles WHERE exchange=? AND symboltoken=? "
            "ORDER BY timestamp",
            (str(exchange).upper(), str(symboltoken)),
        ).fetchall()
    if not rows:
        return None
    result = []
    for row in rows:
        stamp = datetime.fromisoformat(row["timestamp"])
        if start <= stamp.replace(tzinfo=None) <= end:
            result.append([
                (stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)).isoformat(),
                row["open"], row["high"], row["low"],
                row["close"], row["volume"],
            ])
    return result


def cache_ttl():
    try:
        return max(0.0, float(os.getenv("SMARTAPI_MARKET_CACHE_TTL_SECONDS", "5")))
    except ValueError:
        return 5.0


def cached(key, fetch):
    try:
        return CACHE.get(key, cache_ttl())
    except KeyError:
        pass
    try:
        value = fetch()
    except Exception as exc:
        try:
            return CACHE.get(key)
        except KeyError:
            raise MarketDataError(
                "Market data is temporarily unavailable", "AB2001", 503
            ) from exc
    CACHE.put(key, value)
    return value


def _item(exchange, symboltoken, tradingsymbol=None):
    item = mapping_for(exchange, symboltoken, tradingsymbol)
    if item is None:
        if effective_source("market_data_source") is None:
            known = instrument_master.lookup(exchange, symboltoken) or {}
            return {
                "exchange": str(exchange).upper(), "symboltoken": str(symboltoken),
                "tradingsymbol": known.get("symbol") or str(tradingsymbol or "").upper(),
                "yahoo_symbol": "",
            }
        raise MarketDataError("Failed to get symbol details", "AB1018")
    return item


def _angel_quote(exchange, symboltoken, tradingsymbol, client_code=None):
    import angelone_proxy

    try:
        reply = angelone_proxy.broker_for_client(client_code).forward(
            "/rest/secure/angelbroking/order/v1/getLtpData",
            {"exchange": exchange, "tradingsymbol": tradingsymbol, "symboltoken": symboltoken},
        )
        result = reply.json()
        values = result.get("data") if isinstance(result, dict) else None
        if not result or not result.get("status") or not isinstance(values, dict):
            raise angelone_proxy.AngelOneError("Angel One market request failed")
    except angelone_proxy.AngelOneLoginError:
        raise
    except angelone_proxy.AngelOneError as exc:
        raise MarketDataError(str(exc), "AB2001", 503) from exc
    return {
        "exchange": str(values.get("exchange", exchange)).upper(),
        "tradingsymbol": str(values.get("tradingsymbol", tradingsymbol)).upper(),
        "symboltoken": str(values.get("symboltoken", symboltoken)),
        **{key: values.get(key, 0) for key in ("open", "high", "low", "close", "ltp", "volume")},
        "timestamp": datetime.now(IST), "source": "ANGELONE",
    }


def _hijack_quote(override):
    ltp = override["ltp"] if override["ltp"] is not None else override["close"]
    if ltp is None:
        raise MarketDataError("HIJACK LTP is not configured", "AB2001", 503)
    values = {key: ltp if override[key] is None else override[key]
              for key in ("open", "high", "low", "close")}
    return {
        **values, "ltp": ltp, "volume": override["volume"] or 0,
        "timestamp": datetime.now(IST), "source": "HIJACK",
    }


def _hijack_item(exchange, symboltoken, tradingsymbol=None):
    """Display fields for a hijacked instrument: the master symbol when known."""
    known = instrument_master.lookup(exchange, symboltoken) or {}
    mapped = mapping_for(exchange, symboltoken)
    return {"exchange": str(exchange).upper(), "symboltoken": str(symboltoken),
            "tradingsymbol": known.get("symbol") or (mapped["tradingsymbol"] if mapped else "")
            or str(tradingsymbol or "").upper()}


def get_quote(exchange, symboltoken, tradingsymbol=None, client_code=None):
    """Quote from HIJACK, else the effective source: DUMMY, ANGELONE or YAHOO."""
    from angelone_proxy import AngelOneLoginError

    override = hijack_for(exchange, symboltoken)
    if override:  # a local test control: overrides every configured source
        return {**_hijack_item(exchange, symboltoken, tradingsymbol), **_hijack_quote(override)}
    selected = effective_source("market_data_source")
    if selected is None:
        item = _item(exchange, symboltoken, tradingsymbol)
        return {**dict(item), **DUMMY_PROVIDER.quote(), "source": "DUMMY"}
    if selected == "angelone":
        try:
            return _angel_quote(exchange, symboltoken, tradingsymbol or "", client_code)
        except AngelOneLoginError as exc:
            # The internal login failed: market data is now Yahoo for this process.
            if effective_source("market_data_source") != "yahoo":
                raise MarketDataError(str(exc), "AB2001", 503) from exc
    item = _item(exchange, symboltoken, tradingsymbol)
    key = ("quote", item["exchange"], item["symboltoken"], item["yahoo_symbol"])
    quote = {**cached(key, lambda: PROVIDER.quote(item["yahoo_symbol"])), "source": "YAHOO"}
    return {**dict(item), **quote}


def get_candles(exchange, symboltoken, interval, start, end):
    override = hijack_for(exchange, symboltoken)
    if override:
        # Saved override candles only; never merged with provider candles.
        rows = override_candles(exchange, symboltoken, start, end)
        if rows:
            return rows
        quote = _hijack_quote(override)
        stamp = start.replace(tzinfo=IST).isoformat()
        return [[stamp, *(quote[key] for key in ("open", "high", "low", "close", "volume"))]]
    if effective_source("market_data_source") is None:
        return DUMMY_PROVIDER.candles(interval, end)
    # angelone candle requests are proxied in app.dispatch_rest and never reach here.
    item = _item(exchange, symboltoken)
    key = ("candles", item["exchange"], item["symboltoken"], item["yahoo_symbol"],
           interval, start.isoformat(), end.isoformat())
    return cached(key, lambda: PROVIDER.candles(item["yahoo_symbol"], interval, start, end))


def hijacked_request(path, data):
    """Split a broker-bound market request: (data to forward or None, merge or None).

    None as data means every requested instrument is hijacked: serve it locally.
    """
    if path.endswith(("/getLtpData", "/getCandleData")):
        return (None, None) if hijack_for(data.get("exchange"), data.get("symboltoken")) else (data, None)
    tokens = data.get("exchangeTokens")
    if not path.endswith("/quote") or not isinstance(tokens, dict):
        return data, None
    local, remote = {}, {}
    for exchange, values in tokens.items():
        if not isinstance(values, list):
            return data, None
        for token in values:
            (local if hijack_for(exchange, token) else remote).setdefault(exchange, []).append(token)
    if not local:
        return data, None
    if not remote:
        return None, None
    mode = str(data.get("mode", "")).upper()

    def merge(reply):
        body = reply.json()
        if not (isinstance(body, dict) and body.get("status") and isinstance(body.get("data"), dict)):
            return reply
        rows = [quote_view(get_quote(exchange, token), mode)
                for exchange, values in local.items() for token in values]
        body["data"]["fetched"] = [*(body["data"].get("fetched") or []), *rows]
        return type(reply)(reply.status_code, json.dumps(body).encode(), reply.content_type)

    return {**data, "exchangeTokens": remote}, merge


def get_effective_ltp(exchange, symboltoken, tradingsymbol=None, client_code=None):
    return get_quote(exchange, symboltoken, tradingsymbol, client_code)["ltp"]


def instrument_known(exchange, symboltoken, tradingsymbol):
    """Local-order symbol check against the effective market source."""
    if effective_source("market_data_source") == "angelone" and instrument_master.loaded():
        row = instrument_master.lookup(exchange, symboltoken)
        return row is not None and row["symbol"].upper() == str(tradingsymbol).upper()
    if effective_source("market_data_source") == "angelone":
        return True  # no cached master: the broker validates the symbol
    return mapping_for(exchange, symboltoken, tradingsymbol) is not None


def market_error(exc):
    return fail(str(exc), exc.errorcode, exc.status_code)


def secured(request):
    return active_session(request) is not None


async def ltp_data(request: Request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    if not all(data.get(key) for key in ("exchange", "tradingsymbol", "symboltoken")):
        return market_error(MarketDataError("Invalid LTP request", "AB1004"))
    try:
        quote = await asyncio.to_thread(
            get_quote, data.get("exchange"), data.get("symboltoken"), data.get("tradingsymbol")
        )
    except MarketDataError as exc:
        return market_error(exc)
    return ok({
        "exchange": quote["exchange"], "tradingsymbol": quote["tradingsymbol"],
        "symboltoken": quote["symboltoken"], "open": quote["open"],
        "high": quote["high"], "low": quote["low"], "close": quote["close"],
        "ltp": quote["ltp"],
    })


def quote_view(quote, mode):
    result = {
        "exchange": quote["exchange"], "tradingSymbol": quote["tradingsymbol"],
        "symbolToken": quote["symboltoken"], "ltp": quote["ltp"],
    }
    if mode in {"OHLC", "FULL"}:
        result.update({key: quote[key] for key in ("open", "high", "low", "close")})
    if mode == "FULL":
        change = round(quote["ltp"] - quote["close"], 4)
        percent = round(change * 100 / quote["close"], 4) if quote["close"] else 0
        stamp = quote["timestamp"].strftime("%d-%b-%Y %H:%M:%S")
        result.update({
            "lastTradeQty": 0, "exchFeedTime": stamp, "exchTradeTime": stamp,
            "netChange": change, "percentChange": percent,
            "avgPrice": round((quote["open"] + quote["high"] + quote["low"] + quote["ltp"]) / 4, 4),
            "tradeVolume": quote["volume"], "opnInterest": 0,
            "lowerCircuit": 0, "upperCircuit": 0, "totBuyQuan": 0,
            "totSellQuan": 0, "52WeekLow": quote["low"],
            "52WeekHigh": quote["high"], "depth": {"buy": [], "sell": []},
        })
    return result


async def market_data(request: Request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    mode = str(data.get("mode", "")).upper()
    exchange_tokens = data.get("exchangeTokens")
    if (mode not in {"LTP", "OHLC", "FULL"} or not isinstance(exchange_tokens, dict)
            or not exchange_tokens
            or any(not isinstance(tokens, list) for tokens in exchange_tokens.values())):
        return market_error(MarketDataError("Invalid market data request", "AB1004"))
    requested = sum(len(tokens) for tokens in exchange_tokens.values())
    if requested == 0 or requested > 50:
        return market_error(MarketDataError("A maximum of 50 symbols is allowed", "AB1004"))
    fetched, unfetched = [], []
    for exchange, tokens in exchange_tokens.items():
        for token in tokens:
            try:
                quote = await asyncio.to_thread(get_quote, exchange, token)
                fetched.append(quote_view(quote, mode))
            except MarketDataError as exc:
                unfetched.append({
                    "exchange": exchange, "symbolToken": str(token),
                    "message": str(exc), "errorCode": exc.errorcode,
                })
    return ok({"fetched": fetched, "unfetched": unfetched})


async def candle_data(request: Request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    interval = str(data.get("interval", "")).upper()
    try:
        start = datetime.strptime(data.get("fromdate", ""), "%Y-%m-%d %H:%M")
        end = datetime.strptime(data.get("todate", ""), "%Y-%m-%d %H:%M")
        if interval not in INTERVALS or start > end:
            raise ValueError
        rows = await asyncio.to_thread(get_candles, data.get("exchange"), data.get("symboltoken"), interval, start, end)
    except (TypeError, ValueError):
        return market_error(MarketDataError("Invalid candle request", "AB1004"))
    except MarketDataError as exc:
        return market_error(exc)
    return ok(rows)
