"""Persistence across two independent server processes, using only SDK root."""
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
from SmartApi import SmartConnect


@contextmanager
def process_server(db, port, tmp_path):
    runner = Path(__file__).with_name("sdk_server_process.py")
    env = {key: value for key, value in os.environ.items() if not key.startswith("SMARTAPI_")}
    with (tmp_path / "server.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(runner), str(db), str(port)], cwd=tmp_path, env=env,
            stdin=subprocess.PIPE, stdout=log, stderr=log, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        root = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert process.poll() is None, (tmp_path / "server.log").read_text()
                try:
                    if httpx.get(root + "/health", timeout=.5).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
            else:
                raise AssertionError("Server process did not start")
            yield root
        finally:
            if process.poll() is None:
                try:
                    process.communicate("stop\n", timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)
                    raise AssertionError("Server process did not stop gracefully")


def test_actual_process_restart_preserves_trading_and_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "restart.db"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with process_server(db, port, tmp_path) as root:
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE rate_limit_config SET enabled=0")
        with httpx.Client(base_url=root) as admin:
            assert admin.post("/admin/account/DUMMY001/funds", data={"action": "add", "amount": 1000}).status_code == 303
            assert admin.post("/admin/market", data={"exchange": "NSE", "symboltoken": "3045", "mode": "HIJACK", "ltp": 100, "volume": 500}).status_code == 303
        sdk = SmartConnect(api_key="DUMMY_API_KEY", root=root)
        assert sdk.generateSession("DUMMY001", "password", "123456")["status"]
        params = {"variety": "NORMAL", "exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045",
                  "transactiontype": "BUY", "ordertype": "MARKET", "producttype": "DELIVERY",
                  "duration": "DAY", "price": 0, "quantity": 2}
        filled = sdk.placeOrder(params)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            book = sdk.orderBook()["data"]
            if book and book[0]["status"] == "complete":
                break
            time.sleep(.05)
        assert book[0]["status"] == "complete"
        waiting = sdk.placeOrder({**params, "ordertype": "LIMIT", "price": 90, "quantity": 1})
        before = {name: getattr(sdk, name)()["data"] for name in
                  ("orderBook", "tradeBook", "position", "holding", "allholding", "rmsLimit")}
        assert before["rmsLimit"]["availablecash"] == 710
        access, refresh = sdk.access_token, sdk.refresh_token
    with process_server(db, port, tmp_path) as root:
        # Persisted tokens remain valid; no re-login or SDK transport changes.
        restored = SmartConnect(api_key="DUMMY_API_KEY", root=root)
        restored.setAccessToken(access)
        restored.setRefreshToken(refresh)
        assert restored.getProfile(refresh)["data"]["clientcode"] == "DUMMY001"
        for name, expected in before.items():
            assert getattr(restored, name)()["data"] == expected, name
        assert restored.ltpData("NSE", "SBIN-EQ", "3045")["data"]["ltp"] == 100
        with httpx.Client(base_url=root) as admin:
            assert admin.get("/admin/logs").status_code == 200
            assert admin.post("/admin/market", data={"exchange": "NSE", "symboltoken": "3045", "mode": "HIJACK", "ltp": 80}).status_code == 303
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            book = restored.orderBook()["data"]
            row = next(row for row in book if row["orderid"] == waiting)
            if row["status"] == "complete":
                break
            time.sleep(.05)
        assert row["status"] == "complete" and row["averageprice"] == 90
        assert len(restored.tradeBook()["data"]) == 2
        assert restored.holding()["data"][0]["quantity"] == 3
        assert restored.rmsLimit()["data"]["availablecash"] == 710
        assert {row["orderid"] for row in book} == {filled, waiting}
