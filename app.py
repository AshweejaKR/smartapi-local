"""Small local SmartAPI-compatible REST server."""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
import sqlite3

from admin import init_admin, record_audit, router as admin_router
import angelone_proxy
from angelone_proxy import AngelOneRemoteError, PROXY
from auth import active_session, failed, generate_tokens, init_auth, login, logout, payload, profile
from charges import init_charges
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fault import active_fault, fault_response, init_faults, slow_delay_seconds
from market import candle_data, init_market, ltp_data, market_data
from orders import (
    cancel_order, init_orders, modify_order, order_checker, place_order, stop_checker,
)
import extra_routes
from extra_routes import init_extra_routes
from portfolio import holdings, init_portfolio, order_book, positions, rms_limit, trade_book
from rate_limit import client_code_for, init_rate_limits, limiter
from server_config import init_config, is_angel, transparent_angel_proxy_enabled


BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "smartapi_local.db"


def init_db():
    """Create the local database and current phase tables."""
    init_config(BASE_DIR)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS server_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO server_meta(key, value) VALUES ('schema_version', '1')"
        )

    init_auth(DB_PATH)
    init_portfolio(DB_PATH)
    init_admin(DB_PATH)
    init_market(DB_PATH)
    with sqlite3.connect(DB_PATH) as conn:
        init_charges(conn)
    init_orders(DB_PATH)
    init_extra_routes(DB_PATH)
    init_rate_limits(DB_PATH)
    init_faults(DB_PATH)


def _cli_value(name, default):
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return default


def server_addresses():
    host = os.getenv("SMARTAPI_HOST", _cli_value("--host", "127.0.0.1"))
    port = int(os.getenv("SMARTAPI_PORT", _cli_value("--port", "8000")))
    public = os.getenv("SMARTAPI_PUBLIC_HOST", "").strip()
    base = f"http://{public or '127.0.0.1'}:{port}"
    print("\n" + "=" * 44)
    print(" SmartAPI Local Server")
    print("=" * 44)
    print(f"Bind       : {host}:{port}")
    print(f"Local      : http://127.0.0.1:{port}")
    if public:
        print(f"Public     : http://{public}:{port}")
    print(f"Admin      : {base}/admin")
    print(f"Health     : {base}/health")
    print("=" * 44 + "\n")


def success(data=None, message="SUCCESS"):
    return {"status": True, "message": message, "errorcode": "", "data": data}


def error(message="Request failed", errorcode="AB1000", status_code=400, data=None):
    return JSONResponse(
        status_code=status_code,
        content={"status": False, "message": message, "errorcode": errorcode, "data": data},
    )


# Method/path inventory from angel-one/smartapi-python SmartConnect._routes.
# Paths without a leading slash upstream resolve to these same paths via urljoin.
SDK_ROUTES = [
    ("POST", "/rest/auth/angelbroking/user/v1/loginByPassword", "api.login"),
    ("POST", "/rest/secure/angelbroking/user/v1/logout", "api.logout"),
    ("POST", "/rest/auth/angelbroking/jwt/v1/generateTokens", "api.token, api.refresh"),
    ("GET", "/rest/secure/angelbroking/user/v1/getProfile", "api.user.profile"),
    ("POST", "/rest/secure/angelbroking/order/v1/placeOrder", "api.order.place, api.order.placefullresponse"),
    ("POST", "/rest/secure/angelbroking/order/v1/modifyOrder", "api.order.modify"),
    ("POST", "/rest/secure/angelbroking/order/v1/cancelOrder", "api.order.cancel"),
    ("GET", "/rest/secure/angelbroking/order/v1/getOrderBook", "api.order.book"),
    ("POST", "/rest/secure/angelbroking/order/v1/getLtpData", "api.ltp.data"),
    ("GET", "/rest/secure/angelbroking/order/v1/getTradeBook", "api.trade.book"),
    ("GET", "/rest/secure/angelbroking/user/v1/getRMS", "api.rms.limit"),
    ("GET", "/rest/secure/angelbroking/portfolio/v1/getHolding", "api.holding"),
    ("GET", "/rest/secure/angelbroking/order/v1/getPosition", "api.position"),
    ("POST", "/rest/secure/angelbroking/order/v1/convertPosition", "api.convert.position"),
    ("POST", "/gtt-service/rest/secure/angelbroking/gtt/v1/createRule", "api.gtt.create"),
    ("POST", "/gtt-service/rest/secure/angelbroking/gtt/v1/modifyRule", "api.gtt.modify"),
    ("POST", "/gtt-service/rest/secure/angelbroking/gtt/v1/cancelRule", "api.gtt.cancel"),
    ("POST", "/rest/secure/angelbroking/gtt/v1/ruleDetails", "api.gtt.details"),
    ("POST", "/rest/secure/angelbroking/gtt/v1/ruleList", "api.gtt.list"),
    ("POST", "/rest/secure/angelbroking/historical/v1/getCandleData", "api.candle.data"),
    ("POST", "/rest/secure/angelbroking/historical/v1/getOIData", "api.oi.data"),
    ("POST", "/rest/secure/angelbroking/market/v1/quote", "api.market.data"),
    ("POST", "/rest/secure/angelbroking/order/v1/searchScrip", "api.search.scrip"),
    ("GET", "/rest/secure/angelbroking/portfolio/v1/getAllHolding", "api.allholding"),
    ("GET", "/rest/secure/angelbroking/order/v1/details/{order_id}", "api.individual.order.details"),
    ("POST", "/rest/secure/angelbroking/margin/v1/batch", "api.margin.api"),
    ("POST", "/rest/secure/angelbroking/brokerage/v1/estimateCharges", "api.estimateCharges"),
    ("POST", "/rest/secure/angelbroking/edis/v1/verifyDis", "api.verifyDis"),
    ("POST", "/rest/secure/angelbroking/edis/v1/generateTPIN", "api.generateTPIN"),
    ("POST", "/rest/secure/angelbroking/edis/v1/getTranStatus", "api.getTranStatus"),
    ("POST", "/rest/secure/angelbroking/marketData/v1/optionGreek", "api.optionGreek"),
    ("POST", "/rest/secure/angelbroking/marketData/v1/gainersLosers", "api.gainersLosers"),
    ("GET", "/rest/secure/angelbroking/marketData/v1/putCallRatio", "api.putCallRatio"),
    ("POST", "/rest/secure/angelbroking/marketData/v1/OIBuildup", "api.oIBuildup"),
    ("GET", "/rest/secure/angelbroking/marketData/v1/nseIntraday", "api.nseIntraday"),
    ("GET", "/rest/secure/angelbroking/marketData/v1/bseIntraday", "api.bseIntraday"),
]


