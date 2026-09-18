"""Minimal OAuth2 "Authorization Code" client for logging a user in with
Google or GitHub, plus a ready-to-mount Starlette route pair
(`/login/{provider}`, `/callback/{provider}`, `/logout`) that ties it to
`editor_common.session_tokens` for the resulting login cookie.

This is deliberately not a general OAuth2/OIDC library — it only does what
a "Sign in with Google/GitHub" button needs: redirect to the provider,
exchange the returned code for an access token, fetch the user's email,
and hand that email to the caller's own `get_or_create_user` callback.
"""
import logging
import secrets
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

logger = logging.getLogger(__name__)

STATE_COOKIE_MAX_AGE_SECONDS = 600  # just long enough to complete the redirect round-trip


class OAuthError(Exception):
    pass


@dataclass
class OAuthProvider:
    name: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scope: str
    redirect_uri: str
    extract_email: Callable[[dict], Optional[str]]
    extract_name: Callable[[dict], Optional[str]] = field(default=lambda info: None)
    extra_authorize_params: dict[str, str] = field(default_factory=dict)


def google_provider(client_id: str, client_secret: str, redirect_uri: str) -> OAuthProvider:
    return OAuthProvider(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://www.googleapis.com/oauth2/v3/userinfo",
        scope="openid email profile",
        redirect_uri=redirect_uri,
        # Google's userinfo omits email_verified only when there's no email
        # scope at all; when present, treat missing/true the same way and
        # only reject an explicit false.
        extract_email=lambda info: info.get("email") if info.get("email_verified", True) else None,
        extract_name=lambda info: info.get("name"),
        extra_authorize_params={"access_type": "online", "prompt": "select_account"},
    )


def github_provider(client_id: str, client_secret: str, redirect_uri: str) -> OAuthProvider:
    return OAuthProvider(
        name="github",
        client_id=client_id,
        client_secret=client_secret,
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        userinfo_url="https://api.github.com/user",
        scope="read:user user:email",
        redirect_uri=redirect_uri,
        extract_email=lambda info: info.get("email"),
        extract_name=lambda info: info.get("name") or info.get("login"),
    )


def build_authorize_url(provider: OAuthProvider, state: str) -> str:
    params = {
        "client_id": provider.client_id,
        "redirect_uri": provider.redirect_uri,
        "scope": provider.scope,
        "state": state,
        "response_type": "code",
        **provider.extra_authorize_params,
    }
    return f"{provider.authorize_url}?{urlencode(params)}"


