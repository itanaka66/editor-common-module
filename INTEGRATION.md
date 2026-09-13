# 本体アプリとの結合方法

`editor_common` は `integrated_writers_editor` / `integrated_novel_editor` の
どちらにも依存しないので、各アプリ側の `apps/api/app/*.py` を
**その関数・クラスを呼び出すだけの薄いラッパー**に置き換えて結合する。
呼び出し側（`main.py` など）の import 文・呼び出し方は変更不要になるよう、
既存のモジュール名・関数名はそのまま維持する。

## 0. インストール

`apps/api/pyproject.toml`（または `requirements.txt`）に追加する。

```toml
dependencies = [
    # ...既存の依存...
    "editor-common @ git+https://github.com/itanaka66/editor-common-module.git",
]
```

固定したい場合はタグ/コミットを指定する
（`@main` ではなく `@v0.1.0` や `@<commit-sha>`）。

```bash
pip install -e apps/api
```

## 1. `app/db.py`

**Before**（両アプリでほぼ同一）

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

**After**

```python
from editor_common.db import create_db
from .config import settings

_db = create_db(settings.database_url)
engine = _db.engine
SessionLocal = _db.SessionLocal
Base = _db.Base
get_db = _db.get_db
```

`app/models.py` は今まで通り `from .db import Base` を使い続けられる。

## 2. `app/auth.py`（マルチユーザー対応）

**Before**: 単一の管理者ID/パスワードに対する `BasicAuthMiddleware` をファイル内に直接定義。

**After**: HTTP Basic 認証はそのままに、照合先を「固定1組」から「DBのユーザーテーブル」に変更する。

まず `app/models.py` にユーザーテーブルを追加する（両アプリ共通の列は
`editor_common.users.UserMixin` から継承する）。

```python
# app/models.py
from editor_common.users import UserMixin
from .db import Base

class User(Base, UserMixin):
    __tablename__ = "users"
```

Alembic マイグレーションを1本追加してテーブルを作成する
（`id`, `username`, `password_hash`, `is_admin`, `is_active`, `created_at`）。

```python
# app/auth.py
from editor_common.auth import make_basic_auth_middleware
from editor_common.users import authenticate_user
from .db import SessionLocal
from .models import User

def _authenticate(username: str, password: str) -> bool:
    db = SessionLocal()
    try:
        return authenticate_user(db, User, username, password) is not None
    finally:
        db.close()

BasicAuthMiddleware = make_basic_auth_middleware(authenticate=_authenticate)
```

`main.py` 側の `app.add_middleware(BasicAuthMiddleware)` は変更不要。
認証を通ったリクエストは `request.state.username` でログイン中のユーザー名を参照できる
（`make_basic_auth_middleware` が設定する）。

既存の「1組の管理者ID/パスワード」のままにしたい場合は、
`authenticate` に直接ラムダを渡せば移行不要（`editor_common.auth.single_credential_pair`
が旧 `get_credentials` 方式からのアダプタとして残っている）。

```python
from editor_common.auth import make_basic_auth_middleware, single_credential_pair
from .config import settings

BasicAuthMiddleware = make_basic_auth_middleware(
    authenticate=single_credential_pair(
        lambda: (settings.admin_username, settings.admin_password)
    ),
)
```

### ユーザー管理

`editor_common.users` は認証チェックだけでなく作成・パスワード変更・無効化・削除も提供する。
各アプリはこれを呼ぶだけの管理用エンドポイント（`is_admin` な `request.state.username`
のみ許可する等の権限判定はアプリ側で行う）か、`python -m` の管理コマンドを用意する。

```python
from editor_common.users import create_user, change_password, delete_user, list_users, ensure_bootstrap_user
from .db import SessionLocal
from .models import User

db = SessionLocal()
try:
    # 初回デプロイ時、ユーザーテーブルが空なら管理者を1件だけ自動作成
    ensure_bootstrap_user(db, User, settings.admin_username, settings.admin_password)

    create_user(db, User, "alice", "correct horse battery staple", is_admin=False)
    change_password(db, User, "alice", "new-password")
    list_users(db, User)          # -> [User(username="alice"), ...]
    delete_user(db, User, "alice")
finally:
    db.close()
```

パスワードは `editor_common.passwords`（stdlib の PBKDF2-HMAC、追加依存なし）で
ハッシュ化されて保存される。`create_user`/`authenticate_user` を使う限り生パスワードや
ハッシュを直接扱う必要はない。

## 3. `app/cors.py`

**After**

```python
from editor_common.cors import make_dynamic_cors_middleware
from . import runtime_config as rc

DynamicCORSMiddleware = make_dynamic_cors_middleware(get_origins=rc.get_cors_origins)
```

`main.py` の
`app.add_middleware(DynamicCORSMiddleware, allow_methods=['*'], allow_headers=['*'], allow_credentials=True)`
はそのまま。

## 4. `app/revisions.py`

**After**

```python
from editor_common.revisions import make_revision_snapshotter
from .models import EpisodeRevision

snapshot_revision = make_revision_snapshotter(EpisodeRevision)
```

`main.py` の `from .revisions import snapshot_revision` は変更不要。

## 5. `app/qdrant_service.py`

