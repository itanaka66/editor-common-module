"""CORS middleware whose allowed-origins list can change at runtime.

Starlette's `CORSMiddleware` freezes `allow_origins` at app-startup time, so
changing it normally requires editing `.env` and restarting the container.
This subclass overrides the one method that reads that list
(`is_allowed_origin`) to call `get_origins()` fresh on every request instead
— letting a settings screen change it live.
"""
from typing import Callable, Iterable

from starlette.middleware.cors import CORSMiddleware


def make_dynamic_cors_middleware(get_origins: Callable[[], Iterable[str]]):
    """Builds a `DynamicCORSMiddleware` class that consults `get_origins()`
    on every request instead of the static `allow_origins` list."""

    class DynamicCORSMiddleware(CORSMiddleware):
        def is_allowed_origin(self, origin: str) -> bool:
            # "*" means "allow any origin", checked explicitly here rather
            # than relying on the base class's allow_all_origins (computed
            # once, at __init__, from the static allow_origins this
            # subclass never actually uses). The response still echoes back
            # the real Origin header rather than a literal "*", which is
            # required anyway whenever Access-Control-Allow-Credentials is
            # sent.
            origins = set(get_origins())
            return "*" in origins or origin in origins

    return DynamicCORSMiddleware
