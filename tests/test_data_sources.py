from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import yaml

import angelone_proxy
import app as app_module
import market


LOGIN = {
    "headers": {"X-PrivateKey": "DUMMY_API_KEY"},
    "json": {"clientcode": "DUMMY001", "password": "password", "totp": "123456"},
}


def config_file(tmp_path, **values):
    path = tmp_path / "source.yaml"
    path.write_text(yaml.safe_dump({
        "market_data_source": values.get("market", "yahoo"),
        "order_data": values.get("order"),
        "account_data": values.get("account"),
        "credentials_file": "angelone_keys.env",
    }), encoding="utf-8")
    return path


def local_headers(client):
    response = client.post("/rest/auth/angelbroking/user/v1/loginByPassword", **LOGIN)
    return {"Authorization": f"Bearer {response.json()['data']['jwtToken']}"}


def test_dummy_source_returns_fixed_price_and_25_candles(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "dummy.db")
    monkeypatch.setenv("SMARTAPI_CONFIG_FILE", str(config_file(tmp_path, market=None)))
    with TestClient(app_module.app) as client:
        headers = local_headers(client)
        ltp = client.post(
            "/rest/secure/angelbroking/order/v1/getLtpData", headers=headers,
            json={"exchange": "NSE", "tradingsymbol": "ANY-EQ", "symboltoken": "123"},
        ).json()
        candles = client.post(
            "/rest/secure/angelbroking/historical/v1/getCandleData", headers=headers,
            json={"exchange": "NSE", "symboltoken": "123", "interval": "ONE_MINUTE",
                  "fromdate": "2026-09-08 09:15", "todate": "2026-09-08 09:40"},
        ).json()
    assert ltp["status"] and ltp["data"]["ltp"] == 100.05
    assert len(candles["data"]) == 25
    assert all(row[1:5] == [100.05] * 4 for row in candles["data"])


def test_selected_angel_routes_forward_without_local_state(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "angel.db")
    monkeypatch.setenv("SMARTAPI_CONFIG_FILE", str(config_file(
        tmp_path, market="angelone", order="angelone", account="angelone",
    )))
    calls = []

    def forward(path, data=None, order_id=None):
        calls.append((path, data, order_id))
        return angelone_proxy.AngelOneReply(
            200, ('{"status":true,"message":"SUCCESS","errorcode":"","data":{"remote":"' + path + '"}}').encode(),
        )

    monkeypatch.setattr(angelone_proxy.PROXY, "forward", forward)
    with TestClient(app_module.app) as client:
        headers = local_headers(client)
        paths = [
            ("/rest/secure/angelbroking/order/v1/getLtpData", {"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045"}),
            ("/rest/secure/angelbroking/order/v1/placeOrder", {"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "quantity": "1"}),
            ("/rest/secure/angelbroking/order/v1/getPosition", None),
            ("/rest/secure/angelbroking/portfolio/v1/getHolding", None),
            ("/rest/secure/angelbroking/user/v1/getRMS", None),
        ]
        for path, data in paths:
            response = client.post(path, headers=headers, json=data) if data is not None else client.get(path, headers=headers)
            assert response.json()["data"]["remote"] == path
    assert [call[0] for call in calls] == [path for path, _ in paths]
    with sqlite3.connect(tmp_path / "angel.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_proxy_preserves_broker_status_and_payload(monkeypatch, tmp_path):
    config = config_file(tmp_path, market="angelone")
    monkeypatch.setenv("SMARTAPI_CONFIG_FILE", str(config))
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "proxy.db")
    app_module.init_db()
    calls = []

    class Client:
        root = "https://broker.example"
        access_token = "token"
        disable_ssl = False
        timeout = 7
        proxies = {}
        _routes = {"api.ltp.data": "/ltp"}

        def requestHeaders(self):
            return {"X-PrivateKey": "key"}

    class BrokerResponse:
        status_code = 429
        content = b'{"status":false,"message":"Rate limit","errorcode":"AB429","data":null}'
        headers = {"content-type": "application/json"}

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return BrokerResponse()

    proxy = angelone_proxy.AngelOneProxy()
    proxy.client = Client()
    proxy.credentials_path = angelone_proxy.SETTINGS["credentials_file"]
    payload = {"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045"}
    monkeypatch.setattr(angelone_proxy.requests, "request", request)
    reply = proxy.forward("/rest/secure/angelbroking/order/v1/getLtpData", payload)
    assert reply.status_code == 429 and reply.content == BrokerResponse.content
    assert calls[0][0:2] == ("POST", "https://broker.example/ltp")
    assert calls[0][2]["data"] == '{"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045"}'
