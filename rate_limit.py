"""Small in-process, SQLite-configured SmartAPI rate limiter."""
from collections import deque
import json
import re
import threading
import time

from common import connect, fail


CONFIG_CACHE = None
CONFIG_LOCK = threading.Lock()
WINDOWS = (("per_second", 1), ("per_minute", 60), ("per_hour", 3600))
RATE_LIMIT_MESSAGE = "Access denied because of exceeding access rate"

ROUTE_GROUPS = {
    "orders": {
        "/rest/secure/angelbroking/order/v1/placeOrder",
        "/rest/secure/angelbroking/order/v1/modifyOrder",
        "/rest/secure/angelbroking/order/v1/cancelOrder",
    },
    "market": {
        "/rest/secure/angelbroking/order/v1/getLtpData",
        "/rest/secure/angelbroking/market/v1/quote",
        "/rest/secure/angelbroking/historical/v1/getCandleData",
    },
}

DEFAULT_LIMITS = [
    ("endpoint", "/rest/auth/angelbroking/user/v1/loginByPassword", 1, 0, 0),
    ("endpoint", "/rest/auth/angelbroking/jwt/v1/generateTokens", 1, 0, 1000),
    ("endpoint", "/rest/secure/angelbroking/user/v1/getProfile", 3, 0, 1000),
    ("endpoint", "/rest/secure/angelbroking/user/v1/logout", 1, 0, 0),
    ("endpoint", "/rest/secure/angelbroking/user/v1/getRMS", 2, 0, 0),
    ("group", "orders", 9, 500, 1000),
    ("endpoint", "/rest/secure/angelbroking/order/v1/getOrderBook", 1, 0, 0),
    ("endpoint", "/rest/secure/angelbroking/order/v1/getLtpData", 10, 500, 5000),
    ("endpoint", "/rest/secure/angelbroking/order/v1/getPosition", 1, 0, 0),
    ("endpoint", "/rest/secure/angelbroking/order/v1/getTradeBook", 1, 0, 0),
    ("endpoint", "/rest/secure/angelbroking/order/v1/convertPosition", 10, 500, 5000),
    ("endpoint", "/rest/secure/angelbroking/order/v1/searchScrip", 1, 0, 0),
    ("endpoint", "/rest/secure/angelbroking/order/v1/details/{order_id}", 10, 500, 5000),
    ("endpoint", "/rest/secure/angelbroking/portfolio/v1/getHolding", 1, 0, 0),
    ("endpoint", "/rest/secure/angelbroking/portfolio/v1/getAllHolding", 1, 0, 0),
    ("endpoint", "/gtt-service/rest/secure/angelbroking/gtt/v1/createRule", 9, 500, 5000),
    ("endpoint", "/gtt-service/rest/secure/angelbroking/gtt/v1/modifyRule", 9, 500, 5000),
    ("endpoint", "/gtt-service/rest/secure/angelbroking/gtt/v1/cancelRule", 9, 500, 5000),
    ("endpoint", "/rest/secure/angelbroking/gtt/v1/ruleDetails", 10, 500, 5000),
    ("endpoint", "/rest/secure/angelbroking/gtt/v1/ruleList", 10, 500, 5000),
    ("endpoint", "/rest/secure/angelbroking/historical/v1/getCandleData", 3, 180, 5000),
    ("endpoint", "/rest/secure/angelbroking/marketData/v1/optionGreek", 1, 0, 0),
]


def init_rate_limits():
    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rate_limit_config ("
            "scope TEXT NOT NULL, target TEXT NOT NULL, "
            "per_second INTEGER NOT NULL DEFAULT 0, "
            "per_minute INTEGER NOT NULL DEFAULT 0, "
            "per_hour INTEGER NOT NULL DEFAULT 0, "
            "enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(scope, target))"
        )
        conn.executemany(
            "INSERT OR IGNORE INTO rate_limit_config "
            "(scope, target, per_second, per_minute, per_hour) VALUES (?, ?, ?, ?, ?)",
            DEFAULT_LIMITS,
        )
    limiter.clear()


