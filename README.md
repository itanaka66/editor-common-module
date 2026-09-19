# editor-common

Shared Python module extracted from the FastAPI backends of
[integrated_writers_editor](https://github.com/itanaka66/integrated_writers_editor)
and [integrated_novel_editor](https://github.com/itanaka66/integrated_novel_editor).

Both apps started as forks of the same backend and, for the pieces below,
stayed byte-for-byte identical or differed only in a handful of app-specific
constants (app name, storage directory, Qdrant collection name, ...). This
package factors those pieces out so both apps can depend on one
implementation instead of two copies drifting apart.

Every helper here takes those small differences as explicit constructor
arguments or callables (e.g. `get_qdrant_url: Callable[[], str]`) rather than
importing an app's own `config`/`models`/`runtime_config` modules, so this
package has no dependency on either consuming app — each app wires it up in
its own `app/*.py` shim.

## Modules

- `auth` — auth middleware: HTTP Basic (single shared password or multi-user, pluggable) with a brute-force lockout guard, optionally combined with a signed session cookie for OAuth2 login. 401/429 responses deliberately omit `WWW-Authenticate: Basic` — every consumer is a JSON API called from a custom SPA login form, and that header makes browsers pop their own native credential dialog on top of the page.
- `users` / `passwords` — multi-user account store: a `UserMixin` for each app's own model, plus create/authenticate/change-password/delete helpers backed by stdlib PBKDF2 hashing, plus get-or-create for OAuth2 first-login auto-registration.
- `oauth` — "Sign in with Google/GitHub": authorization-code OAuth2 client plus ready-to-mount login/callback/logout routes.
- `session_tokens` — stdlib-only signed session tokens (HMAC, no server-side session table) for the OAuth2 login cookie.
- `cors` — CORS middleware whose allowed-origins list can change at runtime.
- `db` — SQLAlchemy engine/session/declarative-base factory.
- `revisions` — episode revision snapshotting (bounded undo trail).
- `backup` — scheduled/on-demand PostgreSQL + Qdrant backups.
- `file_sync` — mirrors episode content to disk and auto-syncs it to a git remote.
- `qdrant_service` — cheap hash-based "vectorization" for scene-similarity search.
- `rag` — Qdrant-backed retrieval index over story memory, using real embeddings.
- `ollama` — thin async client for a local Ollama server (generate/stream/embed).
- `connection_test` — ad-hoc connectivity probes for a settings screen's "test connection" buttons.

## Install

From each app's `apps/api`:

```bash
pip install "editor-common @ git+https://github.com/itanaka66/editor-common-module.git"
```

or add it as a path/git dependency in `pyproject.toml`/`requirements.txt`.

### Updating

This package isn't tagged/versioned per release (`pyproject.toml` stays at
`0.1.0`), so a plain `pip install` of the line above **will not** pick up new
commits once it's already installed — pip sees a package named
`editor-common` already satisfying the requirement and skips it, without
checking whether `main` has moved. Always force it:

```bash
pip install --upgrade --force-reinstall --no-deps "editor-common @ git+https://github.com/itanaka66/editor-common-module.git"
```

- `--upgrade` — re-resolve/reinstall even though something already satisfies the name
- `--force-reinstall` — the actual fix: reinstall even when pip thinks the version (unchanged at `0.1.0`) already matches
- `--no-deps` — skip re-resolving this package's own dependencies (httpx/qdrant-client/sqlalchemy/starlette), which haven't changed

When installing via `requirements.txt`, either add these flags to the whole
`pip install -r requirements.txt` invocation in your deploy/CI script, or
pin the git ref to a specific commit SHA (`@<commit-sha>` in the URL) and
bump that SHA on each intentional upgrade instead.

## Usage sketch

```python
# app/db.py
from editor_common.db import create_db
from .config import settings

_db = create_db(settings.database_url)
engine, SessionLocal, Base, get_db = _db.engine, _db.SessionLocal, _db.Base, _db.get_db
```

```python
# app/auth.py — multi-user, backed by a DB table
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
```

```python
# app/qdrant_service.py
from editor_common.qdrant_service import QdrantSceneStore
from .config import settings

_store = QdrantSceneStore(settings.qdrant_url, collection="novel_scenes")
upsert_scene = _store.upsert_scene
search = _store.search
```

```python
# app/main.py — "Sign in with Google/GitHub", alongside Basic Auth for scripts/CI
from editor_common.auth import make_auth_middleware, make_session_verifier
from editor_common.oauth import register_oauth_routes, google_provider
from editor_common.users import get_or_create_oauth_user
from .db import SessionLocal
from .models import User

AuthMiddleware = make_auth_middleware(
    authenticate_basic=_authenticate,  # from the app/auth.py snippet above
    verify_session=make_session_verifier(settings.session_secret, SessionLocal, User),
    public_path_prefixes=("/auth",),  # keep the login/callback/logout routes themselves reachable
)
app.add_middleware(AuthMiddleware)

def _get_or_create_user(email, name):
    db = SessionLocal()
    try:
        return get_or_create_oauth_user(db, User, email, display_name=name)
    finally:
        db.close()

register_oauth_routes(
    app,
    providers={"google": google_provider(settings.google_client_id, settings.google_client_secret,
                                          redirect_uri=f"{settings.public_base_url}/auth/callback/google")},
    session_secret=settings.session_secret,
    get_or_create_user=_get_or_create_user,
    session_max_age_seconds=30 * 24 * 3600,
)
```

See each module's docstring for the rest of the constructor arguments, and
[INTEGRATION.md](INTEGRATION.md) for a full file-by-file wiring guide for
both `integrated_writers_editor` and `integrated_novel_editor`.

## License

[MIT](LICENSE)
