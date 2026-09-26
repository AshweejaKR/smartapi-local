"""Persistent SmartAPI portfolio storage and views."""
import asyncio

from auth import active_session
from charges import TRADE_FIELDS, pnl_values
from common import connect, failed, ok
from market import get_effective_ltp

ORDER_STATUS = {
    "PENDING": "open pending", "OPEN": "open", "FILLED": "complete",
    "REJECTED": "rejected", "CANCELLED": "cancelled",
}


def api_order_status(value):
    return ORDER_STATUS.get(str(value).upper(), str(value).lower())


def init_portfolio():
    with connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS accounts (client_code TEXT PRIMARY KEY, available_balance REAL NOT NULL DEFAULT 0, used_funds REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
        for name in ("used_funds", "realized_pnl"):
            if name not in columns:
                conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} REAL NOT NULL DEFAULT 0")
        conn.execute("INSERT OR IGNORE INTO accounts(client_code) SELECT client_code FROM users")
        conn.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, client_code TEXT NOT NULL, order_id TEXT UNIQUE NOT NULL, variety TEXT NOT NULL DEFAULT 'NORMAL', order_type TEXT NOT NULL DEFAULT 'MARKET', product_type TEXT NOT NULL DEFAULT 'CNC', duration TEXT NOT NULL DEFAULT 'DAY', price REAL NOT NULL DEFAULT 0, trigger_price REAL NOT NULL DEFAULT 0, quantity INTEGER NOT NULL, disclosed_quantity INTEGER NOT NULL DEFAULT 0, transaction_type TEXT NOT NULL, exchange TEXT NOT NULL, tradingsymbol TEXT NOT NULL, symboltoken TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN', filled_quantity INTEGER NOT NULL DEFAULT 0, average_price REAL NOT NULL DEFAULT 0, reserved_funds REAL NOT NULL DEFAULT 0, text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, client_code TEXT NOT NULL, trade_id TEXT UNIQUE NOT NULL, order_id TEXT NOT NULL, exchange TEXT NOT NULL, tradingsymbol TEXT NOT NULL, symboltoken TEXT NOT NULL, transaction_type TEXT NOT NULL, product_type TEXT NOT NULL, quantity INTEGER NOT NULL, price REAL NOT NULL, trade_time TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS positions (client_code TEXT NOT NULL, exchange TEXT NOT NULL, symboltoken TEXT NOT NULL, product_type TEXT NOT NULL, tradingsymbol TEXT NOT NULL, net_qty INTEGER NOT NULL DEFAULT 0, buy_qty INTEGER NOT NULL DEFAULT 0, sell_qty INTEGER NOT NULL DEFAULT 0, buy_amount REAL NOT NULL DEFAULT 0, sell_amount REAL NOT NULL DEFAULT 0, avg_price REAL NOT NULL DEFAULT 0, realized_pnl REAL NOT NULL DEFAULT 0, last_price REAL NOT NULL DEFAULT 0, PRIMARY KEY(client_code, exchange, symboltoken, product_type))")
        conn.execute("CREATE TABLE IF NOT EXISTS holdings (client_code TEXT NOT NULL, exchange TEXT NOT NULL, symboltoken TEXT NOT NULL, tradingsymbol TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 0, average_price REAL NOT NULL DEFAULT 0, isin TEXT NOT NULL DEFAULT '', last_price REAL NOT NULL DEFAULT 0, PRIMARY KEY(client_code, exchange, symboltoken))")


def price(row):
    try:
        return float(get_effective_ltp(row["exchange"], row["symboltoken"], row["tradingsymbol"], row["client_code"]))
    except Exception:
        return float(row["last_price"])


def order_view(row):
    filled = row["filled_quantity"]
    status = api_order_status(row["status"])
    return {"variety": row["variety"], "ordertype": row["order_type"], "producttype": row["product_type"], "duration": row["duration"], "price": row["price"], "triggerprice": row["trigger_price"], "quantity": row["quantity"], "disclosedquantity": row["disclosed_quantity"], "transactiontype": row["transaction_type"], "exchange": row["exchange"], "tradingsymbol": row["tradingsymbol"], "symboltoken": row["symboltoken"], "orderid": row["order_id"], "uniqueorderid": row["unique_order_id"], "status": status, "orderstatus": status, "filledshares": filled, "unfilledshares": row["quantity"] - filled, "averageprice": row["average_price"], "text": row["text"], "updatetime": row["updated_at"]}


def trade_view(row):
    return {"trade_id": row["trade_id"], "orderid": row["order_id"], "exchange": row["exchange"], "tradingsymbol": row["tradingsymbol"], "symboltoken": row["symboltoken"], "transactiontype": row["transaction_type"], "producttype": row["product_type"], "fillsize": row["quantity"], "fillprice": row["price"], "tradeDate": row["trade_time"], **{name: row[name] for name in TRADE_FIELDS}}


