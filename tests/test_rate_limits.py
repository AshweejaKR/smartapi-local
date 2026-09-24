from fastapi.testclient import TestClient
import pytest

import app as app_module
from conftest import auth_headers


RMS_PATH = "/rest/secure/angelbroking/user/v1/getRMS"
ORDER_PATH = "/rest/secure/angelbroking/order/v1/placeOrder"
MODIFY_PATH = "/rest/secure/angelbroking/order/v1/modifyOrder"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "rate-limits.db")
    with TestClient(app_module.app) as test_client:
        yield test_client


def save_limit(client, scope, target, second=0, minute=0, hour=0, enabled="on"):
    return client.post(
        "/admin/rate-limits",
        data={
            "scope": scope,
            "target": target,
            "per_second": second,
            "per_minute": minute,
            "per_hour": hour,
            "enabled": enabled,
        },
        follow_redirects=False,
    )


@pytest.mark.smoke
def test_endpoint_limit_uses_smartapi_error_and_runtime_update(client):
    headers = auth_headers(client)
    assert save_limit(client, "endpoint", RMS_PATH, second=1).status_code == 303
    assert client.get(RMS_PATH, headers=headers).status_code == 200
    limited = client.get(RMS_PATH, headers=headers)
    assert limited.status_code == 403
    assert limited.json() == {
        "status": False,
        "message": "Access denied because of exceeding access rate",
        "errorcode": "AB1004",
        "data": None,
    }

    assert save_limit(client, "endpoint", RMS_PATH, second=2).status_code == 303
    assert client.get(RMS_PATH, headers=headers).status_code == 200


def test_minute_and_hour_limits_are_supported(client):
    headers = auth_headers(client)
    assert save_limit(client, "endpoint", RMS_PATH, minute=1, hour=1).status_code == 303
    assert client.get(RMS_PATH, headers=headers).status_code == 200
    assert client.get(RMS_PATH, headers=headers).status_code == 403


def test_route_group_limit_is_cumulative_across_order_routes(client):
    headers = auth_headers(client)
    assert save_limit(client, "group", "orders", second=1).status_code == 303
    assert client.post(ORDER_PATH, headers=headers, json={}).status_code == 400
    assert client.post(MODIFY_PATH, headers=headers, json={}).status_code == 403
