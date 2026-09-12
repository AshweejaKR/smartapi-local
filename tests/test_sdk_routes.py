"""Official SDK REST matrix, checked against upstream 7f10dad8 (2025-05-27).

The upstream individual_order_details wrapper ignores constructor ``root``.
That route uses its unchanged make_authenticated_get_request helper instead;
the URL is built from sdk.root. No SDK code, routes or transport is patched.
"""
from copy import deepcopy
from urllib.parse import urljoin

import pytest
from SmartApi import SmartConnect

from app import SDK_ROUTES, app


# Keep aliases separate: both SDK token helpers and both order helpers are run.
SDK_METHODS = {
    "api.login": ("POST", "generateSession"),
    "api.logout": ("POST", "terminateSession"),
    "api.token": ("POST", "generateToken"),
    "api.refresh": ("POST", "renewAccessToken"),
    "api.user.profile": ("GET", "getProfile"),
    "api.order.place": ("POST", "placeOrder"),
    "api.order.placefullresponse": ("POST", "placeOrderFullResponse"),
    "api.order.modify": ("POST", "modifyOrder"),
    "api.order.cancel": ("POST", "cancelOrder"),
    "api.order.book": ("GET", "orderBook"),
    "api.ltp.data": ("POST", "ltpData"),
    "api.trade.book": ("GET", "tradeBook"),
    "api.rms.limit": ("GET", "rmsLimit"),
    "api.holding": ("GET", "holding"),
    "api.position": ("GET", "position"),
    "api.convert.position": ("POST", "convertPosition"),
    "api.gtt.create": ("POST", "gttCreateRule"),
    "api.gtt.modify": ("POST", "gttModifyRule"),
    "api.gtt.cancel": ("POST", "gttCancelRule"),
    "api.gtt.details": ("POST", "gttDetails"),
    "api.gtt.list": ("POST", "gttLists"),
    "api.candle.data": ("POST", "getCandleData"),
    "api.oi.data": ("POST", "getOIData"),
    "api.market.data": ("POST", "getMarketData"),
    "api.search.scrip": ("POST", "searchScrip"),
    "api.allholding": ("GET", "allholding"),
    "api.individual.order.details": ("GET", "individual_order_details"),
    "api.margin.api": ("POST", "getMarginApi"),
    "api.estimateCharges": ("POST", "estimateCharges"),
    "api.verifyDis": ("POST", "verifyDis"),
    "api.generateTPIN": ("POST", "generateTPIN"),
    "api.getTranStatus": ("POST", "getTranStatus"),
    "api.optionGreek": ("POST", "optionGreek"),
    "api.gainersLosers": ("POST", "gainersLosers"),
    "api.putCallRatio": ("GET", "putCallRatio"),
    "api.oIBuildup": ("POST", "oIBuildup"),
    "api.nseIntraday": ("GET", "nseIntraday"),
    "api.bseIntraday": ("GET", "bseIntraday"),
}
ORDER = {
    "variety": "NORMAL", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045",
    "exchange": "NSE", "transactiontype": "BUY", "ordertype": "LIMIT",
    "producttype": "INTRADAY", "duration": "DAY", "price": "1", "quantity": "1",
}
GTT = {
    "tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "exchange": "NSE",
    "producttype": "MARGIN", "transactiontype": "BUY", "price": 95,
    "qty": 2, "disclosedqty": 2, "triggerprice": 96, "timeperiod": 365,
}
HISTORY = {
    "exchange": "NSE", "symboltoken": "3045", "interval": "ONE_MINUTE",
    "fromdate": "2026-09-08 10:00", "todate": "2026-09-08 10:02",
}


def data(response):
    assert isinstance(response, dict), response
    assert {"status", "message", "errorcode", "data"} <= response.keys()
    assert response["status"] is True, response
    assert response["errorcode"] == ""
    assert isinstance(response["message"], str)
    return response["data"]


def failure(response):
    assert response["status"] is False, response
    assert response["errorcode"]
    assert response["message"]
    assert "data" in response


def details(sdk, identifier):
    # This is the helper the official wrapper calls, with constructor root honored.
    path = sdk._routes["api.individual.order.details"] + identifier
    return sdk.make_authenticated_get_request(urljoin(sdk.root, path), sdk.access_token)


def test_inventory_matches_installed_official_sdk_and_registered_methods():
    assert set(SDK_METHODS) == set(SmartConnect._routes)
    expected = {}
    for key, path in SmartConnect._routes.items():
        path = "/" + path.lstrip("/")
        if key == "api.individual.order.details":
            path += "{order_id}"
        expected[key] = (SDK_METHODS[key][0], path)
        assert callable(getattr(SmartConnect, SDK_METHODS[key][1]))
    declared = {
        key.strip(): (method, path)
        for method, path, names in SDK_ROUTES for key in names.split(",")
    }
    assert declared == expected
    registered = {(method, route.path) for route in app.routes
                  for method in getattr(route, "methods", ())}
    assert set(expected.values()) <= registered