CORE_HANDLERS = {
    "/rest/auth/angelbroking/user/v1/loginByPassword": login,
    "/rest/auth/angelbroking/jwt/v1/generateTokens": generate_tokens,
    "/rest/secure/angelbroking/user/v1/getProfile": profile,
    "/rest/secure/angelbroking/user/v1/logout": logout,
    "/rest/secure/angelbroking/order/v1/getLtpData": ltp_data,
    "/rest/secure/angelbroking/market/v1/quote": market_data,
    "/rest/secure/angelbroking/historical/v1/getCandleData": candle_data,
    "/rest/secure/angelbroking/order/v1/getOrderBook": order_book,
    "/rest/secure/angelbroking/order/v1/getTradeBook": trade_book,
    "/rest/secure/angelbroking/order/v1/placeOrder": place_order,
    "/rest/secure/angelbroking/order/v1/modifyOrder": modify_order,
    "/rest/secure/angelbroking/order/v1/cancelOrder": cancel_order,
    "/rest/secure/angelbroking/user/v1/getRMS": rms_limit,
    "/rest/secure/angelbroking/order/v1/getPosition": positions,
}

MARKET_PROXY_PATHS = {
    "/rest/secure/angelbroking/order/v1/getLtpData",
    "/rest/secure/angelbroking/market/v1/quote",
    "/rest/secure/angelbroking/historical/v1/getCandleData",
    "/rest/secure/angelbroking/historical/v1/getOIData",
}
ORDER_PROXY_PATHS = {
    "/rest/secure/angelbroking/order/v1/placeOrder",
    "/rest/secure/angelbroking/order/v1/modifyOrder",
    "/rest/secure/angelbroking/order/v1/cancelOrder",
    "/rest/secure/angelbroking/order/v1/getOrderBook",
    "/rest/secure/angelbroking/order/v1/getTradeBook",
    "/rest/secure/angelbroking/order/v1/getPosition",
    "/rest/secure/angelbroking/portfolio/v1/getHolding",
    "/rest/secure/angelbroking/portfolio/v1/getAllHolding",
    "/rest/secure/angelbroking/order/v1/convertPosition",
    "/rest/secure/angelbroking/order/v1/details/{order_id}",
}
ACCOUNT_PROXY_PATHS = {
    "/rest/secure/angelbroking/user/v1/getRMS",
    "/rest/secure/angelbroking/margin/v1/batch",
}


def proxy_selected(path):
    if path in MARKET_PROXY_PATHS:
        return is_angel("market_data_source")
    if path in ORDER_PROXY_PATHS:
        return is_angel("order_data")
    return path in ACCOUNT_PROXY_PATHS and is_angel("account_data")


