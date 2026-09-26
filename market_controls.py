"""REST-only local HIJACK controls for LTP and candles (/local/v1/market).

Local test controls: they change only this server's SQLite state and never
Angel One, Yahoo Finance, the instrument master or broker orders.
"""
import asyncio
from datetime import datetime
import math
import re

from fastapi import APIRouter, Request

from auth import active_session, payload
from common import connect, fail, failed, ok, record_audit
import instrument_master
from market import (
    IST, _hijack_quote, candle_timestamp, delete_candle, delete_candles, hijack_for,
    mapping_for, override_candles, save_candle, save_override, clear_override,
)
from server_config import effective_source, source


router = APIRouter(prefix="/local/v1/market", tags=["local market controls"])
EXCHANGE_RE = re.compile(r"[A-Z]{2,10}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
QUOTE_FIELDS = ("open", "high", "low", "close")


class ControlError(Exception):
    def __init__(self, message, errorcode="AB1004", status_code=400):
        super().__init__(message)
        self.errorcode, self.status_code = errorcode, status_code


def finite(value, name):
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        raise ControlError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ControlError(f"{name} must be a number") from None
    if not math.isfinite(result):
        raise ControlError(f"{name} must be finite")
    return result


def positive(value, name):
    result = finite(value, name)
    if result <= 0:
        raise ControlError(f"{name} must be greater than zero")
    return result


def volume(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ControlError("volume must be a non-negative whole number")
    try:
        result = float(value) if not isinstance(value, int) else value
    except (TypeError, ValueError):
        raise ControlError("volume must be a non-negative whole number") from None
    if not math.isfinite(result) or result < 0 or result != int(result):
        raise ControlError("volume must be a non-negative whole number")
    return int(result)


def instant(value, name):
    """ISO-8601 time, normalised to a naive Asia/Kolkata minute."""
    if not isinstance(value, str) or not value.strip():
        raise ControlError(f"{name} must be an ISO-8601 timestamp")
    try:
        return datetime.fromisoformat(candle_timestamp(value.strip()))
    except ValueError:
        raise ControlError(f"{name} must be an ISO-8601 timestamp") from None


def instrument(data):
    """(exchange, token, trading symbol) for the effective market source."""
    exchange = str(data.get("exchange") or "").strip().upper()
    token = str(data.get("symboltoken") or "").strip()
    if not EXCHANGE_RE.fullmatch(exchange) or not TOKEN_RE.fullmatch(token):
        raise ControlError("exchange and symboltoken are required")
    known = instrument_master.lookup(exchange, token)
    mapped = mapping_for(exchange, token)
    selected = effective_source("market_data_source")
    unknown = (selected == "yahoo" and mapped is None) or (
        selected == "angelone" and known is None and instrument_master.loaded())
    if unknown:
        raise ControlError("Failed to get symbol details", "AB1018")
    symbol = (known or {}).get("symbol") or (mapped["tradingsymbol"] if mapped else "")
    return exchange, token, symbol


def candle_row(row):
    stamp = datetime.fromisoformat(row["timestamp"]).replace(tzinfo=IST).isoformat()
    return [stamp, row["open"], row["high"], row["low"], row["close"], row["volume"]]


def state(exchange, token, symbol):
    override = hijack_for(exchange, token)
    with connect() as conn:
        candles = conn.execute(
            "SELECT COUNT(*) FROM market_override_candles WHERE exchange=? AND symboltoken=?",
            (exchange, token),
        ).fetchone()[0]
    provider = effective_source("market_data_source") or "dummy"
    quote = None
    if override:
        values = _hijack_quote(override)
        quote = {key: values[key] for key in ("ltp", *QUOTE_FIELDS, "volume")}
    return {
        "exchange": exchange, "symboltoken": token, "tradingsymbol": symbol,
        "hijack": override is not None, "source": "HIJACK" if override else provider.upper(),
        "provider": provider, "configured_provider": source("market_data_source") or "dummy",
        "override": {key: override[key] for key in ("ltp", *QUOTE_FIELDS, "volume")} if override else None,
        "quote": quote, "overridden_candles": candles,
    }


def audit(request, action, detail):
    # Only instrument ids and prices: never tokens, headers or credentials.
    with connect() as conn:
        record_audit(conn, action, detail, request.state.control_client)


async def control(request, change, query=False):
    """Authenticate, parse and run one control; returns the SmartAPI JSON envelope."""
    session = active_session(request)
    if session is None:
        return failed("Invalid or expired token", 403)
    request.state.control_client = session["client_code"]
    data = dict(request.query_params) if query else await payload(request)
    try:
        return ok(await asyncio.to_thread(lambda: change(request, data, *instrument(data))))
    except ControlError as exc:
        return fail(str(exc), exc.errorcode, exc.status_code)


def enabled(exchange, token):
    override = hijack_for(exchange, token)
    if override is None:
        raise ControlError("HIJACK is not enabled for this instrument")
    return override


def set_ltp(request, exchange, token, symbol, ltp, action, detail):
    override = enabled(exchange, token)
    ltp = round(ltp, 4)
    if not math.isfinite(ltp) or ltp <= 0:
        raise ControlError("Resulting LTP must be greater than zero")
    values = {key: override[key] for key in (*QUOTE_FIELDS, "volume")}
    save_override(exchange, token, {**values, "ltp": ltp})
    audit(request, action, f"{exchange}:{token} {detail} ltp={ltp}")
    return state(exchange, token, symbol)


def current_ltp(exchange, token):
    return _hijack_quote(enabled(exchange, token))["ltp"]


@router.get("/state")
async def read_state(request: Request):
    return await control(request, lambda _, data, *item: state(*item), query=True)


@router.post("/hijack")
async def enable_hijack(request: Request):
    def change(request, data, exchange, token, symbol):
        values = {"ltp": positive(data.get("ltp"), "ltp"), "volume": volume(data.get("volume"))}
        values.update({key: positive(data[key], key) for key in QUOTE_FIELDS
                       if data.get(key) not in (None, "")})
        save_override(exchange, token, values)
        audit(request, "market.hijack_enabled",
              f"{exchange}:{token} " + " ".join(f"{k}={v}" for k, v in values.items() if v is not None))
        return state(exchange, token, symbol)
    return await control(request, change)


@router.post("/ltp/set")
async def set_price(request: Request):
    return await control(request, lambda request, data, *item: set_ltp(
        request, *item, positive(data.get("ltp"), "ltp"), "market.ltp_set", "set"))


@router.post("/ltp/step")
async def step_price(request: Request):
    def change(request, data, exchange, token, symbol):
        step = finite(data.get("step"), "step")
        return set_ltp(request, exchange, token, symbol, current_ltp(exchange, token) + step,
                       "market.ltp_step", f"step={step}")
    return await control(request, change)


@router.post("/ltp/percent")
async def percent_price(request: Request):
    def change(request, data, exchange, token, symbol):
        percent = finite(data.get("percent"), "percent")
        return set_ltp(request, exchange, token, symbol,
                       current_ltp(exchange, token) * (1 + percent / 100),
                       "market.ltp_percent", f"percent={percent}")
    return await control(request, change)


@router.post("/clear")
async def clear_hijack(request: Request):
    def change(request, data, exchange, token, symbol):
        clear_override(exchange, token)
        audit(request, "market.hijack_cleared", f"{exchange}:{token}")
        return state(exchange, token, symbol)
    return await control(request, change)


@router.post("/candles")
async def put_candle(request: Request):
    def change(request, data, exchange, token, symbol):
        stamp = instant(data.get("timestamp"), "timestamp")
        values = {key: positive(data.get(key), key) for key in QUOTE_FIELDS}
        if data.get("volume") in (None, ""):
            raise ControlError("volume must be a non-negative whole number")
        values["volume"] = volume(data.get("volume"))
        if not values["low"] <= min(values["open"], values["close"]) <= max(
                values["open"], values["close"]) <= values["high"]:
            raise ControlError("Candle must satisfy low <= open, close <= high")
        save_candle(exchange, token, stamp.isoformat(), values)
        audit(request, "market.candle_saved", f"{exchange}:{token} {stamp.isoformat()}")
        return {**state(exchange, token, symbol),
                "candle": candle_row({"timestamp": stamp.isoformat(), **values})}
    return await control(request, change)


@router.get("/candles")
async def list_candles(request: Request):
    def change(request, data, exchange, token, symbol):
        start = instant(data["fromdate"], "fromdate") if data.get("fromdate") else datetime.min
        end = instant(data["todate"], "todate") if data.get("todate") else datetime.max
        if start > end:
            raise ControlError("fromdate must not be after todate")
        rows = override_candles(exchange, token, start, end) or []
        return {**state(exchange, token, symbol), "candles": rows}
    return await control(request, change, query=True)


@router.post("/candles/delete")
async def remove_candle(request: Request):
    def change(request, data, exchange, token, symbol):
        stamp = instant(data.get("timestamp"), "timestamp")
        if not delete_candle(exchange, token, stamp.isoformat()):
            raise ControlError("Candle not found", status_code=404)
        audit(request, "market.candle_deleted", f"{exchange}:{token} {stamp.isoformat()}")
        return state(exchange, token, symbol)
    return await control(request, change)


@router.post("/candles/clear")
async def clear_candles(request: Request):
    def change(request, data, exchange, token, symbol):
        deleted = delete_candles(exchange, token)
        audit(request, "market.candles_cleared", f"{exchange}:{token} deleted={deleted}")
        return {**state(exchange, token, symbol), "deleted": deleted}
    return await control(request, change)