def test_every_sdk_route_over_real_http(sdk_server):
    sdk, covered = sdk_server.sdk, set()

    def call(key, *args):
        response = getattr(sdk, SDK_METHODS[key][1])(*deepcopy(args))
        covered.add(key)
        return response

    assert data(call("api.login", "DUMMY001", "password", "123456"))["clientcode"] == "DUMMY001"
    assert data(call("api.user.profile", sdk.refresh_token))["clientcode"] == "DUMMY001"
    assert data(call("api.token", sdk.refresh_token))["jwtToken"]
    renewed = call("api.refresh")
    assert renewed["clientcode"] == "DUMMY001" and renewed["refreshToken"]
    # Upstream renewAccessToken returns token metadata without updating access_token.
    data(call("api.token", renewed["refreshToken"]))
    assert data(call("api.rms.limit"))["availablecash"] >= 100
    first = call("api.order.place", ORDER)
    assert isinstance(first, str) and first
    placed = data(call("api.order.placefullresponse", ORDER))
    assert data(call("api.order.modify", {"orderid": first, "price": "2"}))["orderid"] == first
    assert data(call("api.order.cancel", first, "NORMAL"))["orderid"] == first
    assert {row["orderid"] for row in data(call("api.order.book"))} >= {first, placed["orderid"]}
    assert data(details(sdk, placed["uniqueorderid"]))["orderid"] == placed["orderid"]
    covered.add("api.individual.order.details")
    for key in ("api.trade.book", "api.position", "api.holding"):
        assert isinstance(data(call(key)), list)
    assert {"holdings", "totalholding"} <= data(call("api.allholding")).keys()
    filled = sdk.placeOrder({**ORDER, "price": "100"})
    sdk_server.wait_order(filled)
    data(call("api.convert.position", {
        "exchange": "NSE", "symboltoken": "3045", "oldproducttype": "INTRADAY",
        "newproducttype": "DELIVERY", "tradingsymbol": "SBIN-EQ",
        "transactiontype": "BUY", "quantity": 1, "type": "DAY",
    }))
    assert data(call("api.ltp.data", "NSE", "SBIN-EQ", "3045"))["ltp"] == 100
    assert data(call("api.market.data", "FULL", {"NSE": ["3045"]}))["fetched"]
    assert data(call("api.candle.data", HISTORY))
    assert data(call("api.oi.data", HISTORY))
    assert data(call("api.search.scrip", "NSE", "SBIN"))[0]["symboltoken"] == "3045"
    rule_id = call("api.gtt.create", GTT)
    assert isinstance(rule_id, str)
    assert call("api.gtt.modify", {**GTT, "id": rule_id, "price": 94}) == rule_id
    assert data(call("api.gtt.details", rule_id))["price"] == 94
    assert data(call("api.gtt.list", ["FORALL"], 1, 10))[0]["id"] == rule_id
    assert data(call("api.gtt.cancel", {"id": rule_id, "exchange": "NSE", "symboltoken": "3045"}))["id"] == rule_id
    assert data(call("api.margin.api", {"positions": [
        {"exchange": "NSE", "qty": 2, "price": 100, "productType": "DELIVERY"},
    ]}))["totalMarginRequired"] == 200
    assert data(call("api.estimateCharges", {"orders": [
        {"price": 100, "quantity": 2, "transaction_type": "BUY"},
    ]}))["summary"]["trade_value"] == 200
    verified = data(call("api.verifyDis", {"isin": "INE062A01020", "quantity": "2"}))
    assert {"ReqId", "ReturnURL", "DPId", "BOID", "TransDtls"} <= verified.keys()
    assert data(call("api.generateTPIN", {
        "dpId": verified["DPId"], "ReqId": verified["ReqId"],
        "boid": verified["BOID"], "pan": "ABCDE1234F",
    }))["status"] == "SUCCESS"
    assert data(call("api.getTranStatus", {"ReqId": verified["ReqId"]}))["TransResDtls"]["ReqId"] == verified["ReqId"]
    assert {row["optionType"] for row in data(call("api.optionGreek", {"name": "NIFTY", "expirydate": "24SEP2026"}))} == {"CE", "PE"}
    assert data(call("api.gainersLosers", {"datatype": "PercOIGainers", "expirytype": "NEAR"}))
    assert data(call("api.putCallRatio"))[0]["pcr"] > 0
    assert data(call("api.oIBuildup", {"datatype": "Long Built Up", "expirytype": "NEAR"}))
    for key, exchange in (("api.nseIntraday", "NSE"), ("api.bseIntraday", "BSE")):
        assert all(row["exchange"] == exchange for row in data(call(key)))
    assert data(call("api.logout", "DUMMY001")) is None
    assert covered == set(SmartConnect._routes)