async def angel_response(request, path):
    if active_session(request) is None:
        return failed("Invalid or expired token", 403)
    try:
        data = await payload(request)
        result = await asyncio.to_thread(
            PROXY.forward, path, data, request.path_params.get("order_id"),
        )
        return Response(
            content=result.content, status_code=result.status_code,
            headers={"content-type": result.content_type},
        )
    except AngelOneRemoteError as exc:
        return Response(
            content=exc.reply.content, status_code=exc.reply.status_code,
            headers={"content-type": exc.reply.content_type},
        )
    except Exception:
        return error("Angel One request is unavailable", "AB2001", 503)


async def transparent_angel_response(request):
    """Return the real broker response for an untouched client request."""
    try:
        result = await asyncio.to_thread(
            angelone_proxy.forward_transparent,
            request.method,
            request.url.path,
            request.scope["query_string"].decode("latin-1"),
            dict(request.headers),
            await request.body(),
        )
        return Response(
            content=result.content, status_code=result.status_code,
            headers={"content-type": result.content_type},
        )
    except Exception:
        return error("Angel One request is unavailable", "AB2001", 503)


async def dispatch_rest(request: Request):
    if transparent_angel_proxy_enabled():
        return await transparent_angel_response(request)
    path = request.scope["route"].path
    if proxy_selected(path):
        return await angel_response(request, path)
    if handler := CORE_HANDLERS.get(path):
        return await handler(request)
    if path in {
        "/rest/secure/angelbroking/portfolio/v1/getHolding",
        "/rest/secure/angelbroking/portfolio/v1/getAllHolding",
    }:
        return await holdings(request, path.endswith("getAllHolding"))
    if path == "/rest/secure/angelbroking/order/v1/details/{order_id}":
        return await extra_routes.individual_order_details(request)
    return await extra_routes.HANDLERS[path](request)


async def dispatch_unknown_rest(request: Request):
    if transparent_angel_proxy_enabled():
        return await transparent_angel_response(request)
    return error("Endpoint is not supported", "AB1000", 404)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if os.getenv("SMARTAPI_STARTUP_BANNER", "1").lower() not in {"0", "false", "no"}:
        server_addresses()
    checker = asyncio.create_task(order_checker())
    try:
        yield
    finally:
        await stop_checker(checker)


app = FastAPI(title="Local SmartAPI", lifespan=lifespan)
app.include_router(admin_router)


@app.middleware("http")
async def smartapi_faults(request: Request, call_next):
    if (not transparent_angel_proxy_enabled()
            and request.url.path.startswith(("/rest/", "/gtt-service/rest/"))):
        fault = active_fault()
        if fault:
            request.state.fault_mode = fault["mode"]
            if fault["mode"] == "slow":
                await asyncio.sleep(slow_delay_seconds())
            else:
                return fault_response(fault["mode"])
    return await call_next(request)


@app.middleware("http")
async def smartapi_rate_limit(request: Request, call_next):
    if (not transparent_angel_proxy_enabled()
            and request.url.path.startswith(("/rest/", "/gtt-service/rest/"))):
        failure = limiter.check(request.url.path, request.state.audit_client_code)
        if failure:
            request.state.rate_limited = True
            return failure
    return await call_next(request)


@app.middleware("http")
async def smartapi_audit(request: Request, call_next):
    if not request.url.path.startswith(("/rest/", "/gtt-service/rest/")):
        return await call_next(request)
    status_code = 500
    request.state.audit_client_code = "anonymous"
    try:
        request.state.audit_client_code = await client_code_for(request)
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        # Never persist headers, query strings, request bodies or token responses.
        detail = f"{request.method} {request.url.path} HTTP {status_code}"
        with sqlite3.connect(DB_PATH) as conn:
            record_audit(conn, "rest_request", detail, request.state.audit_client_code)
            if request.url.path.endswith("/loginByPassword"):
                record_audit(conn, "login_attempt", detail, request.state.audit_client_code)
            if getattr(request.state, "rate_limited", False):
                record_audit(conn, "rate_limit", detail, request.state.audit_client_code)
            if mode := getattr(request.state, "fault_mode", None):
                record_audit(conn, "fault_request", f"{mode}: {detail}", request.state.audit_client_code)


@app.get("/health")
async def health():
    return success({"service": "smartapi-local"})


for method, path, name in SDK_ROUTES:
    app.add_api_route(path, dispatch_rest, methods=[method], name=name)


app.add_api_route("/rest/{path:path}", dispatch_unknown_rest,
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
app.add_api_route("/gtt-service/{path:path}", dispatch_unknown_rest,
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("SMARTAPI_HOST", "127.0.0.1"),
        port=int(os.getenv("SMARTAPI_PORT", "8000")),
    )
