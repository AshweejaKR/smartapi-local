"""Angel One broker sessions and forwarding for selected SmartAPI routes.

Broker API keys, passwords, TOTP seeds and tokens stay in process memory. They
are never written to SQLite, the audit log, logs, admin pages or error text.
"""
import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import struct
import threading
import time

import requests

from common import connect, record_audit
import server_config


BROKER_ROOT = "https://apiconnect.angelone.in"
LOGIN_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
REFRESH_PATH = "/rest/auth/angelbroking/jwt/v1/generateTokens"
LOGOUT_PATH = "/rest/secure/angelbroking/user/v1/logout"
ORDER_DETAILS_PATH = "/rest/secure/angelbroking/order/v1/details/"
EXPIRED_CODES = {"AG8001", "AG8002", "AG8003"}
LOGGER = logging.getLogger("smartapi.angelone")

ROUTES = {
    "/rest/secure/angelbroking/order/v1/getLtpData": "POST",
    "/rest/secure/angelbroking/market/v1/quote": "POST",
    "/rest/secure/angelbroking/historical/v1/getCandleData": "POST",
    "/rest/secure/angelbroking/historical/v1/getOIData": "POST",
    "/rest/secure/angelbroking/order/v1/placeOrder": "POST",
    "/rest/secure/angelbroking/order/v1/modifyOrder": "POST",
    "/rest/secure/angelbroking/order/v1/cancelOrder": "POST",
    "/rest/secure/angelbroking/order/v1/getOrderBook": "GET",
    "/rest/secure/angelbroking/order/v1/getTradeBook": "GET",
    "/rest/secure/angelbroking/order/v1/getPosition": "GET",
    "/rest/secure/angelbroking/portfolio/v1/getHolding": "GET",
    "/rest/secure/angelbroking/portfolio/v1/getAllHolding": "GET",
    "/rest/secure/angelbroking/order/v1/convertPosition": "POST",
    "/rest/secure/angelbroking/user/v1/getRMS": "GET",
    "/rest/secure/angelbroking/margin/v1/batch": "POST",
}


class AngelOneError(RuntimeError):
    """Broker unavailable; the message is safe to show."""


class AngelOneLoginError(AngelOneError):
    """The broker rejected, or could not complete, a login."""

    def __init__(self, reason, errorcode="", message=""):
        super().__init__(reason)
        self.errorcode = errorcode
        self.broker_message = message


class AngelOneSessionExpired(AngelOneError):
    """The broker session expired and could not be refreshed."""


@dataclass
class AngelOneReply:
    status_code: int
    content: bytes
    content_type: str = "application/json"

    def json(self):
        try:
            return json.loads(self.content)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None


def send(method, path, body=None, headers=None):
    """The only function that talks to Angel One; tests replace it."""
    try:
        response = requests.request(
            method, BROKER_ROOT + path,
            data=json.dumps(body) if body is not None else None,
            headers=headers, allow_redirects=False, timeout=30,
        )
    except requests.RequestException as exc:
        # Exception text can echo request details; keep only the class name.
        raise AngelOneError(f"Angel One network error ({type(exc).__name__})") from None
    return AngelOneReply(
        response.status_code, response.content,
        response.headers.get("content-type", "application/json"),
    )


def headers_for(api_key, jwt=None):
    headers = {
        "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1", "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": api_key,
    }
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    return headers


def env_file(path):
    if not path.exists():
        raise AngelOneError("Angel One credentials file is not available")
    result = {}
    for row in path.read_text(encoding="utf-8").splitlines():
        row = row.strip()
        if row and not row.startswith("#") and "=" in row:
            key, value = row.split("=", 1)
            result[key.strip().upper()] = value.strip().strip("\"'")
    return result


def _value(values, *names):
    for name in names:
        if values.get(name):
            return values[name]
    raise AngelOneError("Angel One credentials file is incomplete")


