"""Engine and session helpers.

Why the transaction in orchestrator.py is safe: SQLite allows one writer at a time, and a transaction takes
the write lock at its FIRST WRITE. The decision transaction starts with a write (the `rules started` event)
and only then reads the PO balance, so nobody else can approve against the same PO between our read and our
commit. A second run's decision transaction waits (up to busy_timeout) until the first commits or rolls back.
(In production on Postgres this would be SELECT ... FOR UPDATE on the PO row.)
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base


def make_engine(url: str, busy_timeout: float = 5.0):
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": busy_timeout})

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None          # sqlite3 stops guessing; we issue BEGIN ourselves
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "begin")
    def _on_begin(connection):
        connection.exec_driver_sql("BEGIN")

    return engine


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
