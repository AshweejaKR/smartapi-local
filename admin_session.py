"""Process-local browser Admin PIN sessions and failed-login limits."""
from collections import defaultdict, deque
import hmac
import os
import secrets
import threading
import time


COOKIE = "smartapi_admin"
TTL = 12 * 60 * 60
WINDOW = 5 * 60
MAX_FAILURES = 5
sessions = {}
failures = defaultdict(deque)
lock = threading.Lock()


def reset():
    """A server restart invalidates all browser sessions."""
    with lock:
        sessions.clear()
        failures.clear()


def valid(token):
    with lock:
        expiry = sessions.get(token)
        if expiry is None:
            return False
        if expiry <= time.monotonic():
            sessions.pop(token, None)
            return False
        return True


def authenticate(pin, ip):
    """Return (session ID, limited); never persist or echo the submitted PIN."""
    now = time.monotonic()
    with lock:
        attempts = failures[ip]
        while attempts and attempts[0] <= now - WINDOW:
            attempts.popleft()
        if len(attempts) >= MAX_FAILURES:
            return None, True
        if not hmac.compare_digest(pin.encode("utf-8"), os.environ["SMARTAPI_ADMIN_PIN"].encode("utf-8")):
            attempts.append(now)
            return None, False
        failures.pop(ip, None)
        token = secrets.token_urlsafe(32)
        sessions[token] = now + TTL
        return token, False


def revoke(token):
    with lock:
        sessions.pop(token, None)
