"""Optional persistence for optimization history.

The database is deliberately optional. A platform often starts the web service
before the database is reachable, or the database is simply not provisioned on
a first deploy, and an app that raises on import in that situation never starts
at all: the platform sees the container exit, restarts it, watches it exit
again, and the deploy fails with a crash loop whose actual cause is one
connection refused.

So `init_db()` reports whether it worked rather than raising, every write is
best effort, and `status()` tells `/health` what the real state is. The service
answers requests either way; it just cannot show you history.
"""

import datetime
import threading

import config

_state = {"available": False, "reason": "not initialised", "engine": None}
_lock = threading.Lock()

try:
    from sqlalchemy import (Column, DateTime, Integer, String, Text,
                            create_engine, select)
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session, declarative_base
    SQLALCHEMY_AVAILABLE = True
except ImportError:  # pragma: no cover
    SQLALCHEMY_AVAILABLE = False
    SQLAlchemyError = Exception
    declarative_base = None

if SQLALCHEMY_AVAILABLE:
    Base = declarative_base()

    class Optimization(Base):
        __tablename__ = "optimizations"

        id = Column(Integer, primary_key=True)
        created_at = Column(DateTime, default=datetime.datetime.utcnow)
        style = Column(String(32))
        source = Column(String(16))
        original = Column(Text)
        optimized = Column(Text)
else:  # pragma: no cover
    Base = None
    Optimization = None


def init_db(database_url=None):
    """Try to connect and create tables. Never raises.

    Returns True when the database is usable. A False return is a normal
    outcome, not an error: the caller carries on without persistence.
    """
    url = database_url if database_url is not None else config.DATABASE_URL

    with _lock:
        if not SQLALCHEMY_AVAILABLE:
            _state.update(available=False, reason="sqlalchemy not installed",
                          engine=None)
            return False
        if not url:
            _state.update(available=False, reason="DATABASE_URL is not set",
                          engine=None)
            return False
        try:
            engine = create_engine(url, pool_pre_ping=True, future=True)
            Base.metadata.create_all(engine)
        except SQLAlchemyError as problem:
            _state.update(available=False,
                          reason=_short(problem), engine=None)
            return False
        except Exception as problem:
            # A bad URL scheme raises ArgumentError, a missing driver raises
            # ModuleNotFoundError. Neither should take the service down.
            _state.update(available=False, reason=_short(problem), engine=None)
            return False
        _state.update(available=True, reason="connected", engine=engine)
        return True


def _short(problem):
    text = str(problem).replace("\n", " ")
    return text[:120]


def status():
    """What /health reports about the database."""
    with _lock:
        if _state["available"]:
            return "connected"
        return "unavailable (%s)" % _state["reason"]


def is_available():
    with _lock:
        return bool(_state["available"])


def record(original, optimized, style, source):
    """Persist one optimization. Best effort, never raises.

    Returns True if it was written. A failed write must not turn a successful
    optimization into a 500: the caller already has their answer.
    """
    with _lock:
        engine = _state["engine"] if _state["available"] else None
    if engine is None:
        return False
    try:
        with Session(engine) as session:
            session.add(Optimization(original=original, optimized=optimized,
                                     style=style, source=source))
            session.commit()
        return True
    except SQLAlchemyError as problem:
        with _lock:
            _state.update(available=False, reason=_short(problem))
        return False


def history(limit=20):
    """Recent optimizations, newest first. Empty list if unavailable."""
    with _lock:
        engine = _state["engine"] if _state["available"] else None
    if engine is None:
        return []
    try:
        with Session(engine) as session:
            rows = session.execute(
                select(Optimization)
                .order_by(Optimization.id.desc())
                .limit(limit)).scalars().all()
            return [
                {
                    "id": row.id,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "style": row.style,
                    "source": row.source,
                    "original": row.original,
                    "optimized": row.optimized,
                }
                for row in rows
            ]
    except SQLAlchemyError:
        return []


def reset_for_tests():
    with _lock:
        _state.update(available=False, reason="not initialised", engine=None)
