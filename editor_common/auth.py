"""Auth gate for a whole FastAPI instance: HTTP Basic Auth (a pluggable
`authenticate` callback, so it works for a single shared admin/password
pair or a multi-user DB-backed account store — see `editor_common.users`),
optionally combined with a signed session cookie for browser/OAuth2 login
(see `editor_common.oauth` and `editor_common.session_tokens`).

Usage — Basic Auth only, single shared password (unchanged from before)::

    from editor_common.auth import make_basic_auth_middleware

    BasicAuthMiddleware = make_basic_auth_middleware(
        authenticate=lambda u, p: u == settings.admin_username and p == settings.admin_password,
    )

Usage — Basic Auth only, multiple DB-backed accounts::

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

Usage — session cookie (OAuth2 login) *and* Basic Auth (e.g. for scripts/CI)
side by side; a valid session cookie is tried first, Basic Auth is the
fallback::

    from editor_common.auth import make_auth_middleware, make_session_verifier
    from .db import SessionLocal
    from .models import User

    AuthMiddleware = make_auth_middleware(
        authenticate_basic=_authenticate,
        verify_session=make_session_verifier(settings.session_secret, SessionLocal, User),
    )
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

DEFAULT_SESSION_COOKIE_NAME = "session"


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
    callback these middlewares expect. Kept for callers that have not moved
    to a multi-user account store."""

    def authenticate(username: str, password: str) -> bool:
        admin_username, admin_password = get_credentials()
        return secrets.compare_digest(username, admin_username) and secrets.compare_digest(password, admin_password)

    return authenticate


def make_session_verifier(secret: str, session_factory: Callable, user_model: type) -> Callable[[str], str | None]:
    """Builds a `verify_session(token) -> username | None` callback for
    `make_auth_middleware`, backed by a signed cookie from
    `editor_common.session_tokens` (as set by `editor_common.oauth`'s login
    routes) and a fresh DB lookup on every call — so a deactivated account
    (`is_active=False`) or a deleted user stops working immediately, not
    only after the cookie itself expires.
    """
    from . import session_tokens

    def verify_session(token: str) -> str | None:
        payload = session_tokens.verify(secret, token)
        if not payload:
            return None
        uid = payload.get("uid")
        if uid is None:
            return None
        db = session_factory()
        try:
            user = db.get(user_model, uid)
        finally:
            db.close()
        if user is None or not getattr(user, "is_active", True):
            return None
        return user.username

    return verify_session


def make_auth_middleware(
    *,
    authenticate_basic: Callable[[str, str], bool] | None = None,
    verify_session: Callable[[str], str | None] | None = None,
    session_cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
    public_paths: Iterable[str] = ("/api/v1/health", "/docs", "/openapi.json", "/redoc"),
    public_path_prefixes: Iterable[str] = (),
    failure_window_seconds: int = FAILURE_WINDOW_SECONDS,
    max_failures: int = MAX_FAILURES,
    lockout_seconds: int = LOCKOUT_SECONDS,
):
    """Builds an auth middleware class that accepts either a valid session
    cookie (checked first, via `verify_session`, typically
    `make_session_verifier(...)`) or HTTP Basic credentials (via
    `authenticate_basic`) — pass either or both. At least one must be given.

    `public_path_prefixes` is for route *families* rather than one fixed
    path — most notably `editor_common.oauth`'s login/callback/logout
    routes, which must stay reachable without a session cookie (that's the
    whole point) and include a `{provider}` path segment `public_paths`
    can't match. Pass the same `prefix` given to `register_oauth_routes`
    (default `"/auth"`) here, e.g. `public_path_prefixes=("/auth",)`.

    The brute-force guard (see module docstring) only ever applies to the
    Basic Auth path; a session cookie is either valid or it isn't, and
    forging one requires the signing secret, not guessable attempts.
    """
    if authenticate_basic is None and verify_session is None:
        raise ValueError("make_auth_middleware needs at least one of authenticate_basic, verify_session")

    public_paths = set(public_paths)
    public_path_prefixes = tuple(public_path_prefixes)
    _failures: dict[str, list[float]] = defaultdict(list)

    def _is_public(path: str) -> bool:
        return path in public_paths or path.startswith(public_path_prefixes)

    def _prune(timestamps: list[float], now: float) -> list[float]:
        return [t for t in timestamps if now - t < failure_window_seconds]

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method == "OPTIONS" or _is_public(request.url.path):
                return await call_next(request)

            if verify_session is not None:
                token = request.cookies.get(session_cookie_name)
                if token:
                    try:
                        username = verify_session(token)
                    except Exception:
                        logger.exception("verify_session() raised; falling back to Basic Auth")
                        username = None
                    if username:
                        request.state.username = username
                        return await call_next(request)

            if authenticate_basic is None:
                return _unauthorized()

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
                ok = authenticate_basic(username, password)
            except Exception:
                logger.exception("authenticate_basic() raised; treating as failed login")
                ok = False
            if not ok:
                _failures[ip].append(now)
                return _unauthorized()
            _failures[ip].clear()
            request.state.username = username
            return await call_next(request)

    # Exposed for tests: Starlette's TestClient always reports the same
    # client IP, so failures from one test would otherwise bleed into the
    # next. Call `AuthMiddleware.reset_rate_limit()` in an autouse fixture
    # between tests.
    AuthMiddleware._failures = _failures
    AuthMiddleware.reset_rate_limit = staticmethod(_failures.clear)

    return AuthMiddleware


def make_basic_auth_middleware(
    authenticate: Callable[[str, str], bool],
    public_paths: Iterable[str] = ("/api/v1/health", "/docs", "/openapi.json", "/redoc"),
    failure_window_seconds: int = FAILURE_WINDOW_SECONDS,
    max_failures: int = MAX_FAILURES,
    lockout_seconds: int = LOCKOUT_SECONDS,
):
    """Basic-Auth-only convenience wrapper around `make_auth_middleware` —
    kept as the simple entry point for callers that don't need session-cookie
    (OAuth2) login. See its docstring for the `authenticate` contract.
    """
    return make_auth_middleware(
        authenticate_basic=authenticate,
        public_paths=public_paths,
        failure_window_seconds=failure_window_seconds,
        max_failures=max_failures,
        lockout_seconds=lockout_seconds,
    )
