"""Mirrors episode text to local disk and, optionally, auto-syncs it to a
git remote on a timer. Shared logic from both editors; each app supplies
its own storage directory and git-remote settings.

The database stays the source of truth for every read the app does — this
module is a one-way write-through mirror, so a bug here can never corrupt
what readers see. Two independent failure domains:

- Disk mirror: `write_episode_file`/`delete_episode_file` run synchronously
  right after the DB commit for that episode, so the file on disk reflects
  the last successful save immediately (not on some later timer).
- Git sync: a background loop commits whatever changed since last time and
  pushes it, every `git_autosync_interval_seconds`. Only runs when a git
  remote URL is configured. Failures (no network, bad token, remote
  rejected) are logged and retried on the next tick — they never raise
  into a request handler.
"""
import asyncio
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe_slug(s: str, maxlen: int = 60) -> str:
    s = _SLUG_RE.sub('_', (s or '').strip()) or 'untitled'
    return s[:maxlen].strip() or 'untitled'


class FileSyncManager:
    def __init__(
        self,
        *,
        storage_dir: str,
        git_remote_url: str = '',
        git_autosync_interval_seconds: int = 300,
        commit_message: str = 'Auto-save',
        git_author_name: str = 'Auto-Sync',
        git_author_email: str = 'autosync@localhost',
    ):
        self.storage_dir = storage_dir
        self.git_remote_url = git_remote_url
        self.git_autosync_interval_seconds = git_autosync_interval_seconds
        self.commit_message = commit_message
        self.git_author_name = git_author_name
        self.git_author_email = git_author_email

    def storage_root(self) -> Path:
        p = Path(self.storage_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def project_dir(self, project) -> Path:
        d = self.storage_root() / f'{project.id}_{_safe_slug(project.name)}'
        d.mkdir(parents=True, exist_ok=True)
        return d

    def episode_file_path(self, project, episode) -> Path:
        # Filename keys on episode id (stable) rather than title (editable),
        # so renaming a title never orphans a file or requires a rename dance.
        return self.project_dir(project) / f'{episode.number:04d}_{episode.id}.md'

    def write_episode_file(self, project, episode) -> None:
        path = self.episode_file_path(project, episode)
        summary_line = f'> {episode.summary}\n\n' if episode.summary else ''
        body = f'# 第{episode.number}話 {episode.title}\n\n{summary_line}{episode.content or ""}\n'
        try:
            path.write_text(body, encoding='utf-8')
        except OSError:
            logger.exception('Failed to write episode file for episode %s', episode.id)

    def delete_episode_file(self, project, episode) -> None:
        path = self.episode_file_path(project, episode)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception('Failed to delete episode file for episode %s', episode.id)

    def _run_git(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['git', *args], cwd=self.storage_root(), capture_output=True, text=True, timeout=60,
        )

    def _ensure_repo(self) -> None:
        if not (self.storage_root() / '.git').exists():
            self._run_git(['init'])
            self._run_git(['config', 'user.email', self.git_author_email])
            self._run_git(['config', 'user.name', self.git_author_name])
        if self.git_remote_url:
            remote = self._run_git(['remote', 'get-url', 'origin'])
            if remote.returncode != 0:
                self._run_git(['remote', 'add', 'origin', self.git_remote_url])
            elif remote.stdout.strip() != self.git_remote_url:
                self._run_git(['remote', 'set-url', 'origin', self.git_remote_url])

    def sync_once(self) -> bool:
        """Commit any changed files and push. Returns True if a push happened."""
        if not self.git_remote_url:
            return False
        self._ensure_repo()
        status = self._run_git(['status', '--porcelain'])
        if status.stdout.strip():
            self._run_git(['add', '-A'])
            commit = self._run_git(['commit', '-m', self.commit_message])
            if commit.returncode != 0:
                logger.warning('git commit failed during auto-sync: %s', commit.stderr.strip()[:300])
                return False
        push = self._run_git(['push', 'origin', 'HEAD:main'])
        if push.returncode != 0:
            # Never log stderr/stdout verbatim here: git echoes the remote
            # URL (which embeds the access token) into its own error output.
            logger.warning('git push failed during auto-sync (remote unreachable or rejected)')
            return False
        return True

    async def autosync_loop(self) -> None:
        if not self.git_remote_url:
            logger.info('No git remote configured; local-disk mirror is active but auto-sync is disabled.')
            return
        while True:
            await asyncio.sleep(self.git_autosync_interval_seconds)
            try:
                await asyncio.to_thread(self.sync_once)
            except Exception:
                logger.exception('Unexpected error during git auto-sync')
