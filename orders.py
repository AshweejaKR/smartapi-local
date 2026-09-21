"""Minimal persistent MARKET and LIMIT order execution."""
import asyncio
from contextlib import suppress
from datetime import datetime
import logging
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from fastapi.responses import JSONResponse

from auth import active_session, failed, payload
from charges import TRADE_FIELDS, calculate_charges, pnl_values
from market import MarketDataError, get_effective_ltp, mapping_for

DB_PATH = None
LOGGER = logging.getLogger("smartapi.orders")
OPEN = ("OPEN", "PENDING")
PRODUCTS = {"DELIVERY", "INTRADAY", "MARGIN", "CARRYFORWARD", "BO"}
VARIETIES = {"NORMAL", "STOPLOSS", "AMO", "ROBO"}
DURATIONS = {"DAY", "IOC"}


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def stamp():
    return datetime.now().strftime("%d-%b-%Y %H:%M:%S")


def setting(name, default):
    try:
        return max(1, int(os.getenv(name, default)))
    except ValueError:
        return default


def init_orders(path):
    global DB_PATH
    DB_PATH = Path(path)
    with connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
        for name, definition in {
            "unique_order_id": "TEXT NOT NULL DEFAULT ''",
            "accepted_at_ms": "INTEGER NOT NULL DEFAULT 0",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
        conn.execute("UPDATE orders SET unique_order_id=order_id WHERE unique_order_id=''")
        conn.execute(
            "UPDATE orders SET accepted_at_ms=? WHERE accepted_at_ms=0",
            (int(time.time() * 1000),),
        )
        conn.execute("UPDATE orders SET updated_at=created_at WHERE updated_at=''")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS order_events ("
            "id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, status TEXT NOT NULL, "
            "text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)"
        )


def error(message, code="AB1004", status_code=400):
    return JSONResponse(
        status_code=status_code,
        content={"status": False, "message": message, "errorcode": code, "data": None},
    )


def success(row):
    return {
        "status": True, "message": "SUCCESS", "errorcode": "",
        "data": {"orderid": row["order_id"], "uniqueorderid": row["unique_order_id"]},
    }


def add_event(conn, order_id, status, text=""):
    conn.execute(
        "INSERT INTO order_events(order_id, status, text, created_at) VALUES (?, ?, ?, ?)",
        (order_id, status, text, stamp()),
    )


def parse_order(data, current=None):
    def value(api_name, column=None, default=""):
        if api_name in data:
            return data[api_name]
        return current[column or api_name] if current is not None else default

    order_type = str(value("ordertype", "order_type", "MARKET")).upper()
    side = str(value("transactiontype", "transaction_type")).upper()
    try:
        quantity = int(str(value("quantity")))
        price = float(value("price", default=0) or 0)
        trigger = float(value("triggerprice", "trigger_price", 0) or 0)
        disclosed = int(str(value("disclosedquantity", "disclosed_quantity", 0) or 0))
    except (TypeError, ValueError):
        raise ValueError("Invalid order quantity or price")
    if order_type not in {"MARKET", "LIMIT"} or side not in {"BUY", "SELL"}:
        raise ValueError("Only MARKET/LIMIT BUY/SELL orders are supported")
    if (quantity <= 0 or not all(math.isfinite(n) for n in (price, trigger))
            or price < 0 or trigger < 0 or not 0 <= disclosed <= quantity
            or (order_type == "LIMIT" and price <= 0)):
        raise ValueError("Quantity and prices must be valid positive finite values")
    symbols = {
        name: str(value(name)).upper()
        for name in ("exchange", "tradingsymbol", "symboltoken")
    }
    if not all(symbols.values()):
        raise ValueError("Exchange, trading symbol and symbol token are required")
    variety = str(value("variety", default="NORMAL")).upper()
    product = str(value("producttype", "product_type", "DELIVERY")).upper()
    duration = str(value("duration", default="DAY")).upper()
    if variety not in VARIETIES:
        raise ValueError("Invalid order variety")
    if product not in PRODUCTS:
        raise ValueError("Invalid product type")
    if duration not in DURATIONS:
        raise ValueError("Invalid order duration")
    return {
        **symbols, "variety": variety, "order_type": order_type,
        "product_type": product, "duration": duration, "transaction_type": side,
        "quantity": quantity, "price": price, "trigger_price": trigger,
        "disclosed_quantity": disclosed,
    }


def free_cash(conn, client_code, exclude_order=None):
    account = conn.execute(
        "SELECT available_balance, used_funds FROM accounts WHERE client_code=?",
        (client_code,),
    ).fetchone()
    sql = (
        "SELECT COALESCE(SUM(reserved_funds), 0) FROM orders "
        "WHERE client_code=? AND status IN ('OPEN', 'PENDING')"
    )
    params = [client_code]
    if exclude_order:
        sql += " AND order_id<>?"
        params.append(exclude_order)
    reserved = conn.execute(sql, params).fetchone()[0]
    if account is None:
        return 0
    return account["available_balance"] - account["used_funds"] - reserved


def short_margin_percent():
    try:
        return max(0.0, min(100.0, float(os.getenv("SMARTAPI_SHORT_MARGIN_PERCENT", "20"))))
    except ValueError:
        return 20.0


def validate_instrument(values):
    if mapping_for(values["exchange"], values["symboltoken"], values["tradingsymbol"]) is None:
        raise MarketDataError("Failed to get symbol details", "AB1018")


def order_price(values):
    if values["order_type"] == "LIMIT":
        return values["price"]
    return float(get_effective_ltp(
        values["exchange"], values["symboltoken"], values["tradingsymbol"]
    ))


def required_funds(conn, client_code, values, price):
    charges = calculate_charges(conn, price, values["quantity"], values["transaction_type"])
    total = charges["total_charges"]
    if values["transaction_type"] == "BUY":
        return round(charges["gross_trade_value"] + total, 2)

    if values["product_type"] == "DELIVERY":
        holding = conn.execute(
            "SELECT quantity FROM holdings WHERE client_code=? AND exchange=? AND symboltoken=?",
            (client_code, values["exchange"], values["symboltoken"]),
        ).fetchone()
        if holding is None or holding["quantity"] < values["quantity"]:
            raise ValueError("Insufficient holdings")
        return round(total, 2)

    position = conn.execute(
        "SELECT net_qty FROM positions WHERE client_code=? AND exchange=? AND symboltoken=? "
        "AND product_type=?",
        (client_code, values["exchange"], values["symboltoken"], values["product_type"]),
    ).fetchone()
    long_qty = max(0, position["net_qty"] if position else 0)
    short_qty = max(0, values["quantity"] - long_qty)
    margin = price * short_qty * short_margin_percent() / 100
    return round(total + margin, 2)


def used_funds_for_positions(conn, client_code):
    used = 0.0
    for row in conn.execute(
        "SELECT net_qty, avg_price, product_type FROM positions WHERE client_code=? AND net_qty<>0",
        (client_code,),
    ):
        value = abs(row["net_qty"]) * row["avg_price"]
        used += value if row["net_qty"] > 0 else value * short_margin_percent() / 100
    return round(used, 2)


def insert_order(conn, client_code, values, status, reserved, text=""):
    order_id = str(uuid.uuid4().int % 10**16).zfill(16)
    unique_id = str(uuid.uuid4())
    now = stamp()
    conn.execute(
        "INSERT INTO orders (client_code, order_id, unique_order_id, variety, "
        "order_type, product_type, duration, price, trigger_price, quantity, "
        "disclosed_quantity, transaction_type, exchange, tradingsymbol, symboltoken, "
        "status, reserved_funds, text, created_at, accepted_at_ms, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            client_code, order_id, unique_id, values["variety"], values["order_type"],
            values["product_type"], values["duration"], values["price"],
            values["trigger_price"], values["quantity"], values["disclosed_quantity"],
            values["transaction_type"], values["exchange"], values["tradingsymbol"],
            values["symboltoken"], status, reserved, text, now,
            int(time.time() * 1000), now,
        ),
    )
    add_event(conn, order_id, status, text)
    return conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()