def totp(secret):
    secret = secret.upper().replace(" ", "")
    if secret.isdigit() and len(secret) == 6:
        return secret
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(time.time() // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}"


def _expired(reply):
    data = reply.json()
    code = data.get("errorcode") if isinstance(data, dict) else ""
    return reply.status_code in {401, 403} or code in EXPIRED_CODES


class BrokerSession:
    """One logged-in Angel One session. Tokens live only in this object."""

    def __init__(self, api_key, client_code, tokens):
        self._api_key = api_key
        self.client_code = client_code
        self._jwt = tokens.get("jwtToken")
        self._refresh = tokens.get("refreshToken")
        self.lock = threading.Lock()

    def __repr__(self):
        return f"BrokerSession(client_code={self.client_code!r})"

    def refresh(self):
        reply = send("POST", REFRESH_PATH, {"refreshToken": self._refresh},
                     headers_for(self._api_key, self._jwt))
        data = reply.json()
        tokens = (data.get("data") or {}) if isinstance(data, dict) else {}
        if not (isinstance(data, dict) and data.get("status") and tokens.get("jwtToken")):
            return False
        self._jwt = tokens["jwtToken"]
        self._refresh = tokens.get("refreshToken") or self._refresh
        return True

    def forward(self, path, data=None, order_id=None):
        if order_id is not None:
            method, path, data = "GET", ORDER_DETAILS_PATH + str(order_id), None
        else:
            try:
                method = ROUTES[path]
            except KeyError:
                raise AngelOneError("Unsupported Angel One proxy route") from None
        body = dict(data or {}) if method == "POST" else None
        reply = send(method, path, body, headers_for(self._api_key, self._jwt))
        if _expired(reply):
            with self.lock:
                if not self.refresh():
                    raise AngelOneSessionExpired("Angel One session expired")
            reply = send(method, path, body, headers_for(self._api_key, self._jwt))
            if _expired(reply):
                raise AngelOneSessionExpired("Angel One session expired")
        return reply

    def logout(self):
        try:
            send("POST", LOGOUT_PATH, {"clientcode": self.client_code},
                 headers_for(self._api_key, self._jwt))
        except AngelOneError:
            pass


def broker_login(api_key, client_code, password, totp_code):
    """Validate credentials with Angel One; returns a BrokerSession."""
    reply = send("POST", LOGIN_PATH,
                 {"clientcode": client_code, "password": password, "totp": totp_code},
                 headers_for(api_key))
    data = reply.json()
    tokens = (data.get("data") or {}) if isinstance(data, dict) else {}
    if not (isinstance(data, dict) and data.get("status") and tokens.get("jwtToken")):
        errorcode = str((data or {}).get("errorcode") or "") if isinstance(data, dict) else ""
        message = str((data or {}).get("message") or "") if isinstance(data, dict) else ""
        reason = f"Angel One login failed ({errorcode or 'HTTP ' + str(reply.status_code)})"
        raise AngelOneLoginError(reason, errorcode, message[:120])
    return BrokerSession(api_key, client_code, tokens)


def record(action, detail):
    try:
        with connect() as conn:
            record_audit(conn, action, detail)
    except Exception:
        LOGGER.warning("could not record %s", action)


class InternalBroker:
    """Server-side Angel One session from credentials_file, for dummy clients.

    One login attempt per process: a failure is remembered so the broker is never
    retried with the same credentials (repeated bad logins can lock an account).
    """

    def __init__(self):
        self.session = None
        self.failure = ""
        self.lock = threading.Lock()

    def reset(self):
        with self.lock:
            self.session, self.failure = None, ""

    def _login(self):
        try:
            values = env_file(server_config.credentials_path())
            session = broker_login(
                _value(values, "ANGELONE_API_KEY", "API_KEY"),
                _value(values, "ANGELONE_CLIENT_CODE", "CLIENT_CODE", "CLIENT_ID", "CLIENTCODE"),
                _value(values, "ANGELONE_PASSWORD", "PASSWORD", "MPIN"),
                totp(_value(values, "ANGELONE_TOTP_SECRET", "TOTP_SECRET", "TOTP")),
            )
        except AngelOneError as exc:
            self.failure = str(exc)
        except Exception as exc:  # e.g. a malformed TOTP seed; never echo its text
            self.failure = f"Angel One login failed ({type(exc).__name__})"
        else:
            self.session = session
            record("broker.login", "Internal Angel One login succeeded")
            return session
        record("broker.login_failed", self.failure)
        if server_config.set_market_fallback(self.failure):
            record("source.fallback", f"market_data_source angelone -> yahoo: {self.failure}")
        raise AngelOneLoginError(self.failure)

    def current(self):
        with self.lock:
            if self.session is not None:
                return self.session
            if self.failure:
                raise AngelOneLoginError(self.failure)
            return self._login()

    def forward(self, path, data=None, order_id=None):
        try:
            return self.current().forward(path, data, order_id)
        except AngelOneSessionExpired:
            with self.lock:
                self.session = None
            return self.current().forward(path, data, order_id)


PROXY = InternalBroker()
SESSIONS = {}  # local session id -> BrokerSession, for client_auth: real
SESSIONS_LOCK = threading.Lock()


def reset():
    """Forget every broker session; called at server startup."""
    PROXY.reset()
    with SESSIONS_LOCK:
        SESSIONS.clear()


def register_session(session_id, broker):
    with SESSIONS_LOCK:
        SESSIONS[session_id] = broker


def drop_session(session_id, logout=False):
    with SESSIONS_LOCK:
        broker = SESSIONS.pop(session_id, None)
    if broker and logout:
        broker.logout()


def session_for(session_id):
    with SESSIONS_LOCK:
        return SESSIONS.get(session_id)


def broker_for_client(client_code):
    """Broker used outside a request (order checker, portfolio prices)."""
    if server_config.client_auth() == "dummy":
        return PROXY
    with SESSIONS_LOCK:
        broker = next((item for item in reversed(SESSIONS.values())
                       if item.client_code == client_code), None)
    if broker is None:
        raise AngelOneError("No Angel One session for this client")
    return broker
