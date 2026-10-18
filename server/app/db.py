"""Database engine and session helpers (SQLAlchemy 2, psycopg 3)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine (tests switch databases)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def new_session() -> Session:
    engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, committed by the handler."""
    db = new_session()
    try:
        yield db
    finally:
        db.close()


def end_request_session(db: Session) -> None:
    """Before a long response (server-sent events): end the request session's read-only transaction and give its
    connection back to the pool (``close`` rolls it back; loaded objects stay readable, detached). FastAPI closes a
    yield dependency only after the whole response was sent, so a stream would otherwise hold a connection idle in
    transaction (and its locks) for as long as it is open."""
    db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit on success, roll back on error (workers, scripts)."""
    db = new_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
