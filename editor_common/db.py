"""SQLAlchemy engine/session/base factory shared by both editors.

Each app calls `create_db(settings.database_url)` once in its own `db.py`
and re-exports the pieces it needs (its models subclass the returned
`Base`, its FastAPI dependencies use the returned `get_db`).
"""
from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


@dataclass
class Database:
    engine: object
    SessionLocal: sessionmaker
    Base: type

    def get_db(self):
        db = self.SessionLocal()
        try:
            yield db
        finally:
            db.close()


def create_db(database_url: str) -> Database:
    # SQLite only: each pooled connection must stay pinned to the thread
    # that opened it (the sqlite3 module's own rule) unless told otherwise
    # — but SQLAlchemy's default pool for a file/:memory: URL can hand a
    # connection out to a different thread than the one that created it
    # (FastAPI runs sync request handlers in a thread pool), which raises
    # "SQLite objects created in a thread can only be used in that same
    # thread" the moment that happens. Postgres/MySQL connections have no
    # such restriction, so this only ever applies to sqlite:// URLs.
    connect_args = {'check_same_thread': False} if database_url.startswith('sqlite') else {}
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    class Base(DeclarativeBase):
        pass

    return Database(engine=engine, SessionLocal=SessionLocal, Base=Base)