async def place_order(request):
    auth = active_session(request)
    if auth is None:
        return failed("Invalid or expired token", 403)
    try:
        values = parse_order(await payload(request))
        validate_instrument(values)
        price = await asyncio.to_thread(order_price, values)
    except ValueError as exc:
        return error(str(exc))
    except MarketDataError as exc:
        return error(str(exc), exc.errorcode, exc.status_code)
    status = "PENDING" if values["order_type"] == "MARKET" else "OPEN"
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            reserved = required_funds(conn, auth["client_code"], values, price)
        except ValueError as exc:
            insert_order(conn, auth["client_code"], values, "REJECTED", 0, str(exc))
            return error(str(exc), "AB1002")
        if reserved > free_cash(conn, auth["client_code"]):
            insert_order(
                conn, auth["client_code"], values, "REJECTED", 0,
                "Insufficient funds",
            )
            return error("Insufficient funds", "AB1002")
        row = insert_order(conn, auth["client_code"], values, status, reserved)
    return success(row)


async def modify_order(request):
    auth = active_session(request)
    if auth is None:
        return failed("Invalid or expired token", 403)
    data = await payload(request)
    order_id = str(data.get("orderid", ""))
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id=? AND client_code=?",
            (order_id, auth["client_code"]),
        ).fetchone()
    if row is None:
        return error("Order not found", "AB1010", 404)
    if row["status"] not in OPEN:
        return error("Only an open order can be modified", "AB1011")
    try:
        values = parse_order(data, row)
        if any(values[key] != row[key] for key in (
            "exchange", "tradingsymbol", "symboltoken", "transaction_type"
        )):
            raise ValueError("Order symbol and transaction side cannot be modified")
        validate_instrument(values)
        price = await asyncio.to_thread(order_price, values)
    except ValueError as exc:
        return error(str(exc))
    except MarketDataError as exc:
        return error(str(exc), exc.errorcode, exc.status_code)

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM orders WHERE order_id=? AND client_code=?",
            (order_id, auth["client_code"]),
        ).fetchone()
        if current is None or current["status"] not in OPEN:
            return error("Only an open order can be modified", "AB1011")
        try:
            reserved = required_funds(conn, auth["client_code"], values, price)
        except ValueError as exc:
            return error(str(exc), "AB1002")
        if reserved > free_cash(conn, auth["client_code"], order_id):
            return error("Insufficient funds", "AB1002")
        status = "PENDING" if values["order_type"] == "MARKET" else "OPEN"
        now = stamp()
        conn.execute(
            "UPDATE orders SET variety=?, order_type=?, product_type=?, duration=?, "
            "price=?, trigger_price=?, quantity=?, disclosed_quantity=?, status=?, "
            "reserved_funds=?, text='', accepted_at_ms=?, updated_at=? WHERE order_id=?",
            (
                values["variety"], values["order_type"], values["product_type"],
                values["duration"], values["price"], values["trigger_price"],
                values["quantity"], values["disclosed_quantity"], status, reserved,
                int(time.time() * 1000), now, order_id,
            ),
        )
        add_event(conn, order_id, status, "Order modified")
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
    return success(row)