async def exchange_code(provider: OAuthProvider, code: str) -> str:
    """Trades an authorization `code` for an access token."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            provider.token_url,
            data={
                "client_id": provider.client_id,
                "client_secret": provider.client_secret,
                "code": code,
                "redirect_uri": provider.redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
    token = data.get("access_token")
    if not token:
        raise OAuthError(f"{provider.name}: token endpoint returned no access_token: {data}")
    return token


async def fetch_userinfo(provider: OAuthProvider, access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(provider.userinfo_url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
        r.raise_for_status()
        info = r.json()
        if provider.name == "github" and not info.get("email"):
            # /user only returns an email if the account has a public one
            # set; fall back to the emails endpoint for the primary,
            # verified address (needs the user:email scope).
            er = await c.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            if er.status_code == 200:
                primary = next((e["email"] for e in er.json() if e.get("primary") and e.get("verified")), None)
                if primary:
                    info["email"] = primary
    return info


def register_oauth_routes(
    app,
    *,
    providers: dict[str, OAuthProvider],
    session_secret: str,
    get_or_create_user: Callable[[str, Optional[str]], Awaitable[object] | object],
    prefix: str = "/auth",
    session_cookie_name: str = "session",
    session_max_age_seconds: int,
    state_cookie_name: str = "oauth_state",
    on_login_redirect: str = "/",
    on_logout_redirect: str = "/",
    on_error_redirect: Optional[str] = None,
    secure_cookies: bool = True,
) -> None:
    """Adds `GET {prefix}/login/{{provider}}`, `GET {prefix}/callback/{{provider}}`,
    and `GET/POST {prefix}/logout` routes to `app` (any Starlette-compatible
    app — FastAPI included, since it subclasses Starlette's router).

    `get_or_create_user(email, name)` is the caller's own lookup — typically
    `editor_common.users.get_or_create_oauth_user` bound to that app's User
    model and DB session — and must return an object with `.id` and
    `.username`. It may be sync or async (both are awaited transparently).

    On success, sets a signed session cookie readable by
    `editor_common.auth.make_session_verifier` and redirects to
    `on_login_redirect`. On any failure (unknown provider, state mismatch,
    provider error, no email in the provider's response) it redirects to
    `on_error_redirect` (defaulting to `on_login_redirect`) — never raises
    into the request, since this always runs at the end of a browser
    redirect chain with no caller to catch an exception.
    """
    from editor_common import session_tokens

    error_redirect = on_error_redirect or on_login_redirect

    async def _maybe_await(value):
        if hasattr(value, "__await__"):
            return await value
        return value

    async def login(request: Request) -> Response:
        provider_name = request.path_params["provider"]
        provider = providers.get(provider_name)
        if provider is None:
            return Response(f"Unknown OAuth provider: {provider_name}", status_code=404)
        state = secrets.token_urlsafe(24)
        resp = RedirectResponse(build_authorize_url(provider, state), status_code=302)
        resp.set_cookie(
            state_cookie_name, state, max_age=STATE_COOKIE_MAX_AGE_SECONDS,
            httponly=True, secure=secure_cookies, samesite="lax",
        )
        return resp

    async def callback(request: Request) -> Response:
        provider_name = request.path_params["provider"]
        provider = providers.get(provider_name)
        if provider is None:
            return Response(f"Unknown OAuth provider: {provider_name}", status_code=404)

        expected_state = request.cookies.get(state_cookie_name)
        got_state = request.query_params.get("state")
        code = request.query_params.get("code")
        if not code or not expected_state or not got_state or not secrets.compare_digest(expected_state, got_state):
            logger.warning("OAuth callback for %s rejected: missing/mismatched state or code", provider_name)
            resp = RedirectResponse(error_redirect, status_code=302)
            resp.delete_cookie(state_cookie_name)
            return resp

        try:
            access_token = await exchange_code(provider, code)
            info = await fetch_userinfo(provider, access_token)
            email = provider.extract_email(info)
            name = provider.extract_name(info)
        except Exception:
            logger.exception("OAuth login via %s failed", provider_name)
            resp = RedirectResponse(error_redirect, status_code=302)
            resp.delete_cookie(state_cookie_name)
            return resp

        if not email:
            logger.warning("OAuth login via %s succeeded but returned no usable email", provider_name)
            resp = RedirectResponse(error_redirect, status_code=302)
            resp.delete_cookie(state_cookie_name)
            return resp

        user = await _maybe_await(get_or_create_user(email, name))
        token = session_tokens.sign(session_secret, {"uid": user.id, "username": user.username}, session_max_age_seconds)
        resp = RedirectResponse(on_login_redirect, status_code=302)
        resp.delete_cookie(state_cookie_name)
        resp.set_cookie(
            session_cookie_name, token, max_age=session_max_age_seconds,
            httponly=True, secure=secure_cookies, samesite="lax",
        )
        return resp

    async def logout(request: Request) -> Response:
        resp = RedirectResponse(on_logout_redirect, status_code=302)
        resp.delete_cookie(session_cookie_name)
        return resp

    app.add_route(f"{prefix}/login/{{provider}}", login, methods=["GET"])
    app.add_route(f"{prefix}/callback/{{provider}}", callback, methods=["GET"])
    app.add_route(f"{prefix}/logout", logout, methods=["GET", "POST"])
