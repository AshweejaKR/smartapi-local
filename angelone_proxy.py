"""Small authenticated proxy for selected Angel One SmartAPI routes."""
import base64
import hashlib
import hmac
import struct
import time

from SmartApi import SmartConnect

from server_config import SETTINGS


ROUTES = {
    "/rest/secure/angelbroking/order/v1/getLtpData": ("api.ltp.data", "POST"),
    "/rest/secure/angelbroking/market/v1/quote": ("api.market.data", "POST"),
    "/rest/secure/angelbroking/historical/v1/getCandleData": ("api.candle.data", "POST"),
    "/rest/secure/angelbroking/historical/v1/getOIData": ("api.oi.data", "POST"),
    "/rest/secure/angelbroking/order/v1/placeOrder": ("api.order.placefullresponse", "POST"),
    "/rest/secure/angelbroking/order/v1/modifyOrder": ("api.order.modify", "POST"),
    "/rest/secure/angelbroking/order/v1/cancelOrder": ("api.order.cancel", "POST"),
    "/rest/secure/angelbroking/order/v1/getOrderBook": ("api.order.book", "GET"),
    "/rest/secure/angelbroking/order/v1/getTradeBook": ("api.trade.book", "GET"),
    "/rest/secure/angelbroking/order/v1/getPosition": ("api.position", "GET"),
    "/rest/secure/angelbroking/portfolio/v1/getHolding": ("api.holding", "GET"),
    "/rest/secure/angelbroking/portfolio/v1/getAllHolding": ("api.allholding", "GET"),
    "/rest/secure/angelbroking/order/v1/convertPosition": ("api.convert.position", "POST"),
    "/rest/secure/angelbroking/user/v1/getRMS": ("api.rms.limit", "GET"),
}


class AngelOneError(RuntimeError):
    pass


def _env_file(path):
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


def _totp(secret):
    secret = secret.upper().replace(" ", "")
    if secret.isdigit() and len(secret) == 6:
        return secret
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(time.time() // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}"


class AngelOneProxy:
    def __init__(self):
        self.client = None
        self.credentials_path = None

    def _login(self):
        path = SETTINGS["credentials_file"]
        values = _env_file(path)
        api_key = _value(values, "ANGELONE_API_KEY", "API_KEY")
        client_code = _value(values, "ANGELONE_CLIENT_CODE", "CLIENT_CODE", "CLIENTCODE")
        password = _value(values, "ANGELONE_PASSWORD", "PASSWORD", "MPIN")
        totp_secret = _value(values, "ANGELONE_TOTP_SECRET", "TOTP_SECRET", "TOTP")
        client = SmartConnect(api_key=api_key)
        response = client.generateSession(client_code, password, _totp(totp_secret))
        if not isinstance(response, dict) or not response.get("status"):
            raise AngelOneError("Angel One login failed")
        self.client, self.credentials_path = client, path
        return client

    def _client(self):
        if self.client is None or self.credentials_path != SETTINGS["credentials_file"]:
            return self._login()
        return self.client

    def forward(self, path, data=None, order_id=None):
        client = self._client()
        if order_id is not None:
            return client.individual_order_details(order_id)
        try:
            route, method = ROUTES[path]
        except KeyError as exc:
            raise AngelOneError("Unsupported Angel One proxy route") from exc
        return client._postRequest(route, data or {}) if method == "POST" else client._getRequest(route)


PROXY = AngelOneProxy()