def position_view(row):
    ltp = price(row)
    unrealized = round((ltp - row["avg_price"]) * row["net_qty"], 2)
    return {"exchange": row["exchange"], "symboltoken": row["symboltoken"], "producttype": row["product_type"], "tradingsymbol": row["tradingsymbol"], "buyqty": row["buy_qty"], "sellqty": row["sell_qty"], "buyamount": row["buy_amount"], "sellamount": row["sell_amount"], "netqty": row["net_qty"], "netvalue": round(row["net_qty"] * row["avg_price"], 2), "avgnetprice": row["avg_price"], "ltp": ltp, "realised": row["realized_pnl"], "unrealised": unrealized, **pnl_values(row["realized_pnl"], unrealized, row["total_charges"])}


def holding_view(row):
    ltp = price(row)
    pnl = round((ltp - row["average_price"]) * row["quantity"], 2)
    cost = row["average_price"] * row["quantity"]
    return {"tradingsymbol": row["tradingsymbol"], "exchange": row["exchange"], "isin": row["isin"], "t1quantity": 0, "realisedquantity": row["quantity"], "quantity": row["quantity"], "authorisedquantity": row["quantity"], "product": "CNC", "collateralquantity": 0, "collateraltype": "", "haircut": 0, "averageprice": row["average_price"], "ltp": ltp, "symboltoken": row["symboltoken"], "close": ltp, "profitandloss": pnl, "pnlpercentage": round(pnl * 100 / cost, 2) if cost else 0}


async def rms_limit(request):
    auth = active_session(request)
    if auth is None:
        return failed("Invalid or expired token", 403)
    with connect() as conn:
        account = conn.execute("SELECT * FROM accounts WHERE client_code=?", (auth["client_code"],)).fetchone()
        rows = conn.execute("SELECT * FROM positions WHERE client_code=?", (auth["client_code"],)).fetchall()
        reserved = conn.execute("SELECT COALESCE(SUM(reserved_funds), 0) FROM orders WHERE client_code=? AND status IN ('OPEN', 'PENDING')", (auth["client_code"],)).fetchone()[0]
    positions = await asyncio.to_thread(lambda: [position_view(row) for row in rows])
    used = round((account["used_funds"] if account else 0) + reserved, 2)
    net = round((account["available_balance"] if account else 0) - used, 2)
    realized = round(account["realized_pnl"] if account else 0, 2)
    unrealized = round(sum(row["unrealised"] for row in positions), 2)
    return ok({"net": net, "availablecash": net, "availableintradaypayin": 0, "availablelimitmargin": 0, "collateral": 0, "m2munrealized": unrealized, "m2mrealized": realized, "utiliseddebits": used, "utilisedcredits": 0, "spanmargin": 0, "exposuremargin": 0, "varmargin": 0, "adhocmargin": 0, "cashmarginavailable": net, "rmslimit": net, "unrealizedprofitandloss": unrealized, "realizedprofitandloss": realized, **pnl_values(realized, unrealized, account["total_charges"] if account else 0)})


async def order_book(request):
    auth = active_session(request)
    if auth is None:
        return failed("Invalid or expired token", 403)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM orders WHERE client_code=? ORDER BY id DESC", (auth["client_code"],)).fetchall()
    return ok([order_view(row) for row in rows])


async def trade_book(request):
    auth = active_session(request)
    if auth is None:
        return failed("Invalid or expired token", 403)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM trades WHERE client_code=? ORDER BY id DESC", (auth["client_code"],)).fetchall()
    return ok([trade_view(row) for row in rows])


async def positions(request):
    auth = active_session(request)
    if auth is None:
        return failed("Invalid or expired token", 403)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM positions WHERE client_code=? ORDER BY exchange, tradingsymbol", (auth["client_code"],)).fetchall()
    values = await asyncio.to_thread(lambda: [position_view(row) for row in rows])
    return ok(values)


async def holdings(request, all_holdings=False):
    auth = active_session(request)
    if auth is None:
        return failed("Invalid or expired token", 403)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM holdings WHERE client_code=? ORDER BY exchange, tradingsymbol", (auth["client_code"],)).fetchall()
    values = await asyncio.to_thread(lambda: [holding_view(row) for row in rows])
    if not all_holdings:
        return ok(values)
    value = round(sum(row["ltp"] * row["quantity"] for row in values), 2)
    invested = round(sum(row["averageprice"] * row["quantity"] for row in values), 2)
    pnl = round(value - invested, 2)
    return ok({"holdings": values, "totalholding": {"totalholdingvalue": value, "totalinvvalue": invested, "totalprofitandloss": pnl, "totalpnlpercentage": round(pnl * 100 / invested, 2) if invested else 0}})
