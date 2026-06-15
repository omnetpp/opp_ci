from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opp_ci.config import DATABASE_URL

# The engine is created lazily, on first use, rather than at import time.
# Creating it eagerly would import the DB driver (e.g. psycopg2 for a
# postgresql:// URL) as a side effect of merely importing the CLI — which
# breaks commands that never touch the DB (notably `worker start` and
# `worker service install`) on hosts whose role-extras omit that driver.
_engine = None
_SessionLocal = None


def _init():
    global _engine, _SessionLocal
    if _engine is None:
        connect_args = {}
        if DATABASE_URL.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        _engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
        _SessionLocal = sessionmaker(bind=_engine)
    return _engine, _SessionLocal


def get_engine():
    """Return the SQLAlchemy engine, creating it on first call."""
    return _init()[0]


def SessionLocal():
    """Return a new Session. Call-compatible with the old sessionmaker, so
    existing ``SessionLocal()`` call sites are unchanged; the engine/factory
    are initialised lazily on first use."""
    return _init()[1]()


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
