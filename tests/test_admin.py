import hashlib
import sqlite3
import pytest
from fastapi.testclient import TestClient
from SmartApi import SmartConnect

import app as app_module


LOGIN_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
PROFILE_PATH = "/rest/secure/angelbroking/user/v1/getProfile"
USER = {
    "client_code": "ADMIN002",
    "password": "new-password",
    "api_key": "NEW_API_KEY",
    "totp": "654321",
    "name": "Admin User",
    "email": "admin@example.test",
    "mobile": "9111111111",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "smartapi.db")
    with TestClient(app_module.app) as test_client:
        # Authentication checks are independent of limiter behavior.
        with sqlite3.connect(app_module.DB_PATH) as conn:
            conn.execute("UPDATE rate_limit_config SET enabled=0")
        yield test_client


def login(client, user=USER):
    return client.post(
        LOGIN_PATH,
        headers={"X-PrivateKey": user["api_key"]},
        json={
            "clientcode": user["client_code"],
            "password": user["password"],
            "totp": user["totp"],
        },
    )


def add_user(client):
    return client.post("/admin/users/add", data=USER, follow_redirects=False)


@pytest.mark.smoke
def test_admin_add_edit_disable_delete_changes_auth_immediately(client):
    assert client.get("/admin").status_code == 200
    assert add_user(client).status_code == 303

    logged_in = login(client)
    assert logged_in.status_code == 200
    assert logged_in.json()["status"] is True
    access_token = logged_in.json()["data"]["jwtToken"]

    edited = {
        **USER,
        "client_code": "EDITED002",
        "password": "edited-password",
        "api_key": "EDITED_API_KEY",
        "totp": "111222",
    }
    response = client.post(
        f"/admin/users/{USER['client_code']}/edit",
        data=edited,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert login(client).status_code == 401
    assert client.get(
        PROFILE_PATH, headers={"Authorization": f"Bearer {access_token}"}
    ).status_code == 403
    assert login(client, edited).json()["status"] is True

    assert client.post(
        "/admin/users/EDITED002/toggle", follow_redirects=False
    ).status_code == 303
    assert login(client, edited).status_code == 401
    client.post("/admin/users/EDITED002/toggle", follow_redirects=False)
    assert login(client, edited).json()["status"] is True

    assert client.post(
        "/admin/users/EDITED002/delete", follow_redirects=False
    ).status_code == 303
    assert login(client, edited).status_code == 401


def test_validation_and_password_hash_are_not_exposed(client):
    invalid = {**USER, "client_code": "?", "totp": "abc"}
    response = client.post("/admin/users/add", data=invalid)
    assert response.status_code == 400
    assert "Client code must be" in response.text
    assert "exactly 6 digits" in response.text

    add_user(client)
    listing = client.get("/admin/users").text
    edit_form = client.get(f"/admin/users/{USER['client_code']}/edit").text
    digest = hashlib.sha256(USER["password"].encode()).hexdigest()
    assert f'value="{USER["password"]}"' not in listing + edit_form
    assert digest not in listing + edit_form
    assert "password_hash" not in listing + edit_form


def test_funds_persist_and_cannot_be_overdrawn(tmp_path, monkeypatch):
    database = tmp_path / "persistent.db"
    monkeypatch.setattr(app_module, "DB_PATH", database)
    with TestClient(app_module.app) as client:
        client.post(
            "/admin/account/DUMMY001/funds",
            data={"action": "add", "amount": "1250.75"},
        )
        client.post(
            "/admin/account/DUMMY001/funds",
            data={"action": "remove", "amount": "250.25"},
        )
        overdraw = client.post(
            "/admin/account/DUMMY001/funds",
            data={"action": "remove", "amount": "1000.51"},
        )
        assert "Cannot remove more" in overdraw.text

    with TestClient(app_module.app) as restarted:
        assert "1000.50" in restarted.get("/admin/account").text
    with sqlite3.connect(database) as conn:
        balance = conn.execute(
            "SELECT available_balance FROM accounts WHERE client_code='DUMMY001'"
        ).fetchone()[0]
    assert balance == 1000.5


def test_admin_created_user_works_with_official_sdk(sdk_server):
    s = sdk_server
    assert s.http.post("/admin/users/add", data=USER).status_code == 303
    sdk = SmartConnect(api_key=USER["api_key"], root=s.root, timeout=5)
    session = sdk.generateSession(USER["client_code"], USER["password"], USER["totp"])
    assert session["status"] is True and session["data"]["jwtToken"]
    assert sdk.getProfile(session["data"]["refreshToken"])["data"]["clientcode"] == USER["client_code"]