def list_limits():
    global CONFIG_CACHE
    with CONFIG_LOCK:
        if CONFIG_CACHE is None:
            with connect() as conn:
                CONFIG_CACHE = [
                    dict(row) for row in conn.execute(
                        "SELECT * FROM rate_limit_config ORDER BY scope, target"
                    )
                ]
        return [dict(row) for row in CONFIG_CACHE]


def save_limit(scope, target, per_second, per_minute, per_hour, enabled=True):
    if scope not in {"endpoint", "group"}:
        raise ValueError
    target = target.strip()
    if scope == "endpoint" and not target.startswith("/"):
        raise ValueError
    if scope == "group" and target not in ROUTE_GROUPS:
        raise ValueError
    values = [int(value) for value in (per_second, per_minute, per_hour)]
    if any(value < 0 for value in values):
        raise ValueError
    with connect() as conn:
        conn.execute(
            "INSERT INTO rate_limit_config "
            "(scope, target, per_second, per_minute, per_hour, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, target) DO UPDATE SET per_second=excluded.per_second, "
            "per_minute=excluded.per_minute, per_hour=excluded.per_hour, "
            "enabled=excluded.enabled",
            (scope, target, *values, int(bool(enabled))),
        )
    limiter.clear()


def _matches(path, target):
    pattern = re.sub(r"\{[^/]+\}", r"[^/]+", target.rstrip("/"))
    return re.fullmatch(pattern, path.rstrip("/")) is not None


class RateLimiter:
    def __init__(self):
        self.events = {}
        self.lock = threading.Lock()

    def clear(self):
        global CONFIG_CACHE
        with self.lock:
            self.events.clear()
        with CONFIG_LOCK:
            CONFIG_CACHE = None

    def _configs(self, path):
        return [
            row for row in list_limits()
            if row["enabled"] and (
                (row["scope"] == "endpoint" and _matches(path, row["target"]))
                or (
                    row["scope"] == "group"
                    and path.rstrip("/") in {item.rstrip("/") for item in ROUTE_GROUPS.get(row["target"], set())}
                )
            )
        ]

    def check(self, path, client_code):
        configs = self._configs(path)
        if not configs:
            return None
        now = time.monotonic()
        with self.lock:
            for row in configs:
                for field, seconds in WINDOWS:
                    limit = row[field]
                    if not limit:
                        continue
                    key = (row["scope"], row["target"], client_code, field)
                    events = self.events.setdefault(key, deque())
                    while events and events[0] <= now - seconds:
                        events.popleft()
                    if len(events) >= limit:
                        return fail(RATE_LIMIT_MESSAGE, "AB1004", 403)
            for row in configs:
                for field, _ in WINDOWS:
                    if row[field]:
                        self.events.setdefault(
                            (row["scope"], row["target"], client_code, field), deque()
                        ).append(now)
        return None


limiter = RateLimiter()


async def client_code_for(request):
    body = await request.body()
    values = {}
    try:
        values = json.loads(body.decode()) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    tokens = [
        request.headers.get("Authorization", "").removeprefix("Bearer "),
        values.get("refreshToken", "") if isinstance(values, dict) else "",
    ]
    with connect() as conn:
        for token in tokens:
            if isinstance(token, str) and token:
                row = conn.execute(
                    "SELECT client_code FROM sessions WHERE access_token = ? OR refresh_token = ?",
                    (token, token),
                ).fetchone()
                if row:
                    return row["client_code"]
        api_key = request.headers.get("X-PrivateKey", "")
        row = conn.execute(
            "SELECT client_code FROM users WHERE api_key = ?", (api_key,)
        ).fetchone()
    if row:
        return row["client_code"]
    if isinstance(values, dict):
        code = values.get("clientcode", values.get("clientCode", ""))
        return code if isinstance(code, str) and code else "anonymous"
    return "anonymous"