**After**（novel 側は `collection="novel_scenes"`、writers 側は
`collection="writers_scenes"` のみ差し替える）

```python
from editor_common.qdrant_service import QdrantSceneStore
from .config import settings

_store = QdrantSceneStore(settings.qdrant_url, collection="writers_scenes")

client = _store.client
ensure_collection = _store.ensure_collection
vectorize = _store.vectorize
upsert_scene = _store.upsert_scene
search = _store.search
```

## 6. `app/rag.py`

Ollama の埋め込みには `runtime_config` 経由の実行時上書きが必要なため、
`get_effective_config()` をそのつど呼ぶクロージャを渡す。

```python
from editor_common.rag import RagStore
from .ollama import embed as _ollama_embed
from .runtime_config import get_effective_config

async def _embed(texts):
    cfg = get_effective_config()
    return await _ollama_embed(texts, cfg.ollama_embed_model, cfg.ollama_url)

_store = RagStore(
    get_qdrant_url=lambda: get_effective_config().qdrant_url,
    embed=_embed,
    collection="writers_story_memory",  # novel 側は "novel_story_memory"
)

index = _store.index
search = _store.search
search_all_projects = _store.search_all_projects
```

## 7. `app/ollama.py`

`editor_common.ollama` は `model`/`url` を必須引数にしたので、
`runtime_config` からの実行時解決はアプリ側のラッパーに残す。

```python
from editor_common import ollama as _common_ollama
from .runtime_config import get_effective_config

async def generate(prompt, model=None, url=None, timeout=240):
    cfg = get_effective_config()
    return await _common_ollama.generate(prompt, model or cfg.ollama_model, url or cfg.ollama_url, timeout)

async def generate_with_usage(prompt, model=None, url=None, timeout=240):
    cfg = get_effective_config()
    return await _common_ollama.generate_with_usage(prompt, model or cfg.ollama_model, url or cfg.ollama_url, timeout)

async def stream_generate(prompt, model=None, url=None, timeout=240):
    cfg = get_effective_config()
    async for chunk in _common_ollama.stream_generate(prompt, model or cfg.ollama_model, url or cfg.ollama_url, timeout):
        yield chunk

async def embed(texts):
    cfg = get_effective_config()
    return await _common_ollama.embed(texts, cfg.ollama_embed_model, cfg.ollama_url)
```

novel 側の `controller_generate`（コントローラー専用モデル呼び出し）は
アプリ固有ロジックなのでこのファイルにそのまま残す。

## 8. `app/connection_test.py`

```python
from editor_common import connection_test as _common

from .db import SessionLocal

def test_database():
    return _common.test_database(SessionLocal)

test_qdrant = _common.test_qdrant
test_ollama = _common.test_ollama
test_anthropic = _common.test_anthropic
test_openai = _common.test_openai
test_google = _common.test_google
```

`main.py` の呼び出し（`connection_test.test_database()` など）は変更不要。

## 9. `app/backup.py`

```python
from editor_common.backup import BackupManager
from .config import settings
from .runtime_config import get_effective_config

_manager = BackupManager(
    backup_dir=settings.backup_dir,
    database_url=settings.database_url,
    get_qdrant_url=lambda: get_effective_config().qdrant_url,
    collection="writers_story_memory",  # novel 側は "novel_story_memory"
    retention_count=settings.backup_retention_count,
    default_db_name="writers",  # novel 側は "novel"
)

run_backup = _manager.run_backup
list_backups = _manager.list_backups

async def backup_loop():
    await _manager.backup_loop(
        enabled=settings.backup_enabled,
        interval_seconds=settings.backup_interval_seconds,
    )
```

## 10. `app/file_sync.py`

```python
from editor_common.file_sync import FileSyncManager
from .config import settings

_manager = FileSyncManager(
    storage_dir=settings.writers_storage_dir,  # novel 側は settings.novel_storage_dir
    git_remote_url=settings.git_remote_url,
    git_autosync_interval_seconds=settings.git_autosync_interval_seconds,
    commit_message="Auto-save from Integrated writers Editor (INE)",
    git_author_name="INE Auto-Sync",
    git_author_email="ine-autosync@localhost",
)

storage_root = _manager.storage_root
project_dir = _manager.project_dir
episode_file_path = _manager.episode_file_path
write_episode_file = _manager.write_episode_file
delete_episode_file = _manager.delete_episode_file
sync_once = _manager.sync_once
autosync_loop = _manager.autosync_loop
```

## 移行の進め方

1. まず `writers` 側で 1 ファイルずつ置き換え、
   `pytest apps/api/tests` を通す（既存のテストは関数名/挙動が変わらない前提で書かれているので、
   ラッパーのシグネチャが一致していれば無改修で通るはず）。
2. `novel` 側も同様に置き換える（`app/rag.py`・`app/backup.py`・`app/qdrant_service.py`・
   `app/file_sync.py` のコレクション名/ストレージ設定/コミットメッセージだけがアプリ固有）。
3. どちらのアプリでも重複コードが消えたことを確認し、
   `editor_common` に変更が必要になった場合は
   このリポジトリ側で修正してからバージョンタグを打ち、
   両アプリの依存バージョンを上げる。
