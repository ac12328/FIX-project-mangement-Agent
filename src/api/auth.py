"""Dashboard authentication compatibility facade and request dependencies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable

from fastapi import HTTPException, Request, Response

from config.settings import CONFIG
from src.auth.rbac import ROLE_ADMIN, permissions_for_role
from src.auth.sessions import (
    SessionMaterial,
    authenticate_session,
    validate_csrf_tokens,
)

SESSION_COOKIE = "agentfixb_session"
CSRF_COOKIE = "agentfixb_csrf"
CSRF_HEADER = "x-csrf-token"


@dataclass(init=False)
class CurrentActor:
    user_id: str
    email: str | None
    display_name: str
    role: str
    status: str
    auth_method: str
    session_id: str | None
    permissions: frozenset[str]
    surface: str

    def __init__(
        self,
        user_id: str | None = None,
        email: str | None = None,
        display_name: str | None = None,
        role: str = ROLE_ADMIN,
        status: str = "active",
        auth_method: str = "disabled",
        session_id: str | None = None,
        permissions: Iterable[str] | None = None,
        surface: str = "dashboard",
        *,
        operator_id: str | None = None,
        username: str | None = None,
    ) -> None:
        resolved_user_id = user_id or operator_id or CONFIG.operator_id or "default_operator"
        resolved_name = display_name or username or email or resolved_user_id
        self.user_id = resolved_user_id
        self.email = email
        self.display_name = resolved_name
        self.role = role
        self.status = status
        self.auth_method = auth_method
        self.session_id = session_id
        self.permissions = frozenset(permissions if permissions is not None else permissions_for_role(role))
        self.surface = surface

    @property
    def operator_id(self) -> str:
        """Compatibility alias for legacy actor attribution."""
        return self.user_id

    @property
    def username(self) -> str:
        """Compatibility alias used by older Dashboard routes and tests."""
        return self.email or self.display_name


def actor_from_session(session: dict) -> CurrentActor:
    return CurrentActor(
        user_id=str(session["user_id"]),
        email=session.get("email"),
        display_name=str(session.get("display_name") or session.get("email") or session["user_id"]),
        role=str(session["role"]),
        status=str(session["status"]),
        auth_method=str(session["auth_method"]),
        session_id=str(session["session_id"]),
    )


def hash_password(password: str, *, iterations: int = 600_000) -> str:
    """Create a PBKDF2 hash for the explicitly opt-in legacy login path."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _decode(raw_salt), int(raw_iterations)
        )
        return hmac.compare_digest(actual, _decode(raw_digest))
    except (TypeError, ValueError):
        return False


def auth_enabled() -> bool:
    return bool(CONFIG.dashboard_auth.enabled)


def set_session_cookies(response: Response, material: SessionMaterial) -> None:
    max_age = max(1, CONFIG.dashboard_auth.absolute_session_hours) * 60 * 60
    common = {
        "secure": bool(CONFIG.dashboard_auth.cookie_secure),
        "samesite": "lax",
        "path": "/",
        "max_age": max_age,
    }
    response.set_cookie(SESSION_COOKIE, material.session_token, httponly=True, **common)
    response.set_cookie(CSRF_COOKIE, material.csrf_token, httponly=False, **common)


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=bool(CONFIG.dashboard_auth.cookie_secure),
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE,
        path="/",
        secure=bool(CONFIG.dashboard_auth.cookie_secure),
        httponly=False,
        samesite="lax",
    )


def require_current_actor(request: Request) -> CurrentActor:
    """Resolve the current DB-backed session and live user role/status."""
    if not auth_enabled():
        return CurrentActor(
            user_id=CONFIG.operator_id or "default_operator",
            email=None,
            display_name=CONFIG.dashboard_auth.username or "Operator",
            role=ROLE_ADMIN,
            auth_method="disabled",
        )

    from src.auth.session_touch import enqueue_session_touch
    from src.db.connection import scoped_observability_write, scoped_read

    token = request.cookies.get(SESSION_COOKIE)
    try:
        with scoped_read() as conn:
            session = authenticate_session(conn, token, touch=False)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        from src.auth.repository import bootstrap_primary_admin
        from src.auth.schema import ensure_auth_schema

        with scoped_observability_write(timeout=0.05) as conn:
            ensure_auth_schema(conn)
            bootstrap_primary_admin(conn)
        with scoped_read() as conn:
            session = authenticate_session(conn, token, touch=False)
    if session:
        enqueue_session_touch(str(session.get("session_id") or ""))
        return actor_from_session(session)

    if CONFIG.dashboard_auth.legacy_password_login_enabled:
        payload = parse_session_token(token)
        if payload:
            return CurrentActor(
                user_id=str(payload.get("operator_id") or CONFIG.operator_id),
                display_name=str(payload.get("sub") or CONFIG.dashboard_auth.username),
                role=ROLE_ADMIN,
                auth_method="legacy",
                session_id=None,
            )
    raise HTTPException(401, "Authentication required")


def validate_csrf(request: Request, actor: CurrentActor) -> CurrentActor:
    if not auth_enabled() or request.method in {"GET", "HEAD", "OPTIONS"}:
        return actor
    supplied = request.headers.get(CSRF_HEADER)
    cookie_token = request.cookies.get(CSRF_COOKIE)
    if actor.auth_method == "legacy" and not actor.session_id:
        payload = parse_session_token(request.cookies.get(SESSION_COOKIE))
        expected = str(payload.get("csrf") or "") if payload else ""
        valid = bool(
            supplied
            and cookie_token
            and expected
            and hmac.compare_digest(supplied, cookie_token)
            and hmac.compare_digest(supplied, expected)
        )
    else:
        from src.db.connection import scoped_read

        with scoped_read() as conn:
            valid = validate_csrf_tokens(
                conn,
                session_id=actor.session_id,
                supplied_token=supplied,
                cookie_token=cookie_token,
            )
    if not valid:
        raise HTTPException(403, "CSRF validation failed")
    return actor


# Legacy signed-token helpers remain only for explicit migration compatibility.
def create_session_token(username: str, csrf_token: str) -> str:
    now = int(time.time())
    payload = {
        "sub": username,
        "operator_id": CONFIG.operator_id or "default_operator",
        "csrf": csrf_token,
        "iat": now,
        "exp": now + max(1, CONFIG.dashboard_auth.session_ttl_minutes) * 60,
    }
    encoded = _encode_json(payload)
    return f"{encoded}.{_sign(encoded)}"


def parse_session_token(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    encoded, supplied_signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(encoded), supplied_signature):
        return None
    try:
        payload = json.loads(_decode(encoded))
        if int(payload.get("exp") or 0) <= int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _legacy_secret() -> bytes:
    secret = CONFIG.dashboard_auth.session_secret
    if CONFIG.dashboard_auth.legacy_password_login_enabled and len(secret) < 32:
        raise RuntimeError("dashboard_auth.session_secret must be at least 32 characters")
    return (secret or "development-auth-disabled-secret").encode()


def _sign(value: str) -> str:
    digest = hmac.new(_legacy_secret(), value.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _encode_json(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
