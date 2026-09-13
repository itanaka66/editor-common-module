"""HTTP Basic Auth gate for a whole FastAPI instance, with a pluggable
`authenticate` callback so it works for both the original single shared
admin/password pair and a multi-user DB-backed account store (see
`editor_common.users`).

Neither editor has a session/cookie login flow — HTTP Basic keeps the
browser's own credential prompt/autofill and needs no login page, which
suited a single shared password. It works just as well for multiple
accounts: the callback below is asked "is this username/password pair
valid?" on every request, so swapping in a per-user check is a one-line
change at the call site (see `editor_common.users.authenticate_user`).

This middleware also carries a simple in-memory brute-force guard, since
HTTP Basic Auth has no built-in lockout — without it a script could retry
passwords as fast as the network allows. It is keyed by client IP (not by
username), so it protects the login endpoint itself regardless of which
account is being guessed at.

Usage — single shared password (unchanged from before)::

    from editor_common.auth import make_basic_auth_middleware

    BasicAuthMiddleware = make_basic_auth_middleware(
        authenticate=lambda u, p: u == settings.admin_username and p == settings.admin_password,
    )

Usage — multiple DB-backed accounts::

    from editor_common.auth import make_basic_auth_middleware
    from editor_common.users import authenticate_user
    from .db import SessionLocal
    from .models import User

    def _authenticate(username, password):
        db = SessionLocal()
        try:
            return authenticate_user(db, User, username, password) is not None
        finally:
            db.close()

    BasicAuthMiddleware = make_basic_auth_middleware(authenticate=_authenticate)
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


def single_credential_pair(get_credentials: Callable[[], tuple[str, str]]) -> Callable[[str, str], bool]:
    """Adapts the old `get_credentials() -> (username, password)` shape
    (one fixed shared pair, re-read on every call so a live settings
    override still works) into the `authenticate(username, password)`
    callback `make_basic_auth_middleware` expects. Kept for callers that
    have not moved to a multi-user account store."""

    def authenticate(username: str, password: str) -> bool:
        admin_username, admin_password = get_credentials()
        return secrets.compare_digest(username, admin_username) and secrets.compare_digest(password, admin_password)

    return authenticate


def make_basic_auth_middleware(
    authenticate: Callable[[str, str], bool],
    public_paths: Iterable[str] = ("/api/v1/health", "/docs", "/openapi.json", "/redoc"),
    failure_window_seconds: int = FAILURE_WINDOW_SECONDS,
    max_failures: int = MAX_FAILURES,
    lockout_seconds: int = LOCKOUT_SECONDS,
):
    """Builds a `BasicAuthMiddleware` class that checks each request's
    credentials with `authenticate(username, password) -> bool`.

    `authenticate` is called on every request (nothing here caches it), so
    a DB-backed check sees newly-created/deleted users and changed
    passwords immediately — no restart needed.
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

            try:
                ok = authenticate(username, password)
            except Exception:
                logger.exception("authenticate() raised; treating as failed login")
                ok = False
            if not ok:
                _failures[ip].append(now)
                return _unauthorized()
            _failures[ip].clear()
            request.state.username = username
            return await call_next(request)

    # Exposed for tests: Starlette's TestClient always reports the same
    # client IP, so failures from one test would otherwise bleed into the
    # next. Call `BasicAuthMiddleware.reset_rate_limit()` in an autouse
    # fixture between tests.
    BasicAuthMiddleware._failures = _failures
    BasicAuthMiddleware.reset_rate_limit = staticmethod(_failures.clear)

    return BasicAuthMiddleware
