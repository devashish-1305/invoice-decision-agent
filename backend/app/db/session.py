"""Engine and session helpers.

Two kinds of engine over the same SQLite file:
  - writer (default): every transaction starts with BEGIN IMMEDIATE, i.e. it takes SQLite's single write lock
    up front and WAITS (up to busy_timeout) if another writer has it. Pipeline runs, the queued-run insert and
    reset use this. It is also what makes "read the PO balance, decide, insert into the ledger" atomic: nobody
    else can write between our read and our commit.
    (A plain deferred transaction that reads first and writes later can be refused instantly with "database is
    locked" when another run holds the lock. That is a lock-upgrade deadlock, and it is why writers use IMMEDIATE.)
  - reader (immediate=False): plain BEGIN. GET endpoints and the SSE poll use this and never take the write lock.
WAL mode lets readers keep reading while a writer works. (In production on Postgres: SELECT ... FOR UPDATE.)
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base


def make_engine(url: str, busy_timeout: float = 5.0, immediate: bool = True):
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": busy_timeout})

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None          # sqlite3 stops guessing; we issue BEGIN ourselves
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    @event.listens_for(engine, "begin")
    def _on_begin(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return engine


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