@pytest.mark.parametrize("method,path", sorted({
    (method, path.replace("{order_id}", "missing"))
    for method, path, _ in SDK_ROUTES if "/secure/" in path
}))
def test_every_secure_route_rejects_missing_authentication(sdk_server, method, path):
    response = sdk_server.http.request(method, path)
    assert response.status_code == 403, response.text
    failure(response.json())
    assert response.json()["errorcode"] == "AG8001"


def test_gtt_lifecycle_pagination_ownership_and_restart(sdk_server):
    sdk = sdk_server.sdk
    first = sdk.gttCreateRule(deepcopy(GTT))
    second = sdk.gttCreateRule({**GTT, "price": 90})
    assert first != second
    assert [row["id"] for row in data(sdk.gttLists(["NEW"], 2, 1))] == [second]
    assert sdk.gttModifyRule({"id": first, "price": 92}) == first
    assert data(sdk.gttDetails(first))["price"] == 92
    assert data(sdk.gttDetails(first))["qty"] == GTT["qty"]
    assert sdk_server.http.post("/admin/users/add", data={
        "client_code": "GTT002", "password": "password", "api_key": "GTT_KEY",
        "totp": "123456", "name": "Second User", "email": "gtt@example.test",
        "mobile": "9000000001",
    }).status_code == 303
    other = SmartConnect(api_key="GTT_KEY", root=sdk.root)
    data(other.generateSession("GTT002", "password", "123456"))
    assert data(other.gttLists(["FORALL"], 1, 10)) == []
    failure(other.gttDetails(first))
    failure(other.gttCancelRule({"id": first}))
    # Create/modify helpers extract ['data']['id'] even on errors. The official
    # generic dispatcher exposes their original failure envelope unchanged.
    failure(other._postRequest("api.gtt.modify", {"id": first, "price": 1}))
    assert data(sdk.gttDetails(first))["price"] == 92
    data(sdk.gttCancelRule({"id": first}))
    assert data(sdk.gttDetails(first))["status"] == "CANCELLED"
    assert [row["id"] for row in data(sdk.gttLists(["CANCELLED"], 1, 10))] == [first]
    before = data(sdk.gttLists(["FORALL"], 1, 10))
    sdk_server.restart()
    assert data(sdk.gttLists(["FORALL"], 1, 10)) == before


def test_deterministic_oi_and_derivative_data(sdk_server):
    sdk = sdk_server.sdk
    oi = data(sdk.getOIData(deepcopy(HISTORY)))
    assert oi == data(sdk.getOIData(deepcopy(HISTORY)))
    assert len(oi) == 3
    assert [row["time"] for row in oi] == [f"2026-09-08T10:{minute:02}:00+05:30" for minute in (0, 1, 2)]
    assert all(isinstance(row["oi"], int) and row["oi"] >= 0 for row in oi)
    gainers = data(sdk.gainersLosers({"datatype": "PercPriceGainers", "expirytype": "NEXT"}))
    losers = data(sdk.gainersLosers({"datatype": "PercPriceLosers", "expirytype": "NEXT"}))
    assert gainers and losers
    assert all(row["percentChange"] > 0 for row in gainers)
    assert all(row["percentChange"] < 0 for row in losers)
    assert data(sdk.putCallRatio()) == data(sdk.putCallRatio())
    assert all(float(row["netChange"]) < 0 for row in data(sdk.oIBuildup({"datatype": "Short Built Up", "expirytype": "FAR"})))
    assert data(sdk.searchScrip("NSE", "DOES-NOT-EXIST")) == []


@pytest.mark.parametrize("method,args", [
    ("getOIData", ({**HISTORY, "interval": "INVALID"},)),
    ("getOIData", ({**HISTORY, "todate": "2026-09-07 09:15"},)),
    ("gttDetails", ("missing",)),
    ("gttLists", (["NEW"], "invalid", 10)),
    ("getMarginApi", ({"positions": [None]},)),
    ("getMarginApi", ({"positions": [{}] * 51},)),
    ("estimateCharges", ({"orders": [None]},)),
    ("verifyDis", ({},)),
    ("generateTPIN", ({},)),
    ("getTranStatus", ({},)),
    ("optionGreek", ({},)),
    ("gainersLosers", ({"datatype": "INVALID", "expirytype": "NEAR"},)),
    ("oIBuildup", ({"datatype": "Long Built Up", "expirytype": "INVALID"},)),
    ("searchScrip", ("", "SBIN")),
])
def test_remaining_route_validation_returns_sdk_error_envelopes(sdk_server, method, args):
    failure(getattr(sdk_server.sdk, method)(*deepcopy(args)))


