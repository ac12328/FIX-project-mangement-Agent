"""Dashboard local, Entra, emergency, logout, and session endpoints."""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, model_validator

from config.settings import CONFIG, MIN_DASHBOARD_PASSWORD_LENGTH
from src.api.auth import (
    CurrentActor,
    auth_enabled,
    clear_session_cookies,
    require_current_actor,
    set_session_cookies,
    verify_password,
)
from src.auth.emergency import authenticate_emergency
from src.auth.local import (
    ActionDelivery,
    authenticate_local,
    register_local_user,
    request_password_reset,
    resend_verification,
    reset_password,
    verify_email_token,
)
from src.auth.mailer import send_auth_action_email
from src.auth.oidc import OIDC_TRANSACTION_COOKIE, complete_login, start_login
from src.auth.repository import bootstrap_primary_admin, record_security_event
from src.auth.sessions import create_session, list_sessions, revoke_all_sessions, revoke_session
from src.db.connection import scoped_read, scoped_write

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

_GENERIC_REGISTER_MESSAGE = (
    "If this email can be registered, a verification link has been sent."
)
_GENERIC_RESET_MESSAGE = (
    "If an active account exists for this email, a password reset link has been sent."
)
_GENERIC_RESEND_MESSAGE = (
    "If this account is waiting for verification, a new link has been sent."
)


class EmergencyLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1000)


class LoginRequest(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    username: str | None = Field(default=None, max_length=254)
    password: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def require_identifier(self) -> "LoginRequest":
        if not (self.email or self.username):
            raise ValueError("email is required")
        return self

    @property
    def identifier(self) -> str:
        return (self.email or self.username or "").strip()


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1000)


class EmailActionRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class TokenActionRequest(BaseModel):
    token: str = Field(min_length=20, max_length=500)


class PasswordResetRequest(TokenActionRequest):
    password: str = Field(min_length=1, max_length=1000)


@router.get("/auth/config")
def auth_config() -> dict:
    return {
        "auth_enabled": auth_enabled(),
        "mode": CONFIG.dashboard_auth.mode,
        "local_registration_enabled": bool(CONFIG.dashboard_auth.local_registration_enabled),
        "allowed_email_domains": sorted(
            domain.strip().casefold().lstrip("@")
            for domain in CONFIG.dashboard_auth.allowed_email_domains
            if domain.strip()
        ),
        "password_min_length": max(
            MIN_DASHBOARD_PASSWORD_LENGTH, CONFIG.dashboard_auth.password_min_length
        ),
    }


