"""Real HTTP fixtures; the official SDK and its transport are never patched."""
import socket
import sqlite3
import threading
import time

import httpx
import pandas as pd
import pytest
from SmartApi import SmartConnect
import uvicorn

import app as app_module
import market
from portfolio import api_order_status


LOGIN_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
DUMMY_LOGIN = {"clientcode": "DUMMY001", "password": "password", "totp": "123456"}


def pytest_configure(config):
    config.addinivalue_line("markers", "smoke: fast offline checks run with `pytest -m smoke`")


def sign_in(client):
    """Log DUMMY001 in over REST and return its token data."""
    response = client.post(LOGIN_PATH, headers={"X-PrivateKey": "DUMMY_API_KEY"}, json=DUMMY_LOGIN)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def auth_headers(client):
    return {"Authorization": f"Bearer {sign_in(client)['jwtToken']}"}


class Ticker:
    """Deterministic Yahoo transport, retaining the real provider/conversion code."""
    fail = False
    symbols = []

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, **kwargs):
        if self.fail:
            raise ConnectionError("Yahoo unavailable")
        self.symbols.append(self.symbol)
        return pd.DataFrame(
            {"Open": [98., 99.], "High": [101., 102.], "Low": [97., 98.],
             "Close": [99., 100.], "Volume": [200, 300]},
            index=pd.date_range("2026-09-08 10:00", periods=2, freq="min", tz=market.IST),
        )


class SDKServer:
    def __init__(self, db):
        self.db = db
        self.server = self.thread = None
        self.port = 0

    def start(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        self.port = listener.getsockname()[1]
        self.root = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(
            app_module.app, host="127.0.0.1", port=self.port, log_level="error",
        ))
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [listener]}, daemon=True,
        )
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert self.server.started, "Uvicorn did not start"

    def stop(self):
        if self.server:
            self.server.should_exit = True
            self.thread.join(10)
            assert not self.thread.is_alive(), "Uvicorn failed to stop"

    def restart(self):
        self.stop()
        self.start()

    def hijack(self, price, **values):
        response = self.http.post("/admin/market", data={
            "exchange": "NSE", "symboltoken": "3045", "mode": "HIJACK",
            "ltp": price, "volume": 1234, **values,
        })
        assert response.status_code == 303

    def wait_order(self, order_id, status="FILLED", timeout=4):
        expected = api_order_status(status)
        deadline = time.monotonic() + timeout
        row = None
        while time.monotonic() < deadline:
            result = self.sdk.orderBook()
            assert result["status"], result
            row = next((r for r in result["data"] if r["orderid"] == order_id), None)
            if row and str(row["status"]).lower() == expected:
                return row
            time.sleep(.05)
        pytest.fail(f"Order {order_id} did not become {status}: {row}")


@pytest.fixture(autouse=True)
def disable_startup_banner(monkeypatch):
    monkeypatch.setenv("SMARTAPI_STARTUP_BANNER", "0")


@pytest.fixture
def sdk_server(tmp_path, monkeypatch):
    db = tmp_path / "sdk.db"
    monkeypatch.setattr(app_module, "DB_PATH", db)
    monkeypatch.setattr(market, "PROVIDER", market.YahooProvider(Ticker))
    monkeypatch.setattr(Ticker, "fail", False)
    monkeypatch.setattr(Ticker, "symbols", [])
    monkeypatch.chdir(tmp_path)  # SDK writes logs to its own working directory.
    for key in ("SMARTAPI_FORCE_TOKEN_EXPIRY", "SMARTAPI_ACCESS_TOKEN_TTL_SECONDS",
                "SMARTAPI_REFRESH_TOKEN_TTL_SECONDS", "SMARTAPI_MARKET_FILL_DELAY_MS",
                "SMARTAPI_ORDER_CHECK_INTERVAL_MS"):
        monkeypatch.delenv(key, raising=False)
    market.CACHE.clear()
    server = SDKServer(db)
    try:
        server.start()
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE rate_limit_config SET enabled=0")
            conn.execute("UPDATE accounts SET available_balance=100000")
        server.http = httpx.Client(base_url=server.root, timeout=5)
        server.sdk = SmartConnect(api_key="DUMMY_API_KEY", root=server.root)
        session = server.sdk.generateSession("DUMMY001", "password", "123456")
        assert session["status"], session
        server.session = session
        yield server
    finally:
        if hasattr(server, "http"):
            server.http.close()
        server.stop()
        market.CACHE.clear()
