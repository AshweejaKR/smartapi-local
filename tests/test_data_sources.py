"""Source selection, client login modes and broker fallback; Angel One is mocked at send()."""
from contextlib import contextmanager
import json
import logging
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest
import yaml

import angelone_proxy
import app as app_module
from conftest import DUMMY_LOGIN, LOGIN_PATH, auth_headers
import market
import server_config


pytestmark = pytest.mark.smoke

LTP = "/rest/secure/angelbroking/order/v1/getLtpData"
PLACE = "/rest/secure/angelbroking/order/v1/placeOrder"
BOOK = "/rest/secure/angelbroking/order/v1/getOrderBook"
RMS = "/rest/secure/angelbroking/user/v1/getRMS"
PROFILE = "/rest/secure/angelbroking/user/v1/getProfile"
LOGOUT = "/rest/secure/angelbroking/user/v1/logout"
SBIN = {"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045"}
FILE_SECRETS = {"api_key": "FILE-API-KEY-7Q2", "password": "FILE-PASSWORD-9Z", "seed": "JBSWY3DPEHPK3PXP"}
REAL = {"api_key": "CLIENT-API-KEY-4R8", "client": "REAL001", "password": "CLIENT-PASS-5T", "totp": "654321"}
BROKER_TOKENS = {"jwtToken": "BROKER-JWT-SECRET-1", "refreshToken": "BROKER-REFRESH-SECRET",
                 "feedToken": "BROKER-FEED-SECRET"}


def reply(status, body):
    return angelone_proxy.AngelOneReply(status, json.dumps(body).encode())


class FakeBroker:
    """Angel One stand-in: accepts the credentials file account and REAL001."""

    def __init__(self, login_ok=True):
        self.login_ok = login_ok
        self.calls = []
        self.jwt = BROKER_TOKENS["jwtToken"]
        self.expire = 0          # next N authenticated calls answer "token expired"
        self.refresh_ok = True

    def paths(self, path):
        return [call for call in self.calls if call[1] == path]

    def __call__(self, method, path, body=None, headers=None):
        headers = dict(headers or {})
        self.calls.append((method, path, body, headers))
        if path == angelone_proxy.LOGIN_PATH:
            accounts = {("BROKERCLIENT1", FILE_SECRETS["password"], FILE_SECRETS["api_key"]),
                        (REAL["client"], REAL["password"], REAL["api_key"])}
            valid = (body["clientcode"], body["password"], headers["X-PrivateKey"]) in accounts
            if not (self.login_ok and valid and len(body["totp"]) == 6):
                return reply(200, {"status": False, "message": "Invalid totp", "errorcode": "AB1050", "data": None})
            return reply(200, {"status": True, "message": "SUCCESS", "errorcode": "", "data": BROKER_TOKENS})
        if path == angelone_proxy.REFRESH_PATH:
            if not self.refresh_ok:
                return reply(403, {"status": False, "message": "Invalid Token", "errorcode": "AG8001", "data": None})
            self.jwt = "BROKER-JWT-SECRET-2"
            return reply(200, {"status": True, "data": {**BROKER_TOKENS, "jwtToken": self.jwt}})
        if self.expire or headers.get("Authorization") != f"Bearer {self.jwt}":
            self.expire = max(0, self.expire - 1)
            return reply(401, {"status": False, "message": "Token expired", "errorcode": "AG8002", "data": None})
        if path == LTP:
            return reply(200, {"status": True, "message": "SUCCESS", "errorcode": "", "data": {
                **body, "open": 120, "high": 125, "low": 119, "close": 121, "ltp": 123.45}})
        return reply(200, {"status": True, "message": "SUCCESS", "errorcode": "", "data": {"remote": path}})


