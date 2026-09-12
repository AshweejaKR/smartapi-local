import socket
import threading
import time

import httpx
from SmartApi import SmartConnect
import uvicorn

import app as app_module


def test_sdk_rest_fault_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "faults.db")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = uvicorn.Server(
        uvicorn.Config(app_module.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.01)
    assert server.started
    root = f"http://127.0.0.1:{port}"
    try:
        sdk = SmartConnect(api_key="DUMMY_API_KEY", root=root, timeout=2)
        session = sdk.generateSession("DUMMY001", "password", "123456")
        refresh = session["data"]["refreshToken"]
        assert httpx.post(
            f"{root}/admin/faults", data={"mode": "unavailable", "duration": "3"}
        ).status_code == 303
        assert httpx.get(f"{root}/admin").status_code == 200
        assert httpx.get(f"{root}/health").status_code == 200
        failed = sdk.getProfile(refresh)
        assert failed["status"] is False
        time.sleep(3.15)
        recovered = sdk.getProfile(refresh)
        assert recovered["status"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)
