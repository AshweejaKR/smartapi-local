"""Small deterministic handlers for the SmartAPI routes outside the core engine."""
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from auth import active_session, failed, payload
from charges import CHARGE_FIELDS, calculate_charges
from market import list_mappings
from portfolio import connect, order_view


DB_PATH = None


def init_phase11(path):
    global DB_PATH
    DB_PATH = Path(path)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS gtt_rules ("
            "id TEXT PRIMARY KEY, client_code TEXT NOT NULL, status TEXT NOT NULL, "
            "payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )


def result(data=None, message="SUCCESS"):
    return {"status": True, "message": message, "errorcode": "", "data": data}


def error(message, code="AB1004", status_code=400, data=None):
    return JSONResponse(
        status_code=status_code,
        content={"status": False, "message": message, "errorcode": code, "data": data},
    )


def session(request):
    value = active_session(request)
    return value


def secured(request):
    return session(request) is not None


def stable_number(value):
    return int(hashlib.sha256(str(value).encode()).hexdigest()[:8], 16)


def now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S+05:30")


def _rule(row):
    data = json.loads(row["payload"])
    data.update({"id": row["id"], "status": row["status"],
                 "createddate": row["created_at"], "updateddate": row["updated_at"]})
    return data


async def convert_position(request):
    auth = session(request)
    if not auth:
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    try:
        quantity = int(data.get("quantity", 0))
        if quantity <= 0 or float(data["quantity"]) != quantity:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        return error("quantity must be a positive integer")
    exchange, token = str(data.get("exchange", "")).upper(), str(data.get("symboltoken", ""))
    old, new = (str(data.get(key, "")).upper() for key in ("oldproducttype", "newproducttype"))
    products = {"DELIVERY", "CNC", "INTRADAY", "MIS", "MARGIN", "CARRYFORWARD", "NRML"}
    side = str(data.get("transactiontype", "")).upper()
    if not exchange or not token or old not in products or new not in products or old == new or side not in {"BUY", "SELL"}:
        return error("Invalid position conversion parameters")
    signed = quantity if side == "BUY" else -quantity
    key = (auth["client_code"], exchange, token)
    delivery = {"DELIVERY", "CNC"}
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute("SELECT * FROM positions WHERE client_code=? AND exchange=? AND symboltoken=? AND product_type=?", (*key, old)).fetchone()
        target = conn.execute("SELECT * FROM positions WHERE client_code=? AND exchange=? AND symboltoken=? AND product_type=?", (*key, new)).fetchone()
        if source is None or source["net_qty"] * signed <= 0 or quantity > abs(source["net_qty"]):
            return error("Insufficient position quantity")
        if target and target["net_qty"] * signed < 0:
            return error("Cannot convert into an opposite position")
        if new in delivery and signed < 0:
            return error("Short positions cannot be converted to delivery")
        holding = conn.execute("SELECT * FROM holdings WHERE client_code=? AND exchange=? AND symboltoken=?", key).fetchone()
        if old in delivery and new not in delivery and (holding is None or holding["quantity"] < quantity):
            return error("Insufficient holding quantity")
        cost = round(source["avg_price"] * quantity, 2)
        leg = "buy" if signed > 0 else "sell"
        remaining = source["net_qty"] - signed
        conn.execute(
            f"UPDATE positions SET net_qty=?, {leg}_qty={leg}_qty-?, {leg}_amount=ROUND({leg}_amount-?,2), avg_price=? WHERE client_code=? AND exchange=? AND symboltoken=? AND product_type=?",
            (remaining, quantity, cost, source["avg_price"] if remaining else 0, *key, old),
        )
        previous_qty = target["net_qty"] if target else 0
        new_qty = previous_qty + signed
        average = round((abs(previous_qty) * (target["avg_price"] if target else 0) + cost) / abs(new_qty), 4)
        conn.execute(
            "INSERT INTO positions(client_code,exchange,symboltoken,product_type,tradingsymbol) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            (*key, new, source["tradingsymbol"]),
        )
        conn.execute(
            f"UPDATE positions SET net_qty=?, {leg}_qty={leg}_qty+?, {leg}_amount=ROUND({leg}_amount+?,2), avg_price=?, last_price=? WHERE client_code=? AND exchange=? AND symboltoken=? AND product_type=?",
            (new_qty, quantity, cost, average, source["last_price"], *key, new),
        )
        if (old in delivery) != (new in delivery):
            previous = holding["quantity"] if holding else 0
            held = previous + (quantity if new in delivery else -quantity)
            held_average = (round((previous * (holding["average_price"] if holding else 0) + cost) / held, 4)
                            if new in delivery else holding["average_price"]) if held else 0
            conn.execute(
                "INSERT INTO holdings(client_code,exchange,symboltoken,tradingsymbol,quantity,average_price,last_price) VALUES (?,?,?,?,?,?,?) ON CONFLICT(client_code,exchange,symboltoken) DO UPDATE SET quantity=excluded.quantity,average_price=excluded.average_price,last_price=excluded.last_price",
                (*key, source["tradingsymbol"], held, held_average, source["last_price"]),
            )
    return result(None)


async def search_scrip(request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    exchange, query = str(data.get("exchange", "")).upper(), str(data.get("searchscrip", "")).upper()
    if not exchange or not query:
        return error("exchange and searchscrip are required")
    values = [
        {"exchange": row["exchange"], "tradingsymbol": row["tradingsymbol"],
         "symboltoken": row["symboltoken"]}
        for row in list_mappings()
        if row["exchange"] == exchange and query in row["tradingsymbol"]
    ]
    return result(values)


async def individual_order_details(request):
    auth = session(request)
    if not auth:
        return failed("Invalid or expired token", 403)
    order_id = request.path_params.get("order_id", "")
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE (order_id=? OR unique_order_id=?) AND client_code=?",
            (str(order_id), str(order_id), auth["client_code"]),
        ).fetchone()
    return error("Order not found", "AB1013", 404) if row is None else result(order_view(row))


def _gtt_error(request):
    return failed("Invalid or expired token", 403) if not session(request) else None


def next_gtt_id(conn):
    value = conn.execute(
        "SELECT COALESCE(MAX(CAST(id AS INTEGER)), 0) FROM gtt_rules"
    ).fetchone()[0]
    return str(value + 1)


async def gtt_create(request):
    if (bad := _gtt_error(request)):
        return bad
    data = await payload(request)
    required = ("tradingsymbol", "symboltoken", "exchange", "transactiontype",
                "producttype", "price", "qty", "triggerprice")
    if any(data.get(name) in (None, "") for name in required):
        return error("Invalid GTT parameters", "AB9001")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rule_id = next_gtt_id(conn)
        stamp = now()
        conn.execute(
            "INSERT INTO gtt_rules VALUES (?, ?, 'NEW', ?, ?, ?)",
            (rule_id, session(request)["client_code"], json.dumps(data, sort_keys=True), stamp, stamp),
        )
    return result({"id": rule_id})


async def gtt_modify(request):
    if (bad := _gtt_error(request)):
        return bad
    data = await payload(request)
    rule_id = str(data.get("id", ""))
    auth = session(request)
    with connect() as conn:
        row = conn.execute("SELECT * FROM gtt_rules WHERE id=? AND client_code=?", (rule_id, auth["client_code"])).fetchone()
        if row is None:
            return error("Invalid GTT rule id", "AB9013")
        values = json.loads(row["payload"])
        values.update({key: value for key, value in data.items() if key != "id"})
        conn.execute("UPDATE gtt_rules SET payload=?, updated_at=? WHERE id=?", (json.dumps(values, sort_keys=True), now(), rule_id))
    return result({"id": rule_id})


async def gtt_cancel(request):
    if (bad := _gtt_error(request)):
        return bad
    data = await payload(request)
    rule_id, auth = str(data.get("id", "")), session(request)
    with connect() as conn:
        row = conn.execute("SELECT id FROM gtt_rules WHERE id=? AND client_code=?", (rule_id, auth["client_code"])).fetchone()
        if row is None:
            return error("Invalid GTT rule id", "AB9013")
        conn.execute("UPDATE gtt_rules SET status='CANCELLED', updated_at=? WHERE id=?", (now(), rule_id))
    return result({"id": rule_id})


async def gtt_details(request):
    if (bad := _gtt_error(request)):
        return bad
    data, auth = await payload(request), session(request)
    with connect() as conn:
        row = conn.execute("SELECT * FROM gtt_rules WHERE id=? AND client_code=?", (str(data.get("id", "")), auth["client_code"])).fetchone()
    return error("Invalid GTT rule id", "AB9013") if row is None else result(_rule(row))


async def gtt_list(request):
    if (bad := _gtt_error(request)):
        return bad
    data, auth = await payload(request), session(request)
    statuses = data.get("status", [])
    if not isinstance(statuses, list):
        return error("status must be a list", "AB9004")
    try:
        page, count = int(data.get("page", 1)), int(data.get("count", 10))
        if page < 1 or count < 1:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        return error("page and count must be positive integers", "AB9004")
    with connect() as conn:
        rows = conn.execute("SELECT * FROM gtt_rules WHERE client_code=? ORDER BY id", (auth["client_code"],)).fetchall()
    if statuses and "FORALL" not in statuses:
        rows = [row for row in rows if row["status"] in statuses]
    start = (page - 1) * count
    return result([_rule(row) for row in rows[start:start + count]])


async def oi_data(request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    try:
        start = datetime.strptime(data["fromdate"], "%Y-%m-%d %H:%M")
        end = datetime.strptime(data["todate"], "%Y-%m-%d %H:%M")
        step = {"ONE_MINUTE": 1, "THREE_MINUTE": 3, "FIVE_MINUTE": 5, "TEN_MINUTE": 10,
                "FIFTEEN_MINUTE": 15, "THIRTY_MINUTE": 30, "ONE_HOUR": 60, "ONE_DAY": 1440}[str(data["interval"]).upper()]
        if start > end:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return error("Invalid OI request")
    seed = stable_number(f"{data.get('exchange')}:{data.get('symboltoken')}")
    values, cursor = [], start
    while cursor <= end and len(values) < 2000:
        values.append({"time": cursor.strftime("%Y-%m-%dT%H:%M:00+05:30"), "oi": seed % 100000 + len(values) * 125})
        cursor += timedelta(minutes=step)
    return result(values)


async def margin_api(request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    positions = data.get("positions", [])
    if not isinstance(positions, list) or len(positions) > 50:
        return error("positions must contain at most 50 items")
    total = 0.0
    premium = 0.0
    for item in positions:
        if not isinstance(item, dict):
            return error("Invalid position")
        try:
            amount = abs(float(item.get("qty", 0))) * float(item.get("price", 0) or 0)
            if not math.isfinite(amount) or amount < 0:
                raise ValueError
        except (TypeError, ValueError):
            return error("Invalid position")
        total += amount * (0.2 if str(item.get("exchange", "")).upper() in {"NFO", "BFO", "MCX"} else 1)
        premium += amount if str(item.get("productType", "")).upper() == "DELIVERY" else 0
    margin = round(total, 2)
    return result({"totalMarginRequired": margin, "marginComponents": {
        "netPremium": round(premium, 2), "spanMargin": margin, "marginBenefit": 0,
        "deliveryMargin": 0, "nonNFOMargin": 0, "totOptionsPremium": round(premium, 2)}})


async def estimate_charges(request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    orders = data.get("orders", [])
    if not isinstance(orders, list):
        return error("orders must be a list")
    totals, turnover, breakup = {}, 0.0, []
    with connect() as conn:
        for item in orders:
            if not isinstance(item, dict):
                return error("Invalid order")
            try:
                values = calculate_charges(conn, float(item.get("price", 0)), int(item.get("quantity", 0)), str(item.get("transaction_type", "")).upper())
            except (TypeError, ValueError, OverflowError):
                return error("Invalid order")
            turnover += values["gross_trade_value"]
            for key in CHARGE_FIELDS:
                amount = values[key]
                totals[key] = round(totals.get(key, 0) + amount, 4)
        breakup = [{"name": key, "amount": amount} for key, amount in totals.items()]
    return result({"summary": {"total_charges": round(sum(totals.values()), 4),
                                 "trade_value": round(turnover, 2), "breakup": breakup}})


async def verify_dis(request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    if not data.get("isin") or not data.get("quantity"):
        return error("isin and quantity are required")
    req_id = f"{stable_number(data['isin']) % 10**16:016d}"
    return result({"ReqId": req_id, "ReturnURL": "https://trade.angelbroking.com/cdslpoa/response",
                   "DPId": "33200", "BOID": "1203320015222472",
                   "TransDtls": f"LOCAL-{req_id}", "version": "1.1"})


async def generate_tpin(request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    if not all(data.get(key) for key in ("dpId", "ReqId", "boid", "pan")):
        return error("dpId, ReqId, boid and pan are required")
    return result({"ReqId": str(data["ReqId"]), "status": "SUCCESS", "message": "TPIN generation initiated"})


async def transaction_status(request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    if not data.get("ReqId"):
        return error("ReqId is required")
    return result({"TransResDtls": {"ReqId": str(data["ReqId"]), "ReqType": "D",
                                    "ResId": f"LOCAL-{data['ReqId']}", "ResStatus": "1",
                                    "ResTime": datetime.now().strftime("%d%m%Y%H%M%S"),
                                    "ResError": "", "Remarks": "",
                                    "RecordResDtls": [{"TxnReqId": str(data["ReqId"]),
                                                       "TxnId": f"LOCAL-{data['ReqId']}",
                                                       "Status": "1", "Errorcode": ""}]}})


async def option_greek(request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    if not data.get("name") or not data.get("expirydate"):
        return error("name and expirydate are required")
    rows = []
    for index, option_type in enumerate(("CE", "PE")):
        rows.append({"name": str(data["name"]).upper(), "expiry": data["expirydate"],
                     "strikePrice": f"{2400 + index * 100:.6f}", "optionType": option_type,
                     "delta": "0.492400" if option_type == "CE" else "-0.507600",
                     "gamma": "0.002800", "theta": "-4.091800", "vega": "2.296700",
                     "impliedVolatility": "16.330000", "tradeVolume": "24048.00"})
    return result(rows)


def derivatives_rows(kind):
    base = (("NIFTY25JAN24FUT", 99926000, 1.04), ("RELIANCE25JAN24FUT", 2885, 0.82),
            ("SBIN25JAN24FUT", 3045, 0.67))
    if kind == "pcr":
        return [{"pcr": pcr, "tradingSymbol": symbol} for symbol, _, pcr in base]
    if kind == "gainers":
        return [{"tradingSymbol": symbol, "percentChange": round(6.25 - index * 1.1, 2),
                 "symbolToken": token, "opnInterest": 100000 + index * 25000,
                 "netChangeOpnInterest": 2500 - index * 400}
                for index, (symbol, token, _) in enumerate(base)]
    return [{"symbolToken": str(token), "ltp": f"{185.50 - index * 12:.2f}",
             "netChange": f"{4.25 - index * 2:.2f}", "percentChange": f"{2.35 - index * .9:.2f}",
             "opnInterest": f"{25000 + index * 5000:.1f}", "netChangeOpnInterest": f"{700 - index * 125:.1f}",
             "tradingSymbol": symbol} for index, (symbol, token, _) in enumerate(base)]


async def gainers_losers(request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    if data.get("datatype") not in {"PercOIGainers", "PercOILosers", "PercPriceGainers", "PercPriceLosers"}:
        return error("Invalid datatype")
    if str(data.get("expirytype", "")).upper() not in {"NEAR", "NEXT", "FAR"}:
        return error("Invalid expirytype")
    rows = derivatives_rows("gainers")
    if "Losers" in data["datatype"]:
        rows = [{**row, "percentChange": -abs(row["percentChange"]), "netChangeOpnInterest": -abs(row["netChangeOpnInterest"])} for row in rows]
    return result(rows)


async def put_call_ratio(request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    return result(derivatives_rows("pcr"))


async def oi_buildup(request):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    if data.get("datatype") not in {"Long Built Up", "Short Built Up", "Short Covering", "Long Unwinding"}:
        return error("Invalid datatype")
    if str(data.get("expirytype", "")).upper() not in {"NEAR", "NEXT", "FAR"}:
        return error("Invalid expirytype")
    rows = derivatives_rows("oi")
    if data["datatype"] in {"Short Built Up", "Long Unwinding"}:
        rows = [{**row, "netChange": f"{-abs(float(row['netChange'])):.2f}", "percentChange": f"{-abs(float(row['percentChange'])):.2f}"} for row in rows]
    return result(rows)


async def intraday(request, exchange):
    if not secured(request):
        return failed("Invalid or expired token", 403)
    names = ("SBIN-EQ", "RELIANCE-EQ", "NIFTY")
    return result([{"exchange": exchange, "SymbolName": name, "Multiplier": "5.0" if name != "NIFTY" else "1.0"} for name in names])

HANDLERS = {
    "/rest/secure/angelbroking/order/v1/convertPosition": convert_position,
    "/gtt-service/rest/secure/angelbroking/gtt/v1/createRule": gtt_create,
    "/gtt-service/rest/secure/angelbroking/gtt/v1/modifyRule": gtt_modify,
    "/gtt-service/rest/secure/angelbroking/gtt/v1/cancelRule": gtt_cancel,
    "/rest/secure/angelbroking/gtt/v1/ruleDetails": gtt_details,
    "/rest/secure/angelbroking/gtt/v1/ruleList": gtt_list,
    "/rest/secure/angelbroking/historical/v1/getOIData": oi_data,
    "/rest/secure/angelbroking/order/v1/searchScrip": search_scrip,
    "/rest/secure/angelbroking/margin/v1/batch": margin_api,
    "/rest/secure/angelbroking/brokerage/v1/estimateCharges": estimate_charges,
    "/rest/secure/angelbroking/edis/v1/verifyDis": verify_dis,
    "/rest/secure/angelbroking/edis/v1/generateTPIN": generate_tpin,
    "/rest/secure/angelbroking/edis/v1/getTranStatus": transaction_status,
    "/rest/secure/angelbroking/marketData/v1/optionGreek": option_greek,
    "/rest/secure/angelbroking/marketData/v1/gainersLosers": gainers_losers,
    "/rest/secure/angelbroking/marketData/v1/putCallRatio": put_call_ratio,
    "/rest/secure/angelbroking/marketData/v1/OIBuildup": oi_buildup,
    "/rest/secure/angelbroking/marketData/v1/nseIntraday": lambda request: intraday(request, "NSE"),
    "/rest/secure/angelbroking/marketData/v1/bseIntraday": lambda request: intraday(request, "BSE"),
}
