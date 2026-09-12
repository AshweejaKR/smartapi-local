"""Side-by-side Angel One SmartAPI compatibility runner.

By default this is read-only against the live broker.  The requested live
orders require both --execute-live-orders and --confirm-live-orders.  The
optional --provision-local-from-live switch creates/updates the matching local
simulator account, stores its API key in the simulator DB, and matches its
available cash to the live account for this run.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import math
from pathlib import Path
import sqlite3
import struct
import time
from typing import Any

from SmartApi import SmartConnect
from auth import password_hash

ROOT = Path(__file__).resolve().parent
DEFAULT_KEYS = ROOT / "angelone_keys.env"
DEFAULT_REPORT = ROOT / "logs" / "smartapi_comparison.json"
LOCAL_DB = ROOT / "smartapi_local.db"
LOCAL_ROOT = "http://127.0.0.1:8000"


def read_env(path: Path) -> dict[str, str]:
    result = {}
    for source in path.read_text(encoding="utf-8").splitlines():
        line = source.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip("\"'")
    return result


def make_totp(secret: str) -> str:
    """RFC 6238 six-digit TOTP without adding a dependency."""
    text = secret.upper().replace(" ", "")
    key = base64.b32decode(text + "=" * (-len(text) % 8))
    counter = int(time.time() // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def hide(value: Any) -> Any:
    sensitive = {"password", "totp", "apikey", "api_key", "jwtToken", "refreshToken",
                 "feedToken", "email", "mobileno"}
    if isinstance(value, dict):
        return {key: "<redacted>" if key in sensitive else hide(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [hide(item) for item in value]
    return value


def summarize(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {"type": type(response).__name__, "value": hide(response)}
    data = response.get("data")
    summary: dict[str, Any] = {"status": response.get("status"),
                               "errorcode": response.get("errorcode"),
                               "message": response.get("message"),
                               "data_type": type(data).__name__}
    if isinstance(data, dict):
        summary["data_keys"] = sorted(data)
        for key in ("ltp", "availablecash", "net", "clientcode"):
            if key in data:
                summary[key] = data[key]
    elif isinstance(data, list):
        summary["data_count"] = len(data)
        summary["row_keys"] = sorted(data[0]) if data and isinstance(data[0], dict) else None
    else:
        summary["data"] = data
    return hide(summary)


def signature(response: Any) -> tuple[Any, ...]:
    data = summarize(response)
    return (data.get("status"), data.get("errorcode"), data.get("data_type"),
            data.get("data_keys"), data.get("row_keys"))


def call(method, *args, **kwargs) -> Any:
    """Run each server call independently with the requested pacing."""
    time.sleep(1)
    try:
        return method(*args, **kwargs)
    except Exception as error:
        return {"status": False, "errorcode": "CLIENT_EXCEPTION", "data": None,
                "message": f"{type(error).__name__}: {error}"}
    finally:
        time.sleep(1)


class Report:
    def __init__(self, live: SmartConnect, local: SmartConnect):
        self.live, self.local = live, local
        self.items: list[dict[str, Any]] = []

    def pair(self, scenario: str, request: dict[str, Any], live_call, local_call) -> tuple[Any, Any]:
        live_response, local_response = call(live_call), call(local_call)
        self.items.append({"scenario": scenario, "request": hide(request),
                           "live": summarize(live_response), "local": summarize(local_response),
                           "responses": {"live": hide(live_response), "local": hide(local_response)},
                           "compatible_envelope": signature(live_response) == signature(local_response)})
        return live_response, local_response

    def snapshots(self, label: str) -> None:
        for method, name in (("orderBook", "order history"), ("position", "positions"),
                             ("holding", "holdings")):
            self.pair(f"{label}: {name}", {}, getattr(self.live, method), getattr(self.local, method))


def choose(response: dict[str, Any], exchange: str, preferred: str) -> dict[str, str]:
    rows = response.get("data") or []
    exact = next((item for item in rows if item.get("tradingsymbol") == preferred), None)
    item = exact or next((item for item in rows if "NIFTYBEES" in item.get("tradingsymbol", "")), None)
    if not response.get("status") or not item:
        raise RuntimeError(f"Could not resolve NIFTYBEES on {exchange}: {summarize(response)}")
    return {"exchange": str(item["exchange"]).upper(),
            "tradingsymbol": str(item["tradingsymbol"]).upper(),
            "symboltoken": str(item["symboltoken"])}


def order(instrument: dict[str, str], side: str, kind: str, quantity: int, price: str = "0") -> dict[str, str]:
    return {"variety": "NORMAL", **instrument, "transactiontype": side, "ordertype": kind,
            "producttype": "DELIVERY", "duration": "DAY", "price": price,
            "quantity": str(quantity)}


def provision_local(keys: dict[str, str], live_available_cash: float, database: Path) -> None:
    """Explicitly opt-in: configure one local account for identical credentials."""
    code = keys["CLIENT_ID"]
    with sqlite3.connect(database, timeout=10) as conn:
        conn.execute(
            "INSERT INTO users(client_code,password_hash,api_key,totp,name,email,mobile,enabled) "
            "VALUES (?,?,?,?,?,?,?,1) ON CONFLICT(client_code) DO UPDATE SET "
            "password_hash=excluded.password_hash,api_key=excluded.api_key,totp=excluded.totp,enabled=1",
            (code, password_hash(keys["PASSWORD"]), keys["API_KEY"], make_totp(keys["TOTP_SECRET"]),
             "Live comparison user", "comparison@local.invalid", "0000000000"))
        existing = conn.execute("SELECT COALESCE(used_funds, 0) FROM accounts WHERE client_code=?", (code,)).fetchone()
        used = float(existing[0]) if existing else 0
        conn.execute("INSERT INTO accounts(client_code,available_balance) VALUES (?,?) "
                     "ON CONFLICT(client_code) DO UPDATE SET available_balance=excluded.available_balance",
                     (code, live_available_cash + used))
        for exchange, symbol, token, yahoo in (("NSE", "NIFTYBEES-EQ", "10576", "NIFTYBEES.NS"),
                                               ("BSE", "NIFTYBEES", "590103", "NIFTYBEES.BO")):
            conn.execute("INSERT INTO symbol_mappings(exchange,tradingsymbol,symboltoken,yahoo_symbol,enabled) "
                         "VALUES (?,?,?,?,1) ON CONFLICT(exchange,symboltoken) DO UPDATE SET "
                         "tradingsymbol=excluded.tradingsymbol,yahoo_symbol=excluded.yahoo_symbol,enabled=1",
                         (exchange, symbol, token, yahoo))


def cancel_open(report: Report) -> None:
    for client in (report.live, report.local):
        book = call(client.orderBook)
        for entry in book.get("data") or []:
            if entry.get("status") in {"OPEN", "PENDING"}:
                call(client.cancelOrder, entry["orderid"], entry.get("variety", "NORMAL"))
    report.snapshots("after cancelling open orders")


def live_order_suite(report: Report, instrument: dict[str, str], ltp: float) -> None:
    limit = f"{math.floor(ltp * .97 * 100) / 100:.2f}"
    for side in ("BUY", "SELL"):
        for label, kind, quantity, price in (("market 200", "MARKET", 200, "0"),
                                              ("limit 200, 3% below LTP", "LIMIT", 200, limit),
                                              ("market 1", "MARKET", 1, "0"),
                                              ("limit 1, 3% below LTP", "LIMIT", 1, limit)):
            request = order(instrument, side, kind, quantity, price)
            report.pair(f"{side} {label}", request,
                        lambda r=request: report.live.placeOrderFullResponse(r),
                        lambda r=request: report.local.placeOrderFullResponse(r))
            if kind == "MARKET":
                time.sleep(1.2)
            report.snapshots(f"{side} {label}")
    cancel_open(report)


def invalid_order_suite(report: Report, instrument: dict[str, str]) -> None:
    attempts = [
        ("wrong symbol BUY", {**order(instrument, "BUY", "MARKET", 1), "tradingsymbol": "NOT-NIFTYBEES-EQ"}),
        ("wrong symbol SELL", {**order(instrument, "SELL", "MARKET", 1), "tradingsymbol": "NOT-NIFTYBEES-EQ"}),
        ("MCX exchange", {**order(instrument, "BUY", "MARKET", 1), "exchange": "MCX"}),
        ("BSE exchange with NSE symbol", {**order(instrument, "BUY", "MARKET", 1), "exchange": "BSE"}),
        ("zero quantity", order(instrument, "BUY", "MARKET", 0)),
    ]
    for label, request in attempts:
        report.pair(f"invalid order: {label}", request,
                    lambda r=request: report.live.placeOrderFullResponse(r),
                    lambda r=request: report.local.placeOrderFullResponse(r))
        report.snapshots(f"invalid order: {label}")


def candle_suite(report: Report, instrument: dict[str, str]) -> None:
    now = datetime.now().replace(second=0, microsecond=0)
    for label, interval, period in (("1m", "ONE_MINUTE", timedelta(days=1)),
                                    ("15m", "FIFTEEN_MINUTE", timedelta(days=5)),
                                    ("1D", "ONE_DAY", timedelta(days=30))):
        request = {"exchange": instrument["exchange"], "symboltoken": instrument["symboltoken"],
                   "interval": interval, "fromdate": (now - period).strftime("%Y-%m-%d %H:%M"),
                   "todate": now.strftime("%Y-%m-%d %H:%M")}
        report.pair(f"historical candles {label}", request,
                    lambda r=request: report.live.getCandleData(r),
                    lambda r=request: report.local.getCandleData(r))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys", type=Path, default=DEFAULT_KEYS)
    parser.add_argument("--local-root", default=LOCAL_ROOT)
    parser.add_argument("--local-db", type=Path, default=LOCAL_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--provision-local-from-live", action="store_true")
    parser.add_argument("--execute-live-orders", action="store_true")
    parser.add_argument("--confirm-live-orders", action="store_true")
    args = parser.parse_args()
    if args.execute_live_orders != args.confirm_live_orders:
        raise SystemExit("Live orders require both --execute-live-orders and --confirm-live-orders.")
    keys = read_env(args.keys)
    missing = {"API_KEY", "CLIENT_ID", "PASSWORD", "TOTP_SECRET"} - keys.keys()
    if missing:
        raise SystemExit(f"Missing credential fields: {', '.join(sorted(missing))}")
    code = make_totp(keys["TOTP_SECRET"])
    live = SmartConnect(api_key=keys["API_KEY"])
    local = SmartConnect(api_key=keys["API_KEY"] if args.provision_local_from_live else "DUMMY_API_KEY",
                         root=args.local_root)
    report = Report(live, local)

    # Required negative-login scenarios use fresh SDK objects so valid sessions remain untouched.
    bad_logins = (("wrong API key", "WRONG_API_KEY", keys["CLIENT_ID"], keys["PASSWORD"], code),
                  ("wrong username", keys["API_KEY"], "WRONG_USER", keys["PASSWORD"], code),
                  ("wrong password", keys["API_KEY"], keys["CLIENT_ID"], "WRONG_PASSWORD", code),
                  ("wrong TOTP", keys["API_KEY"], keys["CLIENT_ID"], keys["PASSWORD"], "000000"))
    for label, api_key, username, password, bad_code in bad_logins:
        report.pair(f"login: {label}", {"clientcode": username, "password": password, "totp": bad_code},
                    lambda: SmartConnect(api_key=api_key).generateSession(username, password, bad_code),
                    lambda: SmartConnect(api_key=api_key if args.provision_local_from_live else "DUMMY_API_KEY",
                                         root=args.local_root).generateSession(username, password, bad_code))

    live_login = call(live.generateSession, keys["CLIENT_ID"], keys["PASSWORD"], code)
    if not live_login.get("status"):
        raise SystemExit(f"Live login failed: {summarize(live_login)}")
    live_rms = call(live.rmsLimit)
    if args.provision_local_from_live:
        provision_local(keys, float((live_rms.get("data") or {}).get("availablecash", 0)), args.local_db)
    local_login = call(local.generateSession,
                       keys["CLIENT_ID"] if args.provision_local_from_live else "DUMMY001",
                       keys["PASSWORD"] if args.provision_local_from_live else "password",
                       make_totp(keys["TOTP_SECRET"]) if args.provision_local_from_live else "123456")
    report.items.append({"scenario": "login: correct credentials", "live": summarize(live_login),
                         "local": summarize(local_login),
                         "responses": {"live": hide(live_login), "local": hide(local_login)},
                         "compatible_envelope": signature(live_login) == signature(local_login)})
    if not local_login.get("status"):
        raise SystemExit(f"Local login failed: {summarize(local_login)}")

    report.pair("account details", {}, lambda: live.getProfile(live.refresh_token),
                lambda: local.getProfile(local.refresh_token))
    report.pair("balance", {}, live.rmsLimit, local.rmsLimit)
    live_nse_search, local_nse_search = report.pair("search NIFTYBEES NSE", {"exchange": "NSE", "searchscrip": "NIFTYBEES"},
                                                     lambda: live.searchScrip("NSE", "NIFTYBEES"),
                                                     lambda: local.searchScrip("NSE", "NIFTYBEES"))
    live_bse_search, local_bse_search = report.pair("search NIFTYBEES BSE", {"exchange": "BSE", "searchscrip": "NIFTYBEES"},
                                                     lambda: live.searchScrip("BSE", "NIFTYBEES"),
                                                     lambda: local.searchScrip("BSE", "NIFTYBEES"))
    instrument = choose(live_nse_search, "NSE", "NIFTYBEES-EQ")
    choose(local_nse_search, "NSE", "NIFTYBEES-EQ")
    bse = choose(live_bse_search, "BSE", "NIFTYBEES")
    choose(local_bse_search, "BSE", "NIFTYBEES")
    live_ltp, _ = report.pair("NIFTYBEES LTP NSE", instrument,
                              lambda: live.ltpData(**instrument), lambda: local.ltpData(**instrument))
    report.pair("NIFTYBEES LTP BSE", bse, lambda: live.ltpData(**bse), lambda: local.ltpData(**bse))
    ltp = float((live_ltp.get("data") or {}).get("ltp", 0))
    if ltp <= 0:
        raise SystemExit("Live NSE LTP unavailable; refusing to calculate limit prices.")
    if args.execute_live_orders:
        live_order_suite(report, instrument, ltp)
        invalid_order_suite(report, instrument)
    else:
        report.items.append({"scenario": "order scenarios", "skipped": True,
                             "reason": "Requires both live-order confirmation flags."})
    candle_suite(report, instrument)
    output = {"generated_at": datetime.now().astimezone().isoformat(),
              "live_orders_executed": args.execute_live_orders,
              "local_provisioned_from_live": args.provision_local_from_live,
              "records": report.items}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    mismatches = sum(item.get("compatible_envelope") is False for item in report.items)
    print(f"Report: {args.report}")
    print(f"Scenarios: {len(report.items)}; envelope mismatches: {mismatches}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())