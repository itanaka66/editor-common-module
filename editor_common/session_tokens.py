"""Signed, stateless session tokens for a login cookie — stdlib-only
(HMAC-SHA256 + base64url + JSON, no external dependency), in the same spirit
as `editor_common.passwords`: no server-side session table to manage, no new
dependency, and the whole session lives in one cookie value.

Not encryption — the payload is base64-encoded, not hidden, so never put a
secret (a password, an API key) in it. What the signature guarantees is that
the payload wasn't forged or modified without the secret key, and `verify()`
also enforces the expiry embedded at signing time.
"""
import base64
import hashlib
import hmac
import json
import time
from typing import Any

DEFAULT_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def sign(secret: str, payload: dict[str, Any], max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> str:
    """Serializes `payload` plus an expiry into one signed token string."""
    body = {**payload, "_exp": int(time.time()) + max_age_seconds}
    body_b64 = _b64encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{body_b64}.{_b64encode(signature)}"


def verify(secret: str, token: str) -> dict[str, Any] | None:
    """Returns the original payload (without `_exp`) if `token` is
    well-formed, correctly signed with `secret`, and not expired — None
    otherwise. Never raises on malformed input."""
    if not token or "." not in token:
        return None
    body_b64, _, signature_b64 = token.partition(".")
    expected = hmac.new(secret.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        actual = _b64decode(signature_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, actual):
        return None
    try:
        body = json.loads(_b64decode(body_b64))
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("_exp", 0) < time.time():
        return None
    body.pop("_exp", None)
    return body
