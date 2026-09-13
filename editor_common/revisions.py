"""Episode revision snapshotting — shared by the episode-save endpoint and
the bulk importer in both editors, so both leave the same undo trail behind
a content change.
"""
from typing import Callable

from sqlalchemy import select

DEFAULT_MAX_REVISIONS_PER_EPISODE = 20


def make_revision_snapshotter(
    revision_model: type,
    max_revisions: int = DEFAULT_MAX_REVISIONS_PER_EPISODE,
) -> Callable:
    """Builds a `snapshot_revision(db, episode)` function bound to the
    caller's own `EpisodeRevision` model (each app defines its own, since
    the column set differs slightly between the two apps)."""

    def snapshot_revision(db, e):
        db.add(revision_model(
            episode_id=e.id, project_id=e.project_id,
            title=e.title, summary=e.summary, content=e.content,
        ))
        db.commit()
        old = db.scalars(
            select(revision_model)
            .where(revision_model.episode_id == e.id)
            .order_by(revision_model.id.desc())
            .offset(max_revisions)
        ).all()
        for o in old:
            db.delete(o)
        if old:
            db.commit()

    return snapshot_revision
