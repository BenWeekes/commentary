"""Simple HMAC-signed session cookie auth for ops pages."""

import base64
import hashlib
import hmac
import json
import time
from http.cookies import SimpleCookie


def create_session_cookie(username: str, secret: str, ttl_hours: int = 12) -> str:
    """Create an HMAC-signed session cookie value.

    Returns a base64-encoded string: payload.signature
    """
    now = time.time()
    payload = {
        "u": username,
        "iat": int(now),
        "exp": int(now + ttl_hours * 3600),
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_session_cookie(cookie_value: str, secret: str) -> dict | None:
    """Verify an HMAC-signed session cookie.

    Returns the payload dict if valid and not expired, None otherwise.
    """
    if not cookie_value or "." not in cookie_value:
        return None
    parts = cookie_value.split(".", 1)
    if len(parts) != 2:
        return None
    payload_b64, sig = parts
    expected = hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def parse_cookie(cookie_header: str, name: str) -> str | None:
    """Extract a named cookie from a Cookie header string."""
    if not cookie_header:
        return None
    sc = SimpleCookie()
    try:
        sc.load(cookie_header)
    except Exception:
        return None
    morsel = sc.get(name)
    return morsel.value if morsel else None
