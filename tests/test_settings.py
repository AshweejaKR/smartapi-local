"""Admin settings: validation, atomic save to the active config file, apply on restart."""
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
import yaml

import app as app_module
import server_config


pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[1]
CONFIG_B = {"market_data_source": "angelone", "order_data": "null", "account_data": "angelone",
            "client_auth": "real", "credentials_file": "other_keys.env"}


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "custom.yaml"
    path.write_text("market_data_source: yahoo\norder_data: None\naccount_data: None\n", encoding="utf-8")
    monkeypatch.setenv("SMARTAPI_CONFIG_FILE", str(path))
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "settings.db")
    return path


def test_defaults_and_repository_config():
    assert server_config.validate({}) == {
        "market_data_source": "yahoo", "order_data": None, "account_data": None,
        "client_auth": "dummy", "credentials_file": "angelone_keys.env"}
    shipped = yaml.safe_load((ROOT / "default.yaml").read_text(encoding="utf-8"))
    assert server_config.validate(shipped)["client_auth"] == "dummy"
    for value in ("None", "null", None, ""):
        assert server_config.validate({"order_data": value})["order_data"] is None


@pytest.mark.parametrize("field, value", [
    ("market_data_source", "bogus"), ("order_data", "yahoo"), ("account_data", "yahoo"),
    ("client_auth", "maybe"), ("credentials_file", ""),
])
def test_invalid_choices_are_rejected_and_nothing_is_written(config, field, value):
    before = config.read_bytes()
    with TestClient(app_module.app) as client:
        response = client.post("/admin/settings", data={**CONFIG_B, field: value})
    assert response.status_code == 400 and field in response.text
    assert config.read_bytes() == before


def test_settings_save_to_active_file_and_apply_after_restart(config):
    shipped = (ROOT / "default.yaml").read_bytes()
    with TestClient(app_module.app) as client:
        assert server_config.client_auth() == "dummy"
        response = client.post("/admin/settings", data=CONFIG_B, follow_redirects=False)
        assert response.status_code == 303 and "Restart" in response.headers["location"]
        assert server_config.SETTINGS["client_auth"] == "dummy"  # running process unchanged
        assert "Saved settings differ" in client.get("/admin/settings").text
    saved = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert saved == {**CONFIG_B, "order_data": None}
    assert list(config.parent.glob(".smartapi-*")) == []  # atomic write left no temp file
    assert (ROOT / "default.yaml").read_bytes() == shipped
    with TestClient(app_module.app) as client:
        assert server_config.SETTINGS == {**CONFIG_B, "order_data": None}
        assert server_config.credentials_path() == config.parent / "other_keys.env"
        page = client.get("/admin/settings").text
        assert "Saved settings differ" not in page and "not found" in page
    with TestClient(app_module.app):
        assert server_config.SETTINGS["client_auth"] == "real"