async def cancel_order(request):
    auth = active_session(request)
    if auth is None:
        return failed("Invalid or expired token", 403)
    order_id = str((await payload(request)).get("orderid", ""))
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id=? AND client_code=?",
            (order_id, auth["client_code"]),
        ).fetchone()
        if row is None:
            return error("Order not found", "AB1010", 404)
        if row["status"] not in OPEN:
            return error("Only an open order can be cancelled", "AB1011")
        conn.execute(
            "UPDATE orders SET status='CANCELLED', reserved_funds=0, text=?, "
            "updated_at=? WHERE order_id=?",
            ("Order cancelled", stamp(), order_id),
        )
        add_event(conn, order_id, "CANCELLED", "Order cancelled")
    return success(row)


def next_average(old_qty, old_average, signed_qty, fill_price):
    new_qty = old_qty + signed_qty
    if old_qty == 0 or old_qty * signed_qty > 0:
        total = abs(old_qty) * old_average + abs(signed_qty) * fill_price
        return round(total / abs(new_qty), 4)
    if new_qty == 0:
        return 0
    return old_average if old_qty * new_qty > 0 else fill_price


def update_position(conn, row, fill_price, total_charges):
    key = (
        row["client_code"], row["exchange"], row["symboltoken"], row["product_type"]
    )
    current = conn.execute(
        "SELECT * FROM positions WHERE client_code=? AND exchange=? AND symboltoken=? "
        "AND product_type=?",
        key,
    ).fetchone()
    old_qty = current["net_qty"] if current else 0
    old_average = current["avg_price"] if current else 0
    signed = row["quantity"] if row["transaction_type"] == "BUY" else -row["quantity"]
    closed = min(abs(signed), abs(old_qty)) if old_qty * signed < 0 else 0
    previous_realized = current["realized_pnl"] if current else 0
    gross_pnl = round(closed * (
        fill_price - old_average if old_qty > 0 else old_average - fill_price
    ), 2)
    realized = previous_realized + gross_pnl
    buy_qty = (current["buy_qty"] if current else 0) + (
        row["quantity"] if signed > 0 else 0
    )
    sell_qty = (current["sell_qty"] if current else 0) + (
        row["quantity"] if signed < 0 else 0
    )
    buy_amount = (current["buy_amount"] if current else 0) + (
        fill_price * row["quantity"] if signed > 0 else 0
    )
    sell_amount = (current["sell_amount"] if current else 0) + (
        fill_price * row["quantity"] if signed < 0 else 0
    )
    conn.execute(
        "INSERT INTO positions(client_code, exchange, symboltoken, product_type, "
        "tradingsymbol, net_qty, buy_qty, sell_qty, buy_amount, sell_amount, avg_price, "
        "realized_pnl, last_price, total_charges) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(client_code, exchange, symboltoken, product_type) DO UPDATE SET "
        "net_qty=excluded.net_qty, buy_qty=excluded.buy_qty, sell_qty=excluded.sell_qty, "
        "buy_amount=excluded.buy_amount, sell_amount=excluded.sell_amount, "
        "avg_price=excluded.avg_price, realized_pnl=excluded.realized_pnl, "
        "last_price=excluded.last_price, total_charges=excluded.total_charges",
        (
            *key, row["tradingsymbol"], old_qty + signed, buy_qty, sell_qty,
            round(buy_amount, 2), round(sell_amount, 2),
            next_average(old_qty, old_average, signed, fill_price),
            round(realized, 2), fill_price,
            round((current["total_charges"] if current else 0) + total_charges, 2),
        ),
    )
    return gross_pnl


