"""Single-user HTTP Basic Auth gate for a whole FastAPI instance.

Verbatim shared logic from both editors: neither app has a user model, so
each protects its entire instance with one shared admin/password pair. This
middleware also carries a simple in-memory brute-force guard, since HTTP
Basic Auth has no built-in lockout — without it a script could retry
passwords as fast as the network allows.

Usage::

    from editor_common.auth import make_basic_auth_middleware

    BasicAuthMiddleware = make_basic_auth_middleware(
        get_credentials=lambda: (settings.admin_username, settings.admin_password),
        public_paths={"/api/v1/health", "/docs", "/openapi.json", "/redoc"},
    )
    app.add_middleware(BasicAuthMiddleware)
"""
import base64
import logging
import secrets
import time
from collections import defaultdict
from typing import Callable, Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Keyed by client IP; single-process only, and resets on restart.
FAILURE_WINDOW_SECONDS = 300
MAX_FAILURES = 10
LOCKOUT_SECONDS = 300


def _unauthorized(retry_after: int | None = None) -> Response:
    headers = {"WWW-Authenticate": "Basic"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return Response(
        status_code=401 if retry_after is None else 429,
        content='{"detail":"Too many failed login attempts. Try again later."}' if retry_after is not None
        else '{"detail":"Not authenticated"}',
        media_type="application/json",
        headers=headers,
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def make_basic_auth_middleware(
    get_credentials: Callable[[], tuple[str, str]],
    public_paths: Iterable[str] = ("/api/v1/health", "/docs", "/openapi.json", "/redoc"),
    failure_window_seconds: int = FAILURE_WINDOW_SECONDS,
    max_failures: int = MAX_FAILURES,
    lockout_seconds: int = LOCKOUT_SECONDS,
):
    """Builds a `BasicAuthMiddleware` class bound to the given app's settings.

    `get_credentials` is called on every request (not cached), so a live
    settings override (e.g. from a runtime-config screen) takes effect
    immediately.
    """
    public_paths = set(public_paths)
    _failures: dict[str, list[float]] = defaultdict(list)

    def _prune(timestamps: list[float], now: float) -> list[float]:
        return [t for t in timestamps if now - t < failure_window_seconds]

    class BasicAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method == "OPTIONS" or request.url.path in public_paths:
                return await call_next(request)

            ip = _client_ip(request)
            now = time.time()
            _failures[ip] = _prune(_failures[ip], now)
            if len(_failures[ip]) >= max_failures:
                logger.warning(
                    "Login rate limit hit for %s (%d failures in the last %ds)",
                    ip, len(_failures[ip]), failure_window_seconds,
                )
                return _unauthorized(retry_after=lockout_seconds)

            header = request.headers.get("authorization", "")
            if not header.startswith("Basic "):
                return _unauthorized()
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                username, _, password = decoded.partition(":")
            except Exception:
                _failures[ip].append(now)
                return _unauthorized()

            admin_username, admin_password = get_credentials()
            user_ok = secrets.compare_digest(username, admin_username)
            pass_ok = secrets.compare_digest(password, admin_password)
            if not (user_ok and pass_ok):
                _failures[ip].append(now)
                return _unauthorized()
            _failures[ip].clear()
            return await call_next(request)

    return BasicAuthMiddleware
