"""Load SmartAPI Local data-source settings."""
from pathlib import Path
import os

import yaml


DEFAULTS = {
    "market_data_source": "yahoo",
    "order_data": None,
    "account_data": None,
    "credentials_file": "angelone_keys.env",
}
SETTINGS = DEFAULTS.copy()


def _source(value):
    value = "" if value is None else str(value).strip().lower()
    if value in {"", "none", "null", "dummy"}:
        return None
    if value not in {"angelone", "yahoo"}:
        raise ValueError("Data source must be angelone, yahoo, or None")
    return value


def init_config(base_dir):
    """Read the selected YAML file once per server startup."""
    path = Path(os.getenv("SMARTAPI_CONFIG_FILE", Path(base_dir) / "default.yaml"))
    path = path.expanduser().resolve()
    values = DEFAULTS.copy()
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("SmartAPI config must be a YAML mapping")
        values.update({key: loaded[key] for key in DEFAULTS if key in loaded})
    for name in ("market_data_source", "order_data", "account_data"):
        values[name] = _source(values[name])
    if values["order_data"] == "yahoo" or values["account_data"] == "yahoo":
        raise ValueError("order_data and account_data must be angelone or None")
    credentials = Path(str(values["credentials_file"])).expanduser()
    values["credentials_file"] = credentials if credentials.is_absolute() else path.parent / credentials
    SETTINGS.clear()
    SETTINGS.update(values)


def source(name):
    return SETTINGS[name]


def is_angel(name):
    return source(name) == "angelone"


def transparent_angel_proxy_enabled():
    """True only when every data source is the real broker."""
    return all(is_angel(name) for name in (
        "market_data_source", "order_data", "account_data",
    ))
