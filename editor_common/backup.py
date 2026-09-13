"""Scheduled + on-demand backups of PostgreSQL (pg_dump) and the Qdrant
vector store (a collection snapshot). Shared logic from both editors;
each app supplies its own settings (paths, DB URL, Qdrant collection name,
live Qdrant URL resolver) at construction time.

Off by default so no existing deployment suddenly starts writing to disk
on a timer just from upgrading — that's the caller's `backup_enabled` flag.
"""
import asyncio
import logging
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

PG_DUMP_TIMEOUT_SECONDS = 300
QDRANT_TIMEOUT_SECONDS = 60


class BackupManager:
    def __init__(
        self,
        *,
        backup_dir: str,
        database_url: str,
        get_qdrant_url: Callable[[], str],
        collection: str,
        retention_count: int = 14,
        default_db_name: str = "postgres",
    ):
        self.backup_dir = backup_dir
        self.database_url = database_url
        self._get_qdrant_url = get_qdrant_url
        self.collection = collection
        self.retention_count = retention_count
        self.default_db_name = default_db_name

    def _backup_root(self) -> Path:
        p = Path(self.backup_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _dump_postgres(self, out_path: Path) -> tuple[bool, str]:
        url = make_url(self.database_url)
        args = [
            'pg_dump', '-h', url.host or 'localhost', '-p', str(url.port or 5432),
            '-U', url.username or self.default_db_name, '-d', url.database or self.default_db_name,
            '-F', 'c', '-f', str(out_path),
        ]
        env = {'PGPASSWORD': url.password or ''}
        try:
            r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=PG_DUMP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as ex:
            return False, str(ex)
        if r.returncode != 0:
            return False, r.stderr.strip()[:500]
        return True, ''

    def _snapshot_qdrant(self, out_path: Path) -> tuple[bool, str]:
        base = self._get_qdrant_url().rstrip('/')
        try:
            with httpx.Client(timeout=QDRANT_TIMEOUT_SECONDS) as c:
                exists = c.get(f'{base}/collections/{self.collection}')
                if exists.status_code == 404:
                    return False, 'skipped (nothing indexed yet)'
                exists.raise_for_status()
                created = c.post(f'{base}/collections/{self.collection}/snapshots')
                created.raise_for_status()
                name = created.json()['result']['name']
                downloaded = c.get(f'{base}/collections/{self.collection}/snapshots/{name}')
                downloaded.raise_for_status()
                out_path.write_bytes(downloaded.content)
        except Exception as ex:
            return False, str(ex)
        return True, ''

    def _prune_old_backups(self) -> None:
        dirs = sorted((d for d in self._backup_root().iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
        for d in dirs[max(self.retention_count, 0):]:
            shutil.rmtree(d, ignore_errors=True)

    def run_backup(self) -> dict:
        start = time.monotonic()
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        out_dir = self._backup_root() / stamp
        out_dir.mkdir(parents=True, exist_ok=True)

        pg_ok, pg_error = self._dump_postgres(out_dir / 'postgres.dump')
        qdrant_ok, qdrant_error = self._snapshot_qdrant(out_dir / f'qdrant-{self.collection}.snapshot')
        self._prune_old_backups()

        result = {
            'timestamp': stamp,
            'postgres_ok': pg_ok, 'postgres_error': pg_error,
            'qdrant_ok': qdrant_ok, 'qdrant_error': qdrant_error,
            'duration_seconds': round(time.monotonic() - start, 1),
        }
        if pg_ok:
            logger.info('Backup %s: postgres OK, qdrant %s', stamp, 'OK' if qdrant_ok else qdrant_error)
        else:
            logger.error('Backup %s: postgres FAILED (%s)', stamp, pg_error)
        return result

    def list_backups(self) -> list[dict]:
        out = []
        for d in sorted(self._backup_root().iterdir(), reverse=True):
            if not d.is_dir():
                continue
            files = list(d.iterdir())
            out.append({
                'timestamp': d.name,
                'has_postgres': any(f.name == 'postgres.dump' for f in files),
                'has_qdrant': any(f.name.startswith('qdrant-') for f in files),
                'size_bytes': sum(f.stat().st_size for f in files),
            })
        return out

    async def backup_loop(self, *, enabled: bool, interval_seconds: int) -> None:
        if not enabled:
            logger.info('Scheduled backups are disabled (manual backup still works).')
            return
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await asyncio.to_thread(self.run_backup)
            except Exception:
                logger.exception('Unexpected error during scheduled backup')