def update_holding(conn, row, fill_price):
    if row["product_type"] not in {"CNC", "DELIVERY"}:
        return
    key = (row["client_code"], row["exchange"], row["symboltoken"])
    current = conn.execute(
        "SELECT * FROM holdings WHERE client_code=? AND exchange=? AND symboltoken=?",
        key,
    ).fetchone()
    old_qty = current["quantity"] if current else 0
    old_average = current["average_price"] if current else 0
    signed = row["quantity"] if row["transaction_type"] == "BUY" else -row["quantity"]
    conn.execute(
        "INSERT INTO holdings(client_code, exchange, symboltoken, tradingsymbol, "
        "quantity, average_price, last_price) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(client_code, exchange, symboltoken) DO UPDATE SET "
        "quantity=excluded.quantity, average_price=excluded.average_price, "
        "last_price=excluded.last_price",
        (
            *key, row["tradingsymbol"], old_qty + signed,
            next_average(old_qty, old_average, signed, fill_price), fill_price,
        ),
    )


def fill_order(order_id, ltp):
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if row is None or row["status"] not in OPEN:
            return
        if (
            row["order_type"] == "MARKET"
            and int(time.time() * 1000) - row["accepted_at_ms"]
            < setting("SMARTAPI_MARKET_FILL_DELAY_MS", 1000)
        ):
            return
        if row["order_type"] == "LIMIT":
            triggered = row["transaction_type"] == "BUY" and ltp <= row["price"]
            triggered |= row["transaction_type"] == "SELL" and ltp >= row["price"]
            if not triggered:
                return
        fill_price = ltp if row["order_type"] == "MARKET" else row["price"]
        charges = calculate_charges(conn, fill_price, row["quantity"], row["transaction_type"])
        values = {
            "exchange": row["exchange"], "tradingsymbol": row["tradingsymbol"],
            "symboltoken": row["symboltoken"], "product_type": row["product_type"],
            "transaction_type": row["transaction_type"], "quantity": row["quantity"],
        }
        try:
            needed = required_funds(conn, row["client_code"], values, fill_price)
        except ValueError as exc:
            conn.execute(
                "UPDATE orders SET status='REJECTED', reserved_funds=0, text=?, "
                "updated_at=? WHERE order_id=?",
                (str(exc), stamp(), order_id),
            )
            add_event(conn, order_id, "REJECTED", str(exc))
            return
        if needed > round(free_cash(conn, row["client_code"], order_id), 2):
            conn.execute(
                "UPDATE orders SET status='REJECTED', reserved_funds=0, text=?, "
                "updated_at=? WHERE order_id=?",
                ("Insufficient funds", stamp(), order_id),
            )
            add_event(conn, order_id, "REJECTED", "Insufficient funds")
            return
        now = stamp()
        conn.execute(
            "UPDATE orders SET status='FILLED', filled_quantity=quantity, "
            "average_price=?, reserved_funds=0, text='', updated_at=? WHERE order_id=?",
            (fill_price, now, order_id),
        )
        conn.execute(
            "INSERT INTO trades(client_code, trade_id, order_id, exchange, "
            "tradingsymbol, symboltoken, transaction_type, product_type, quantity, "
            "price, trade_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["client_code"], f"T{order_id}", order_id, row["exchange"],
                row["tradingsymbol"], row["symboltoken"], row["transaction_type"],
                row["product_type"], row["quantity"], fill_price, now,
            ),
        )
        gross_pnl = update_position(conn, row, fill_price, charges["total_charges"])
        used_funds = used_funds_for_positions(conn, row["client_code"])
        conn.execute(
            "UPDATE accounts SET used_funds=?, "
            "available_balance=ROUND(available_balance + ? - ?, 2), "
            "realized_pnl=ROUND(realized_pnl + ?, 2), "
            "total_charges=ROUND(total_charges + ?, 2) WHERE client_code=?",
            (used_funds, gross_pnl, charges["total_charges"], gross_pnl,
             charges["total_charges"], row["client_code"]),
        )
        charges.update(pnl_values(gross_pnl, 0, charges["total_charges"]))
        conn.execute(
            "UPDATE trades SET " + ", ".join(f"{name}=?" for name in TRADE_FIELDS)
            + " WHERE trade_id=?",
            (*[charges[name] for name in TRADE_FIELDS], f"T{order_id}"),
        )
        update_holding(conn, row, fill_price)
        add_event(conn, order_id, "FILLED")


def check_open_orders():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status IN ('OPEN', 'PENDING') ORDER BY id"
        ).fetchall()
    now_ms = int(time.time() * 1000)
    delay = setting("SMARTAPI_MARKET_FILL_DELAY_MS", 1000)
    for row in rows:
        if row["order_type"] == "MARKET" and now_ms - row["accepted_at_ms"] < delay:
            continue
        try:
            ltp = float(get_effective_ltp(
                row["exchange"], row["symboltoken"], row["tradingsymbol"]
            ))
        except (MarketDataError, TypeError, ValueError):
            continue
        triggered = row["order_type"] == "MARKET"
        triggered |= row["transaction_type"] == "BUY" and ltp <= row["price"]
        triggered |= row["transaction_type"] == "SELL" and ltp >= row["price"]
        if triggered:
            fill_order(row["order_id"], ltp)


async def order_checker():
    while True:
        try:
            await asyncio.to_thread(check_open_orders)
        except Exception:
            LOGGER.exception("order checker failed")
        await asyncio.sleep(setting("SMARTAPI_ORDER_CHECK_INTERVAL_MS", 100) / 1000)


async def stop_checker(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
