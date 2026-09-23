# -*- coding: utf-8 -*-
"""Simple sequential real-versus-local SmartAPI smoke test."""

from copy import deepcopy
from os import getenv
from time import sleep

from pyotp import TOTP
from SmartApi import SmartConnect

SYMBOL = "NIFTYBEES-EQ"
EXCHANGE = "NSE"
QTY = 1
DELAY = 1
LOCAL_ROOT = "http://127.0.0.1:8000"  # Or http://13.233.59.93:8000
LOCAL_MODE = getenv("SMARTAPI_LOCAL_MODE", "angelone").strip().lower()
LOCAL_DUMMY = ("DUMMY_API_KEY", "DUMMY001", "password", "123456")


def load_credentials(path="credentials.txt"):
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def call(client, method, args):
    try:
        return getattr(client, method)(*deepcopy(args))
    except Exception as exc:
        return {"exception": str(exc)}


def compare(label, method, real_args=(), local_args=None):
    local_args = real_args if local_args is None else local_args
    print(f"{label} real_args:", real_args)
    print(f"{label} local_args:", local_args)
    real = call(client, method, real_args)
    sleep(DELAY)
    local = call(client_2, method, local_args)
    print(f"{label} real:", real)
    print(f"{label} local:", local)
    print(f"{label} same:", real == local)
    return real, local


def response_value(response, *keys):
    value = response
    for key in keys:
        value = value[key]
    return value


creds = load_credentials()
if LOCAL_MODE not in {"angelone", "none"}:
    raise SystemExit("SMARTAPI_LOCAL_MODE must be angelone or none")
client = SmartConnect(api_key=creds["API_KEY"])
local_api_key = creds["API_KEY"] if LOCAL_MODE == "angelone" else LOCAL_DUMMY[0]
client_2 = SmartConnect(api_key=local_api_key, root=LOCAL_ROOT)
print("local mode:", LOCAL_MODE)

# 1. generateSession
totp = TOTP(creds["TOTP_SECRET"]).now()
real_login = (creds["CLIENT_ID"], creds["PASSWORD"], totp)
local_login = real_login if LOCAL_MODE == "angelone" else LOCAL_DUMMY[1:]
real_session, local_session = compare(
    "generateSession", "generateSession",
    real_login, local_login,
)
try:
    refresh_token = response_value(real_session, "data", "refreshToken")
    refresh_token_2 = response_value(local_session, "data", "refreshToken")
except (KeyError, TypeError):
    raise SystemExit(1)
sleep(DELAY)

# 2. getProfile
compare(
    "getProfile", "getProfile", (refresh_token,), (refresh_token_2,),
)
sleep(DELAY)

# lookup symboltoken needed to place orders for NIFTYBEES-EQ
real_search, local_search = compare(
    "symbol lookup", "searchScrip", (EXCHANGE, SYMBOL),
)
try:
    symbol_token = response_value(real_search, "data")[0]["symboltoken"]
    symbol_token_2 = response_value(local_search, "data")[0]["symboltoken"]
except (KeyError, TypeError, IndexError):
    raise SystemExit(1)
print("symbol token same:", symbol_token == symbol_token_2)
sleep(DELAY)

order_params = {
    "variety": "NORMAL",
    "tradingsymbol": SYMBOL,
    "symboltoken": symbol_token,
    "transactiontype": "BUY",
    "exchange": EXCHANGE,
    "ordertype": "MARKET",
    "producttype": "DELIVERY",
    "duration": "DAY",
    "price": 0,
    "quantity": QTY,
}
local_order_params = order_params | {"symboltoken": symbol_token_2}

# 3. placeOrder - BUY. This makes two real orders.
compare("placeOrder BUY", "placeOrder", (order_params,), (local_order_params,))
sleep(DELAY)

# 4. orderBook, tradeBook
compare("orderBook", "orderBook")
sleep(DELAY)
compare("tradeBook", "tradeBook")
sleep(DELAY)

# 5. position, holding
compare("position", "position")
sleep(DELAY)
compare("holding", "holding")
sleep(DELAY)

