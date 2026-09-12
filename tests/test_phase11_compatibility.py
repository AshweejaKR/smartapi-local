"""Route/method matrix for the current SmartConnect REST surface."""
from app import SDK_ROUTES, app
import phase11


PHASE11_ROUTE_MATRIX = [
    ("POST", "/rest/secure/angelbroking/order/v1/convertPosition", "convertPosition"),
    ("POST", "/gtt-service/rest/secure/angelbroking/gtt/v1/createRule", "gttCreateRule"),
    ("POST", "/gtt-service/rest/secure/angelbroking/gtt/v1/modifyRule", "gttModifyRule"),
    ("POST", "/gtt-service/rest/secure/angelbroking/gtt/v1/cancelRule", "gttCancelRule"),
    ("POST", "/rest/secure/angelbroking/gtt/v1/ruleDetails", "gttDetails"),
    ("POST", "/rest/secure/angelbroking/gtt/v1/ruleList", "gttLists"),
    ("POST", "/rest/secure/angelbroking/historical/v1/getOIData", "getOIData"),
    ("POST", "/rest/secure/angelbroking/order/v1/searchScrip", "searchScrip"),
    ("GET", "/rest/secure/angelbroking/order/v1/details/{order_id}", "individual_order_details"),
    ("POST", "/rest/secure/angelbroking/margin/v1/batch", "getMarginApi"),
    ("POST", "/rest/secure/angelbroking/brokerage/v1/estimateCharges", "estimateCharges"),
    ("POST", "/rest/secure/angelbroking/edis/v1/verifyDis", "verifyDis"),
    ("POST", "/rest/secure/angelbroking/edis/v1/generateTPIN", "generateTPIN"),
    ("POST", "/rest/secure/angelbroking/edis/v1/getTranStatus", "getTranStatus"),
    ("POST", "/rest/secure/angelbroking/marketData/v1/optionGreek", "optionGreek"),
    ("POST", "/rest/secure/angelbroking/marketData/v1/gainersLosers", "gainersLosers"),
    ("GET", "/rest/secure/angelbroking/marketData/v1/putCallRatio", "putCallRatio"),
    ("POST", "/rest/secure/angelbroking/marketData/v1/OIBuildup", "oIBuildup"),
    ("GET", "/rest/secure/angelbroking/marketData/v1/nseIntraday", "nseIntraday"),
    ("GET", "/rest/secure/angelbroking/marketData/v1/bseIntraday", "bseIntraday"),
]


def test_current_sdk_routes_are_registered():
    registered = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }
    assert {(path, method) for method, path, _ in SDK_ROUTES} <= registered


def test_phase11_routes_are_wired_to_handlers():
    wired = set(phase11.HANDLERS)
    wired.add("/rest/secure/angelbroking/order/v1/details/{order_id}")
    assert {(path, method) for method, path, _ in PHASE11_ROUTE_MATRIX}
    assert {path for _, path, _ in PHASE11_ROUTE_MATRIX} <= wired
