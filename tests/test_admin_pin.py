"""Browser Admin PIN only; SmartAPI client routes retain their own auth."""
import sqlite3

from fastapi.testclient import TestClient
import pytest

import admin_session
import app as app_module
from conftest import DUMMY_LOGIN, LOGIN_PATH


pytestmark = pytest.mark.smoke
PIN = "demo-secret-pin"


@pytest.fixture
def protected(tmp_path, monkeypatch):
    config = tmp_path / "protected.yaml"
    config.write_text("market_data_source: null\nadmin_pin_enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("SMARTAPI_CONFIG_FILE", str(config))
    monkeypatch.setenv("SMARTAPI_ADMIN_PIN", PIN)
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "pin.db")
    return config


def test_login_cookie_protects_pages_and_mutations_and_logout(protected):
    with TestClient(app_module.app) as client:
        for path in ("/admin", "/admin/users", "/admin/unknown"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303 and response.headers["location"] == "/admin/login"
        assert client.post("/admin/users/add", data={}, follow_redirects=False).status_code == 303
        assert client.post("/admin/logout", follow_redirects=False).headers["location"] == "/admin/login"
        assert "password" in client.get("/admin/login").text
        wrong = client.post("/admin/login", data={"pin": "wrong"}, follow_redirects=False)
        assert wrong.status_code == 401 and "set-cookie" not in wrong.headers
        assert PIN not in wrong.text
        response = client.post("/admin/login", data={"pin": PIN}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/admin"
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=43200" in cookie
        assert PIN not in cookie and client.get("/admin").status_code == 200
        assert client.get("/admin/users").status_code == 200
        assert client.post("/admin/logout", follow_redirects=False).status_code == 303
        assert client.get("/admin", follow_redirects=False).status_code == 303
        with sqlite3.connect(app_module.DB_PATH) as conn:
            assert PIN not in str(conn.execute("SELECT * FROM audit_log").fetchall())


def test_expiry_and_restart_invalidate_session(protected, monkeypatch):
    with TestClient(app_module.app) as client:
        client.post("/admin/login", data={"pin": PIN})
        token = client.cookies[admin_session.COOKIE]
        assert admin_session.valid(token)
        with admin_session.lock:
            admin_session.sessions[token] = 0
        assert client.get("/admin", follow_redirects=False).status_code == 303
        client.post("/admin/login", data={"pin": PIN})
        token = client.cookies[admin_session.COOKIE]
    with TestClient(app_module.app) as restarted:
        restarted.cookies.set(admin_session.COOKIE, token, path="/admin")
        assert restarted.get("/admin", follow_redirects=False).status_code == 303


def test_failed_attempts_are_limited_per_ip(protected):
    with TestClient(app_module.app) as client:
        for _ in range(5):
            assert client.post("/admin/login", data={"pin": "wrong"}).status_code == 401
        assert client.post("/admin/login", data={"pin": PIN}).status_code == 429
        assert client.get("/admin", follow_redirects=False).status_code == 303


def test_missing_pin_setup_error_leaves_sdk_and_health_running(protected, monkeypatch):
    monkeypatch.delenv("SMARTAPI_ADMIN_PIN")
    with TestClient(app_module.app) as client:
        assert client.get("/admin").status_code == 503
        assert client.get("/admin/login").status_code == 503
        assert client.get("/health").status_code == 200
        response = client.post(LOGIN_PATH, headers={"X-PrivateKey": "DUMMY_API_KEY"}, json=DUMMY_LOGIN)
        assert response.status_code == 200 and response.json()["status"]
        assert client.get("/local/v1/market/state").status_code == 403


def test_pin_can_be_explicitly_disabled(protected):
    protected.write_text("market_data_source: null\nadmin_pin_enabled: false\n", encoding="utf-8")
    with TestClient(app_module.app) as client:
        assert client.get("/admin").status_code == 200
