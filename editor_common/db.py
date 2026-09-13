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
    engine = create_engine(database_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    class Base(DeclarativeBase):
        pass

    return Database(engine=engine, SessionLocal=SessionLocal, Base=Base)
