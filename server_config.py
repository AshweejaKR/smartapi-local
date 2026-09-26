"""Load, validate and save SmartAPI Local data-source settings."""
from pathlib import Path
import os
import tempfile
import threading

import yaml


DEFAULTS = {
    "market_data_source": "yahoo",
    "order_data": None,
    "account_data": None,
    "client_auth": "dummy",
    "credentials_file": "angelone_keys.env",
}
CHOICES = {
    "market_data_source": ("angelone", "yahoo", None),
    "order_data": ("angelone", None),
    "account_data": ("angelone", None),
    "client_auth": ("dummy", "real"),
}
SOURCES = ("market_data_source", "order_data", "account_data")
SETTINGS = DEFAULTS.copy()
CONFIG_PATH = None
# Process-only state: never written back to the YAML file.
RUNTIME = {"market_fallback": ""}
RUNTIME_LOCK = threading.Lock()


def _source(value):
    value = "" if value is None else str(value).strip().lower()
    return None if value in {"", "none", "null", "dummy"} else value


def validate(values):
    """Return normalised settings or raise ValueError naming the bad field."""
    result = DEFAULTS.copy()
    result.update({key: values[key] for key in DEFAULTS if key in values})
    for name in SOURCES:
        result[name] = _source(result[name])
    result["client_auth"] = str(result["client_auth"] or "dummy").strip().lower()
    for name, allowed in CHOICES.items():
        if result[name] not in allowed:
            names = ", ".join("null" if item is None else item for item in allowed)
            raise ValueError(f"{name} must be one of: {names}")
    credentials = str(result["credentials_file"] or "").strip()
    if not credentials or len(credentials) > 260 or any(ch in credentials for ch in "\r\n\0"):
        raise ValueError("credentials_file must be a file path")
    result["credentials_file"] = credentials
    return result


def config_path(base_dir):
    path = Path(os.getenv("SMARTAPI_CONFIG_FILE", Path(base_dir) / "default.yaml"))
    return path.expanduser().resolve()


def read_config(path):
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("SmartAPI config must be a YAML mapping")
    return validate(loaded)


def init_config(base_dir):
    """Read the selected YAML file once per server startup."""
    global CONFIG_PATH
    CONFIG_PATH = config_path(base_dir)
    values = validate(read_config(CONFIG_PATH))
    SETTINGS.clear()
    SETTINGS.update(values)
    with RUNTIME_LOCK:
        RUNTIME["market_fallback"] = ""


def saved_settings():
    """Settings currently in the active file; they apply after a restart."""
    return validate(read_config(CONFIG_PATH)) if CONFIG_PATH else dict(SETTINGS)


def save_config(values):
    """Validate and atomically replace the active config file."""
    values = validate(values)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "# SmartAPI Local settings. Restart the server to apply changes.\n"
        + yaml.safe_dump(values, sort_keys=False)
    )
    handle, temp = tempfile.mkstemp(prefix=".smartapi-", suffix=".yaml", dir=CONFIG_PATH.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, CONFIG_PATH)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return values


def credentials_path():
    path = Path(SETTINGS["credentials_file"]).expanduser()
    base = CONFIG_PATH.parent if CONFIG_PATH else Path.cwd()
    return path if path.is_absolute() else base / path


def source(name):
    """Desired source from the running configuration."""
    return SETTINGS[name]


def effective_source(name):
    """Source actually used by this process, after any market fallback."""
    if name == "market_data_source" and SETTINGS[name] == "angelone" and RUNTIME["market_fallback"]:
        return "yahoo"
    return SETTINGS[name]


def is_angel(name):
    return effective_source(name) == "angelone"


def client_auth():
    return SETTINGS["client_auth"]


def uses_internal_login():
    """Dummy clients reach the broker through the server's credentials file."""
    return client_auth() == "dummy" and any(SETTINGS[name] == "angelone" for name in SOURCES)


def set_market_fallback(reason):
    """Switch market data to Yahoo for this process; True only on the first switch."""
    with RUNTIME_LOCK:
        if RUNTIME["market_fallback"] or SETTINGS["market_data_source"] != "angelone":
            return False
        RUNTIME["market_fallback"] = reason
        return True


def market_fallback():
    return RUNTIME["market_fallback"]