def write_config(tmp_path, market_source="angelone", order="angelone", account="angelone", client_auth="dummy"):
    keys = tmp_path / "keys.env"
    keys.write_text(f"API_KEY={FILE_SECRETS['api_key']}\nCLIENT_CODE=BROKERCLIENT1\n"
                    f"PASSWORD={FILE_SECRETS['password']}\nTOTP_SECRET={FILE_SECRETS['seed']}\n", encoding="utf-8")
    path = tmp_path / "source.yaml"
    path.write_text(yaml.safe_dump({
        "market_data_source": market_source, "order_data": order, "account_data": account,
        "client_auth": client_auth, "credentials_file": "keys.env",
    }), encoding="utf-8")
    return path


class FakeYahoo:
    def quote(self, symbol):
        return {"timestamp": market.datetime.now(market.IST), "open": 50.0, "high": 51.0,
                "low": 49.0, "close": 50.0, "ltp": 50.5, "volume": 10}


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Start the app with a config and fake broker; yields a starter function."""
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "sources.db")
    monkeypatch.setattr(market, "PROVIDER", FakeYahoo())
    monkeypatch.setenv("SMARTAPI_MARKET_FILL_DELAY_MS", "1")
    market.CACHE.clear()

    @contextmanager
    def start(broker, **config):
        monkeypatch.setenv("SMARTAPI_CONFIG_FILE", str(write_config(tmp_path, **config)))
        monkeypatch.setattr(angelone_proxy, "send", broker)
        with TestClient(app_module.app) as client:
            with sqlite3.connect(tmp_path / "sources.db") as conn:
                conn.execute("UPDATE rate_limit_config SET enabled=0")
            yield client

    yield start
    market.CACHE.clear()


def db_rows(tmp_path, sql):
    with sqlite3.connect(tmp_path / "sources.db") as conn:
        return conn.execute(sql).fetchall()


def real_login(client, api_key=REAL["api_key"], password=REAL["password"]):
    return client.post(LOGIN_PATH, headers={"X-PrivateKey": api_key}, json={
        "clientcode": REAL["client"], "password": password, "totp": REAL["totp"]})


def test_config_a_dummy_clients_use_the_internal_broker_session(server, tmp_path):
    broker = FakeBroker()
    with server(broker) as client:
        login = broker.paths(angelone_proxy.LOGIN_PATH)
        assert len(login) == 1  # internal login at startup, from the credentials file
        _, _, body, headers = login[0]
        assert body["clientcode"] == "BROKERCLIENT1" and headers["X-PrivateKey"] == FILE_SECRETS["api_key"]
        headers = auth_headers(client)  # DUMMY001 / password / DUMMY_API_KEY / 123456
        assert BROKER_TOKENS["jwtToken"] not in headers["Authorization"]
        assert client.post(LTP, headers=headers, json=SBIN).json()["data"]["ltp"] == 123.45
        assert client.post(PLACE, headers=headers, json={**SBIN, "quantity": "1"}).json()["data"] == {"remote": PLACE}
        assert client.get(RMS, headers=headers).json()["data"] == {"remote": RMS}
        assert client.get(PROFILE, headers=headers).json()["data"]["clientcode"] == "DUMMY001"
    forwarded = [call for call in broker.calls if call[1] in {LTP, PLACE, RMS}]
    assert [call[1] for call in forwarded] == [LTP, PLACE, RMS]
    assert all(call[3]["Authorization"] == f"Bearer {BROKER_TOKENS['jwtToken']}" for call in forwarded)
    assert len(broker.paths(angelone_proxy.LOGIN_PATH)) == 1
    assert db_rows(tmp_path, "SELECT COUNT(*) FROM orders")[0][0] == 0


def test_config_b_dummy_keeps_orders_local_with_broker_prices(server, tmp_path):
    broker = FakeBroker()
    with server(broker, order=None) as client:
        headers = auth_headers(client)
        client.post("/admin/account/DUMMY001/funds", data={"action": "add", "amount": "10000"})
        order = {**SBIN, "variety": "NORMAL", "transactiontype": "BUY", "ordertype": "MARKET",
                 "producttype": "DELIVERY", "duration": "DAY", "price": "0", "quantity": "2"}
        placed = client.post(PLACE, headers=headers, json=order).json()
        assert placed["status"], placed
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            book = client.get(BOOK, headers=headers).json()["data"]
            if book[0]["status"] == "complete":
                break
            time.sleep(.05)
        assert book[0]["status"] == "complete" and book[0]["averageprice"] == 123.45
        # Angel One market data covers instruments outside the Yahoo catalog too.
        tcs = {**order, "tradingsymbol": "TCS-EQ", "symboltoken": "11536", "ordertype": "LIMIT", "price": "1"}
        assert client.post(PLACE, headers=headers, json=tcs).json()["status"] is True
        unknown = client.post(PLACE, headers=headers, json={**tcs, "symboltoken": "424242"}).json()
        assert unknown["errorcode"] == "AB1018"
        assert client.get(RMS, headers=headers).json()["data"] == {"remote": RMS}
    assert not broker.paths(PLACE) and not broker.paths(BOOK)
    assert db_rows(tmp_path, "SELECT COUNT(*) FROM orders")[0][0] == 2


def test_failed_internal_login_falls_back_to_yahoo_for_market_data_only(server, tmp_path):
    broker = FakeBroker(login_ok=False)
    with server(broker) as client:
        headers = auth_headers(client)
        assert server_config.source("market_data_source") == "angelone"
        assert server_config.effective_source("market_data_source") == "yahoo"
        ltp = client.post(LTP, headers=headers, json=SBIN).json()
        assert ltp["status"] and ltp["data"]["ltp"] == 50.5  # Yahoo, via the verified catalog
        tcs = client.post(LTP, headers=headers, json={**SBIN, "tradingsymbol": "TCS-EQ", "symboltoken": "11536"})
        assert tcs.json()["errorcode"] == "AB1018"  # not a verified Yahoo mapping
        for response in (client.post(PLACE, headers=headers, json={**SBIN, "quantity": "1"}),
                         client.get(RMS, headers=headers)):
            assert response.status_code == 503
            assert response.json() == {"status": False, "message": "Angel One request is unavailable",
                                       "errorcode": "AB2001", "data": None}
        settings = client.get("/admin/settings").text
        assert "Market data fallback" in settings and "AB1050" in settings
    assert len(broker.paths(angelone_proxy.LOGIN_PATH)) == 1  # never retried with bad credentials
    assert db_rows(tmp_path, "SELECT COUNT(*) FROM orders")[0][0] == 0  # no silent local orders
    fallback = db_rows(tmp_path, "SELECT detail FROM audit_log WHERE action='source.fallback'")
    assert fallback == [("market_data_source angelone -> yahoo: Angel One login failed (AB1050)",)]
    saved = yaml.safe_load((tmp_path / "source.yaml").read_text(encoding="utf-8"))
    assert saved["market_data_source"] == "angelone"  # the user's selection is not rewritten


def test_real_mode_validates_with_angel_one_and_uses_that_session(server, tmp_path):
    broker = FakeBroker()
    with server(broker, order=None, client_auth="real") as client:
        assert broker.calls == []  # no internal login in real mode
        wrong = real_login(client, password="wrong")
        assert wrong.status_code == 401
        assert wrong.json() == {"status": False, "message": "Invalid totp", "errorcode": "AB1050", "data": None}
        dummy = client.post(LOGIN_PATH, headers={"X-PrivateKey": "DUMMY_API_KEY"}, json=DUMMY_LOGIN)
        assert dummy.json()["status"] is False  # dummy credentials are not real credentials
        tokens = real_login(client).json()["data"]
        assert set(tokens) == {"jwtToken", "refreshToken", "feedToken"}
        assert not set(tokens.values()) & set(BROKER_TOKENS.values())
        headers = {"Authorization": f"Bearer {tokens['jwtToken']}"}
        assert client.post(LTP, headers=headers, json=SBIN).json()["data"]["ltp"] == 123.45
        _, _, _, sent = broker.paths(LTP)[-1]
        assert sent["X-PrivateKey"] == REAL["api_key"]
        assert client.get(RMS, headers=headers).json()["data"] == {"remote": RMS}

        client.post(f"/admin/account/{REAL['client']}/funds", data={"action": "add", "amount": "1000"})
        order = {**SBIN, "variety": "NORMAL", "transactiontype": "BUY", "ordertype": "LIMIT",
                 "producttype": "DELIVERY", "duration": "DAY", "price": "1", "quantity": "1"}
        assert client.post(PLACE, headers=headers, json=order).json()["status"] is True
        assert not broker.paths(PLACE)  # order_data: null keeps orders local

        broker.expire = 1  # broker token expired once: refreshed and retried
        assert client.get(RMS, headers=headers).json()["data"] == {"remote": RMS}
        assert len(broker.paths(angelone_proxy.REFRESH_PATH)) == 1
        broker.expire, broker.refresh_ok = 10, False  # refresh fails: the local session ends
        assert client.get(RMS, headers=headers).status_code == 403
        assert client.get(PROFILE, headers=headers).status_code == 403

        again = {"Authorization": f"Bearer {real_login(client).json()['data']['jwtToken']}"}
        broker.expire = 0
        assert client.post(LOGOUT, headers=again, json={"clientcode": REAL["client"]}).json()["status"]
        assert broker.paths(angelone_proxy.LOGOUT_PATH)
        assert angelone_proxy.SESSIONS == {}
        user = db_rows(tmp_path, f"SELECT api_key, totp FROM users WHERE client_code='{REAL['client']}'")
        assert user == [("(angelone login)", "")]


def test_real_mode_sessions_end_with_the_process(server):
    broker = FakeBroker()
    with server(broker, order=None, client_auth="real") as client:
        token = real_login(client).json()["data"]["jwtToken"]
    with server(broker, order=None, client_auth="real") as client:
        assert client.get(PROFILE, headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_real_mode_reports_an_unreachable_broker(server):
    def down(*args, **kwargs):
        raise angelone_proxy.AngelOneError("Angel One network error (ConnectTimeout)")

    with server(down, client_auth="real") as client:
        response = real_login(client)
    assert response.status_code == 503 and response.json()["errorcode"] == "AB2001"


@pytest.mark.parametrize("mode, login_ok", [("dummy", True), ("dummy", False), ("real", True)])
def test_credentials_and_broker_tokens_never_leak(server, tmp_path, capsys, caplog, mode, login_ok):
    caplog.set_level(logging.DEBUG)
    broker = FakeBroker(login_ok=login_ok)
    seen = []
    with server(broker, order=None, client_auth=mode) as client:
        login = real_login(client) if mode == "real" else client.post(
            LOGIN_PATH, headers={"X-PrivateKey": "DUMMY_API_KEY"}, json=DUMMY_LOGIN)
        headers = {"Authorization": f"Bearer {login.json()['data']['jwtToken']}"}
        seen.append(login.text)
        for response in (client.post(LTP, headers=headers, json=SBIN), client.get(RMS, headers=headers),
                         client.get(PROFILE, headers=headers), real_login(client, password="bad")):
            seen.append(response.text)
        for page in ("", "/settings", "/instruments", "/audit", "/users", "/account", "/market"):
            seen.append(client.get("/admin" + page).text)
    with sqlite3.connect(tmp_path / "sources.db") as conn:
        for table in ("audit_log", "sessions", "users", "server_meta"):
            seen.append(repr(conn.execute(f"SELECT * FROM {table}").fetchall()))
    captured = capsys.readouterr()
    seen += [captured.out, captured.err, caplog.text]
    text = "\n".join(seen)
    secrets = [*FILE_SECRETS.values(), *BROKER_TOKENS.values(),
               "BROKER-JWT-SECRET-2", REAL["api_key"], REAL["password"]]
    assert [secret for secret in secrets if secret in text] == []
