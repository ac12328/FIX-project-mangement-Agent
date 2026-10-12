"""
FastAPI dependency injection — scoped SQLite connections.

IMPORTANT: get_conn() now uses scoped_read() to prevent the API server from
holding the SQLite write lock during request processing. This eliminates
cross-process lock contention with the bot's email poll and scheduler jobs.

Endpoints that need to write MUST use get_write_conn() explicitly.
"""

from fastapi import Depends, Request

from src.api.auth import CurrentActor, require_current_actor, validate_csrf
from src.db.connection import scoped_read, scoped_write


def get_read_conn():
    """Dependency: yields a short-lived read-only connection."""
    with scoped_read() as conn:
        yield conn


def get_write_conn():
    """Dependency: yields a short-lived write connection, auto-commits."""
    with scoped_write() as conn:
        yield conn


def get_write_scope():
    """Inject a write-scope factory when work must precede the writer lease."""
    return scoped_write


def get_current_actor(request: Request) -> CurrentActor:
    """Return the actor established by auth middleware or validate directly."""
    actor = getattr(request.state, "actor", None)
    return actor if actor is not None else require_current_actor(request)


def get_write_actor(
    request: Request,
    actor: CurrentActor = Depends(get_current_actor),
) -> CurrentActor:
    """Return the current actor after CSRF validation for a write request."""
    return validate_csrf(request, actor)


def get_conn():
    """Read-only connection for backward compatibility.

    Previously used scoped_deferred() (= scoped_write()), causing every API
    request to grab the SQLite write lock via BEGIN IMMEDIATE — even GET
    endpoints. This starved the bot process of write access for 30s+ at a time.

    Now uses scoped_read(). Routers that need writes must switch to
    get_write_conn().
    """
    with scoped_read() as conn:
        yield conn
