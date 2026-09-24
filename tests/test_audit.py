"""Request auditing and defensive authentication regressions."""
import json
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

import app as app_module
import auth
from conftest import DUMMY_LOGIN as CREDENTIALS, LOGIN_PATH as LOGIN, sign_in
from fault import clear_fault, start_fault
from rate_limit import save_limit


PROFILE = "/rest/secure/angelbroking/user/v1/getProfile"
REFRESH = "/rest/auth/angelbroking/jwt/v1/generateTokens"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "audit.db")
    with TestClient(app_module.app, raise_server_exceptions=False) as client:
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute("UPDATE rate_limit_config SET enabled=0")
        yield client


def entries():
    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM audit_log ORDER BY id")]


@pytest.mark.smoke
def test_requests_login_rate_limit_and_fault_are_audited_without_secrets(client):
    tokens = sign_in(client)
    headers = {"Authorization": "Bearer " + tokens["jwtToken"]}
    save_limit("endpoint", PROFILE, 1, 0, 0)
    assert client.get(PROFILE, headers=headers, params={"refreshToken": tokens["refreshToken"]}).status_code == 200
    assert client.get(PROFILE, headers=headers).status_code == 403
    save_limit("endpoint", PROFILE, 1, 0, 0, False)
    start_fault("503", 60)
    assert client.get(PROFILE, headers=headers).status_code == 503
    clear_fault()
    assert client.get(PROFILE, headers=headers).status_code == 200
    assert client.get("/rest/missing").status_code == 404
    bad_password = "audit-private-password"
    assert client.post(LOGIN, json={**CREDENTIALS, "password": bad_password}).status_code == 401

    rows = entries()
    requests = [row for row in rows if row["action"] == "rest_request"]
    assert [row["detail"].rsplit(" ", 1)[-1] for row in requests] == ["200", "200", "403", "503", "200", "404", "401"]
    assert all(row["client_code"] == "DUMMY001" for row in requests[:5])
    assert len([row for row in rows if row["action"] == "login_attempt"]) == 2
    assert any(row["action"] == "rate_limit" and "HTTP 403" in row["detail"] for row in rows)
    assert any(row["action"] == "fault_request" and "HTTP 503" in row["detail"] for row in rows)
    logged = json.dumps(rows)
    assert all(secret not in logged for secret in [bad_password, "DUMMY_API_KEY", *tokens.values()])


def test_audit_survives_handler_error_and_restart(client, monkeypatch):
    async def broken_profile(request):
        raise RuntimeError("private exception context")

    tokens = sign_in(client)
    headers = {"Authorization": "Bearer " + tokens["jwtToken"]}
    monkeypatch.setitem(app_module.CORE_HANDLERS, PROFILE, broken_profile)
    assert client.get(PROFILE, headers=headers).status_code == 500
    before = entries()
    assert before[-1]["detail"] == f"GET {PROFILE} HTTP 500"
    assert "private exception context" not in json.dumps(before)
    app_module.init_db()
    assert entries() == before


@pytest.mark.parametrize("body", [
    [], {"clientcode": []}, {"clientcode": {}},
    {**CREDENTIALS, "password": None}, {**CREDENTIALS, "password": {}},
    {**CREDENTIALS, "totp": "\u2603"},
])
def test_malformed_login_returns_auth_envelope(client, body):
    response = client.post(LOGIN, json=body)
    assert response.status_code == 401
    assert response.json()["errorcode"] == "AG8001"
    assert entries()[-1]["action"] == "login_attempt"


@pytest.mark.parametrize("refresh", [None, [], {}, 123, "invalid"])
def test_malformed_refresh_returns_auth_envelope(client, refresh):
    response = client.post(REFRESH, json={"refreshToken": refresh})
    assert response.status_code == 403
    assert response.json()["errorcode"] == "AG8001"


def test_tokens_expire_at_boundary_and_disabled_user_cannot_access(client, monkeypatch):
    tokens = sign_in(client)
    instant = int(time.time())
    monkeypatch.setattr(auth.time, "time", lambda: instant)
    headers = {"Authorization": "Bearer " + tokens["jwtToken"]}
    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.execute("UPDATE sessions SET access_expires_at=?, refresh_expires_at=?", (instant, instant))
    assert client.get(PROFILE, headers=headers).status_code == 403
    assert client.post(REFRESH, json={"refreshToken": tokens["refreshToken"]}).status_code == 403
    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.execute("UPDATE sessions SET access_expires_at=?, refresh_expires_at=?", (instant + 60, instant + 60))
        conn.execute("UPDATE users SET enabled=0 WHERE client_code='DUMMY001'")
    assert client.get(PROFILE, headers=headers).status_code == 403
    assert client.post(REFRESH, json={"refreshToken": tokens["refreshToken"]}).status_code == 403