# 6. placeOrder - SELL. This makes two real orders.
order_params["transactiontype"] = "SELL"
local_order_params["transactiontype"] = "SELL"
compare("placeOrder SELL", "placeOrder", (order_params,), (local_order_params,))
sleep(DELAY)

# 7. orderBook, tradeBook
compare("orderBook", "orderBook")
sleep(DELAY)
compare("tradeBook", "tradeBook")
sleep(DELAY)

# 8. position, holding
compare("position", "position")
sleep(DELAY)
compare("holding", "holding")
sleep(DELAY)

# 9. searchScrip
compare("searchScrip", "searchScrip", (EXCHANGE, "NIFTYBEES"))
sleep(DELAY)

# 10. ltpData
compare("ltpData", "ltpData", (EXCHANGE, SYMBOL, symbol_token), (EXCHANGE, SYMBOL, symbol_token_2))
sleep(DELAY)

# 11. getCandleData
candle_params = {
    "exchange": EXCHANGE,
    "symboltoken": symbol_token,
    "interval": "ONE_DAY",
    "fromdate": "2026-09-01 09:15",
    "todate": "2026-09-20 15:30",
}
local_candle_params = candle_params | {"symboltoken": symbol_token_2}
compare("getCandleData", "getCandleData", (candle_params,), (local_candle_params,))
sleep(DELAY)

# 12. getMarginApi for 1 qty
margin_params = {
    "positions": [{
        "exchange": EXCHANGE,
        "qty": QTY,
        "price": 0,
        "productType": "DELIVERY",
        "token": symbol_token,
        "tradeType": "BUY",
        "orderType": "MARKET",
    }],
}
local_margin_params = deepcopy(margin_params)
local_margin_params["positions"][0]["token"] = symbol_token_2
compare("getMarginApi 1 qty", "getMarginApi", (margin_params,), (local_margin_params,))
sleep(DELAY)

# 13. getMarginApi for 10 qty
margin_params["positions"][0]["qty"] = QTY * 10
local_margin_params["positions"][0]["qty"] = QTY * 10
compare("getMarginApi 10 qty", "getMarginApi", (margin_params,), (local_margin_params,))
sleep(DELAY)

# 14. estimateCharges 1 order
QTY = 1
PRICE = 100
charge_order = {
    "product_type": "DELIVERY",
    "transaction_type": "BUY",
    "quantity": str(QTY),
    "price": str(PRICE),
    "exchange": EXCHANGE,
    "symbol_name": SYMBOL,
    "token": symbol_token,
}
local_charge_order = charge_order | {"token": symbol_token_2}
compare("estimateCharges 1 order", "estimateCharges", ({"orders": [charge_order]},), ({"orders": [local_charge_order]},))
sleep(DELAY)

# 15. estimateCharges 2 same orders
QTY *= 10
charge_order["quantity"] = str(QTY)
local_charge_order["quantity"] = str(QTY)
compare(
    "estimateCharges 2 same orders", "estimateCharges",
    ({"orders": [charge_order, charge_order]},),
    ({"orders": [local_charge_order, local_charge_order]},),
)
sleep(DELAY)

# 16. estimateCharges 2 different orders
SYMBOL_2 = "SBIN-EQ"
real_search, local_search = compare(
    "symbol lookup SBIN", "searchScrip", (EXCHANGE, SYMBOL_2),
)
try:
    symbol_token_2 = response_value(real_search, "data")[0]["symboltoken"]
    local_symbol_token_2 = response_value(local_search, "data")[0]["symboltoken"]
except (KeyError, TypeError, IndexError):
    raise SystemExit(1)
charge_order_2 = charge_order | {"symbol_name": SYMBOL_2, "token": symbol_token_2}
local_charge_order_2 = local_charge_order | {"symbol_name": SYMBOL_2, "token": local_symbol_token_2}
compare(
    "estimateCharges 2 different orders", "estimateCharges",
    ({"orders": [charge_order, charge_order_2]},),
    ({"orders": [local_charge_order, local_charge_order_2]},),
)
