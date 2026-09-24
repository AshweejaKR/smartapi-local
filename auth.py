"""SQLite-backed fixed-TOTP authentication for the local SmartAPI server."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid

from fastapi import Request

from common import connect, failed, ok


def init_auth():
    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "client_code TEXT PRIMARY KEY, password_hash TEXT NOT NULL, api_key TEXT NOT NULL, "
            "totp TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL, mobile TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 1)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, client_code TEXT NOT NULL, access_token TEXT UNIQUE NOT NULL, "
            "refresh_token TEXT UNIQUE NOT NULL, feed_token TEXT NOT NULL, "
            "access_expires_at INTEGER NOT NULL, refresh_expires_at INTEGER NOT NULL, "
            "active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO users VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (
                "DUMMY001", password_hash("password"), "DUMMY_API_KEY", "123456",
                "Local Test User", "dummy@example.test", "9000000000",
            ),
        )


def invalidate_sessions(client_code):
    """Revoke active tokens after an admin credential/status change."""
    with connect() as conn:
        conn.execute(
            "UPDATE sessions SET active = 0 WHERE client_code = ?", (client_code,)
        )


def password_hash(password):
    return hashlib.sha256(password.encode()).hexdigest()


def setting(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def new_tokens(client_code):
    now = int(time.time())
    force_expired = os.getenv("SMARTAPI_FORCE_TOKEN_EXPIRY", "").lower() in {"1", "true", "yes"}
    access_expires_at = now - 1 if force_expired else now + setting("SMARTAPI_ACCESS_TOKEN_TTL_SECONDS", 3600)
    refresh_expires_at = now + setting("SMARTAPI_REFRESH_TOKEN_TTL_SECONDS", 86400)
    payload = base64.urlsafe_b64encode(json.dumps({"sub": client_code, "exp": access_expires_at}).encode()).decode().rstrip("=")
    access = f"eyJhbGciOiJub25lIn0.{payload}.{secrets.token_urlsafe(12)}"
    return access, secrets.token_urlsafe(32), secrets.token_urlsafe(32), access_expires_at, refresh_expires_at


def token_session(request):
    authorization = request.headers.get("Authorization", "")
    access = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not access:
        return None
    with connect() as conn:
        return conn.execute(
            "SELECT sessions.* FROM sessions JOIN users USING(client_code) "
            "WHERE access_token = ? AND active = 1 AND users.enabled = 1", (access,)
        ).fetchone()


def active_session(request):
    session = token_session(request)
    return session if session and session["access_expires_at"] > int(time.time()) else None


async def payload(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def profile_data(user):
    return {
        "clientcode": user["client_code"],
        "name": user["name"],
        "email": user["email"],
        "mobileno": user["mobile"],
        "exchanges": ["NSE", "BSE", "NFO", "MCX", "CDS"],
        "products": ["CNC", "MIS", "NRML"],
    }


async def login(request: Request):
    data = await payload(request)
    client_code = data.get("clientcode", data.get("clientCode", ""))
    password = data.get("password", "")
    if not isinstance(client_code, str) or not isinstance(password, str):
        return failed("Invalid client code, password, TOTP, or API key")
    totp = str(data.get("totp", ""))
    api_key = request.headers.get("X-PrivateKey", "")
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE client_code = ?", (client_code,)).fetchone()
        valid = user and user["enabled"] and hmac.compare_digest(user["password_hash"], password_hash(password))
        valid = valid and hmac.compare_digest(user["api_key"].encode(), api_key.encode())
        totp_disabled = os.getenv("SMARTAPI_DISABLE_TOTP", "").lower() in {"1", "true", "yes"}
        valid = valid and (totp_disabled or hmac.compare_digest(user["totp"].encode(), totp.encode()))
        if not valid:
            return failed("Invalid client code, password, TOTP, or API key")
        access, refresh, feed, access_expires, refresh_expires = new_tokens(client_code)
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (str(uuid.uuid4()), client_code, access, refresh, feed, access_expires, refresh_expires, int(time.time())),
        )
    return ok({"jwtToken": access, "refreshToken": refresh, "feedToken": feed})


async def generate_tokens(request: Request):
    refresh = (await payload(request)).get("refreshToken", "")
    if not isinstance(refresh, str):
        return failed("Invalid or expired refresh token", 403,
                      {"jwtToken": "", "refreshToken": "", "feedToken": ""})
    with connect() as conn:
        session = conn.execute(
            "SELECT sessions.* FROM sessions JOIN users ON users.client_code = sessions.client_code "
            "WHERE refresh_token = ? AND active = 1 AND users.enabled = 1", (refresh,)
        ).fetchone()
        if session is None or session["refresh_expires_at"] <= int(time.time()):
            return failed("Invalid or expired refresh token", 403,
                          {"jwtToken": "", "refreshToken": refresh, "feedToken": ""})
        access, _, feed, access_expires, _ = new_tokens(session["client_code"])
        conn.execute(
            "UPDATE sessions SET access_token = ?, feed_token = ?, access_expires_at = ? WHERE id = ?",
            (access, feed, access_expires, session["id"]),
        )
    return ok({"jwtToken": access, "refreshToken": refresh, "feedToken": feed})


async def profile(request: Request):
    session = active_session(request)
    if session is None:
        return failed("Invalid or expired token", 403)
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE client_code = ?", (session["client_code"],)).fetchone()
    return ok(profile_data(user))


async def logout(request: Request):
    session = active_session(request)
    if session is None:
        return failed("Invalid or expired token", 403)
    client_code = (await payload(request)).get("clientcode", "")
    if client_code and client_code != session["client_code"]:
        return failed("Client code does not match token", 403)
    with connect() as conn:
        conn.execute("UPDATE sessions SET active = 0 WHERE id = ?", (session["id"],))
    return ok()