@router.get("/auth/entra/login")
def entra_login(
    request: Request,
    redirect_to: str = Query("/", max_length=500),
) -> Response:
    _require_entra_mode()
    if not auth_enabled():
        return RedirectResponse(_dashboard_redirect(redirect_to), status_code=302)
    try:
        with scoped_write() as conn:
            started = start_login(
                conn,
                redirect_to=redirect_to,
                ip_address=_client_ip(request),
            )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(503, str(exc)) from exc
    response = RedirectResponse(started.authorization_url, status_code=302)
    response.set_cookie(
        OIDC_TRANSACTION_COOKIE,
        started.transaction_cookie,
        max_age=started.max_age,
        path="/api/auth/entra/callback",
        secure=bool(CONFIG.dashboard_auth.cookie_secure),
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/entra/callback")
def entra_callback(request: Request) -> Response:
    _require_entra_mode()
    try:
        with scoped_write() as conn:
            user, redirect_to = complete_login(
                conn,
                auth_response=dict(request.query_params),
                transaction_cookie=request.cookies.get(OIDC_TRANSACTION_COOKIE),
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
            material = create_session(
                conn,
                user_id=user["user_id"],
                auth_method="entra",
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    response = RedirectResponse(_dashboard_redirect(redirect_to), status_code=302)
    set_session_cookies(response, material)
    response.delete_cookie(
        OIDC_TRANSACTION_COOKIE,
        path="/api/auth/entra/callback",
        secure=bool(CONFIG.dashboard_auth.cookie_secure),
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/register")
def register(body: RegisterRequest, request: Request) -> dict:
    _require_local_mode()
    delivery: ActionDelivery | None = None
    try:
        with scoped_write() as conn:
            delivery = register_local_user(
                conn,
                email=body.email,
                password=body.password,
                display_name=body.display_name,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _deliver_auth_email(delivery)
    return {"accepted": True, "message": _GENERIC_REGISTER_MESSAGE}


@router.post("/auth/verify-email")
def verify_email(body: TokenActionRequest) -> dict:
    _require_local_mode()
    try:
        with scoped_write() as conn:
            user = verify_email_token(conn, body.token)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "verified": True,
        "email": user.get("email"),
        "message": "Email verified. You can now sign in.",
    }


@router.post("/auth/resend-verification")
def resend_verification_email(body: EmailActionRequest, request: Request) -> dict:
    _require_local_mode()
    with scoped_write() as conn:
        delivery = resend_verification(
            conn,
            email=body.email,
            ip_address=_client_ip(request),
        )
    _deliver_auth_email(delivery)
    return {"accepted": True, "message": _GENERIC_RESEND_MESSAGE}


@router.post("/auth/forgot-password")
def forgot_password(body: EmailActionRequest, request: Request) -> dict:
    _require_local_mode()
    with scoped_write() as conn:
        delivery = request_password_reset(
            conn,
            email=body.email,
            ip_address=_client_ip(request),
        )
    _deliver_auth_email(delivery)
    return {"accepted": True, "message": _GENERIC_RESET_MESSAGE}


@router.post("/auth/reset-password")
def complete_password_reset(body: PasswordResetRequest) -> dict:
    _require_local_mode()
    try:
        with scoped_write() as conn:
            reset_password(conn, token=body.token, password=body.password)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"reset": True, "message": "Password updated. Sign in with your new password."}


@router.post("/auth/emergency/login")
def emergency_login(body: EmergencyLoginRequest, request: Request, response: Response) -> dict:
    if not auth_enabled():
        return {"authenticated": True, "auth_enabled": False}
    if _legacy_credentials_match(body.username, body.password):
        return _complete_legacy_login(request, response, entry_point="emergency-login")
    try:
        with scoped_write() as conn:
            user = authenticate_emergency(
                conn,
                username=body.username,
                password=body.password,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
            material = create_session(
                conn,
                user_id=user["user_id"],
                auth_method="emergency",
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
    except PermissionError as exc:
        message = str(exc)
        status = 429 if "rate limited" in message else 401
        raise HTTPException(status, message) from exc
    set_session_cookies(response, material)
    return _session_payload(
        CurrentActor(
            user_id=user["user_id"],
            email=user.get("email"),
            display_name=user["display_name"],
            role=user["role"],
            status=user["status"],
            auth_method="emergency",
            session_id=material.session_id,
        )
    )


@router.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict:
    if not auth_enabled():
        return {"authenticated": True, "auth_enabled": False}
    if _legacy_credentials_match(body.identifier, body.password):
        return _complete_legacy_login(request, response, entry_point="legacy-login")
    _require_local_mode()
    try:
        with scoped_write() as conn:
            user = authenticate_local(
                conn,
                email=body.identifier,
                password=body.password,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
            material = create_session(
                conn,
                user_id=user["user_id"],
                auth_method="local",
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
    except PermissionError as exc:
        message = str(exc)
        if "rate limited" in message:
            status = 429
        elif "Verify" in message or "disabled" in message:
            status = 403
        else:
            status = 401
        raise HTTPException(status, message) from exc
    set_session_cookies(response, material)
    return _session_payload(
        CurrentActor(
            user_id=user["user_id"],
            email=user.get("email"),
            display_name=user["display_name"],
            role=user["role"],
            status=user["status"],
            auth_method="local",
            session_id=material.session_id,
        )
    )


def _legacy_credentials_match(username: str, password: str) -> bool:
    if not CONFIG.dashboard_auth.legacy_password_login_enabled:
        return False
    username_ok = hmac.compare_digest(username, CONFIG.dashboard_auth.username)
    password_ok = verify_password(password, CONFIG.dashboard_auth.password_hash)
    return username_ok and password_ok


def _complete_legacy_login(
    request: Request,
    response: Response,
    *,
    entry_point: str,
) -> dict:
    with scoped_write() as conn:
        user = bootstrap_primary_admin(conn)
        material = create_session(
            conn,
            user_id=user["user_id"],
            auth_method="legacy",
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        record_security_event(
            conn,
            "legacy.login_succeeded",
            severity="warning",
            actor_user_id=user["user_id"],
            subject_user_id=user["user_id"],
            session_id=material.session_id,
            metadata={"entry_point": entry_point},
        )
    set_session_cookies(response, material)
    return _session_payload(
        CurrentActor(
            user_id=user["user_id"],
            email=user.get("email"),
            display_name=user["display_name"],
            role=user["role"],
            status=user["status"],
            auth_method="legacy",
            session_id=material.session_id,
        )
    )


@router.get("/auth/session")
def session(actor: CurrentActor = Depends(require_current_actor)) -> dict:
    return _session_payload(actor)


@router.post("/auth/logout")
def logout(response: Response, actor: CurrentActor = Depends(require_current_actor)) -> dict:
    if auth_enabled() and actor.session_id:
        with scoped_write() as conn:
            revoke_session(conn, actor.session_id, actor_user_id=actor.user_id)
    clear_session_cookies(response)
    return {"authenticated": False}


@router.post("/auth/logout-all")
def logout_all(response: Response, actor: CurrentActor = Depends(require_current_actor)) -> dict:
    revoked = 0
    if auth_enabled():
        with scoped_write() as conn:
            revoked = revoke_all_sessions(
                conn,
                user_id=actor.user_id,
                actor_user_id=actor.user_id,
            )
    clear_session_cookies(response)
    return {"authenticated": False, "revoked_sessions": revoked}


@router.get("/auth/sessions")
def my_sessions(actor: CurrentActor = Depends(require_current_actor)) -> dict:
    if not auth_enabled():
        return {"sessions": []}
    with scoped_read() as conn:
        rows = list_sessions(
            conn,
            user_id=actor.user_id,
            current_session_id=actor.session_id,
        )
    return {"sessions": rows}


@router.delete("/auth/sessions/{session_id}")
def delete_my_session(
    session_id: str,
    response: Response,
    actor: CurrentActor = Depends(require_current_actor),
) -> dict:
    with scoped_write() as conn:
        try:
            found = revoke_session(conn, session_id, actor_user_id=actor.user_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
    if not found:
        raise HTTPException(404, "Session not found")
    if session_id == actor.session_id:
        clear_session_cookies(response)
    return {"revoked": True, "session_id": session_id}


def _session_payload(actor: CurrentActor) -> dict:
    return {
        "authenticated": True,
        "auth_enabled": auth_enabled(),
        "user_id": actor.user_id,
        "operator_id": actor.operator_id,
        "email": actor.email,
        "display_name": actor.display_name,
        "username": actor.username,
        "role": actor.role,
        "status": actor.status,
        "auth_method": actor.auth_method,
        "session_id": actor.session_id,
        "permissions": sorted(actor.permissions),
    }


def _deliver_auth_email(delivery: ActionDelivery | None) -> None:
    if not delivery:
        return
    try:
        send_auth_action_email(delivery)
    except Exception:
        # Public responses stay generic and action tokens remain valid for retry.
        logger.exception("Authentication email delivery raised an unexpected error")


def _require_local_mode() -> None:
    if CONFIG.dashboard_auth.mode != "local":
        raise HTTPException(404, "Local Dashboard authentication is disabled")


def _require_entra_mode() -> None:
    if CONFIG.dashboard_auth.mode != "entra":
        raise HTTPException(404, "Microsoft Entra authentication is disabled")


def _client_ip(request: Request) -> str:
    # Never trust forwarding headers at the application layer. Uvicorn may
    # rewrite request.client for explicitly trusted proxies before FastAPI
    # receives the request; direct clients cannot spoof that peer address.
    return request.client.host if request.client else "unknown"


def _dashboard_redirect(path: str) -> str:
    target = path if path.startswith("/") and not path.startswith("//") else "/"
    base = CONFIG.dashboard_base_url.rstrip("/")
    return f"{base}{target}" if base else target