def test_individual_order_identifiers_and_ownership(sdk_server):
    sdk = sdk_server.sdk
    placed = data(sdk.placeOrderFullResponse(deepcopy(ORDER)))
    for key in ("orderid", "uniqueorderid"):
        assert data(details(sdk, placed[key]))["orderid"] == placed["orderid"]
    # Generic helper intentionally returns None on non-200 responses upstream.
    assert details(sdk, "missing") is None
    sdk_server.http.post("/admin/users/add", data={
        "client_code": "ORDER002", "password": "password", "api_key": "ORDER_KEY",
        "totp": "123456", "name": "Other Order User", "email": "order@example.test",
        "mobile": "9000000002",
    })
    other = SmartConnect(api_key="ORDER_KEY", root=sdk.root)
    data(other.generateSession("ORDER002", "password", "123456"))
    assert data(other.orderBook()) == []
    assert details(other, placed["orderid"]) is None
    assert details(other, placed["uniqueorderid"]) is None
    failure(other.modifyOrder({"orderid": placed["orderid"], "price": "2"}))
    failure(other.cancelOrder(placed["orderid"], "NORMAL"))


def test_position_conversion_moves_quantity_and_cost_without_new_trades(sdk_server):
    sdk = sdk_server.sdk
    first = sdk.placeOrder({**ORDER, "price": "100", "quantity": "4"})
    sdk_server.wait_order(first)
    sdk_server.hijack(120)
    second = sdk.placeOrder({**ORDER, "price": "120", "quantity": "2", "producttype": "DELIVERY"})
    sdk_server.wait_order(second)
    trades, funds = data(sdk.tradeBook()), data(sdk.rmsLimit())
    conversion = {
        "exchange": "NSE", "symboltoken": "3045", "transactiontype": "BUY",
        "oldproducttype": "INTRADAY", "newproducttype": "DELIVERY", "quantity": 2,
    }
    assert data(sdk.convertPosition(conversion)) is None
    positions = {row["producttype"]: row for row in data(sdk.position())}
    assert positions["INTRADAY"]["netqty"] == 2
    assert positions["INTRADAY"]["avgnetprice"] == 100
    assert positions["DELIVERY"]["netqty"] == 4
    assert positions["DELIVERY"]["avgnetprice"] == 110
    assert sum(row["buyqty"] for row in positions.values()) == 6
    assert sum(row["buyamount"] for row in positions.values()) == 640
    holding = data(sdk.holding())[0]
    assert holding["quantity"] == 4 and holding["averageprice"] == 110
    assert data(sdk.rmsLimit()) == funds
    assert data(sdk.tradeBook()) == trades
    data(sdk.convertPosition({
        **conversion, "oldproducttype": "DELIVERY", "newproducttype": "INTRADAY",
        "quantity": 1,
    }))
    assert data(sdk.holding())[0]["quantity"] == 3
    assert data(sdk.holding())[0]["averageprice"] == 110
    assert data(sdk.tradeBook()) == trades
    before = data(sdk.position())
    sdk_server.restart()
    assert data(sdk.position()) == before
    assert data(sdk.rmsLimit())["availablecash"] == funds["availablecash"]


def test_position_conversion_rejects_invalid_or_unowned_quantities(sdk_server):
    sdk = sdk_server.sdk
    order_id = sdk.placeOrder({**ORDER, "price": "100", "quantity": "2"})
    sdk_server.wait_order(order_id)
    conversion = {
        "exchange": "NSE", "symboltoken": "3045", "transactiontype": "BUY",
        "oldproducttype": "INTRADAY", "newproducttype": "DELIVERY", "quantity": 1,
    }
    before = data(sdk.position()), data(sdk.holding()), data(sdk.rmsLimit())
    for changed in ({"quantity": 0}, {"quantity": -1}, {"quantity": 3},
                    {"quantity": "invalid"}, {"quantity": 1.5},
                    {"oldproducttype": "INVALID"}, {"newproducttype": "INTRADAY"},
                    {"transactiontype": "SELL"}, {"symboltoken": "2885"}):
        failure(sdk.convertPosition({**conversion, **changed}))
    assert (data(sdk.position()), data(sdk.holding()), data(sdk.rmsLimit())) == before
    sdk_server.http.post("/admin/users/add", data={
        "client_code": "CONVERT002", "password": "password", "api_key": "CONVERT_KEY",
        "totp": "123456", "name": "Other User", "email": "convert@example.test",
        "mobile": "9000000003",
    })
    other = SmartConnect(api_key="CONVERT_KEY", root=sdk.root)
    data(other.generateSession("CONVERT002", "password", "123456"))
    failure(other.convertPosition(conversion))
    assert data(other.position()) == []
