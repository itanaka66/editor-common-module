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

- `auth` — HTTP Basic Auth middleware (single shared password or multi-user, pluggable) with a brute-force lockout guard.
- `users` / `passwords` — multi-user account store: a `UserMixin` for each app's own model, plus create/authenticate/change-password/delete helpers backed by stdlib PBKDF2 hashing.
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

See each module's docstring for the rest of the constructor arguments, and
[INTEGRATION.md](INTEGRATION.md) for a full file-by-file wiring guide for
both `integrated_writers_editor` and `integrated_novel_editor`.

## License

[MIT](LICENSE)
