from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config.settings import CONFIG
if TYPE_CHECKING:
    import msal

from src.auth.repository import (
    bind_entra_identity,
    db_timestamp,
    record_security_event,
    utc_now,
)

OIDC_TRANSACTION_COOKIE = "agentfixb_oidc"
OIDC_TRANSACTION_TTL_SECONDS = 600


@dataclass(frozen=True)
class OidcLoginStart:
    authorization_url: str
    transaction_cookie: str
    max_age: int = OIDC_TRANSACTION_TTL_SECONDS


def build_msal_application() -> msal.ConfidentialClientApplication:
    # Contract/schema imports must not initialize provider networking modules.
    import msal

    auth = CONFIG.dashboard_auth
    return msal.ConfidentialClientApplication(
        auth.client_id,
        authority=f"https://login.microsoftonline.com/{auth.tenant_id}",
        client_credential=auth.client_secret,
        exclude_scopes=["offline_access"],
    )


def start_login(
    conn: sqlite3.Connection,
    *,
    redirect_to: str = "/",
    ip_address: str | None = None,
    application: Any | None = None,
) -> OidcLoginStart:
    if not CONFIG.dashboard_auth.enabled:
        raise RuntimeError("Dashboard authentication is disabled")
    redirect_to = _safe_redirect(redirect_to)
    state = secrets.token_urlsafe(32)
    client = application or build_msal_application()
    flow = client.initiate_auth_code_flow(
        # MSAL reserves and automatically adds openid/profile. Requesting email
        # here, with offline_access excluded on the client, yields exactly the
        # required openid/profile/email set without a delegated Graph token.
        scopes=["email"],
        redirect_uri=CONFIG.dashboard_auth.redirect_uri,
        state=state,
    )
    required = ("auth_uri", "state", "nonce", "code_verifier")
    if any(not flow.get(key) for key in required):
        raise RuntimeError("MSAL did not return a complete authorization flow")

    transaction_id = str(uuid.uuid4())
    now = utc_now()
    conn.execute(
        """
        INSERT INTO auth_oidc_transactions(
            transaction_id,state_hash,nonce_hash,pkce_verifier_hash,redirect_to,
            ip_address,created_at,expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transaction_id,
            _hash(flow["state"]),
            _hash(flow["nonce"]),
            _hash(flow["code_verifier"]),
            redirect_to,
            ip_address,
            db_timestamp(now),
            db_timestamp(now + timedelta(seconds=OIDC_TRANSACTION_TTL_SECONDS)),
        ),
    )
    cookie = _encrypt_transaction({"transaction_id": transaction_id, "flow": flow})
    return OidcLoginStart(flow["auth_uri"], cookie)


def complete_login(
    conn: sqlite3.Connection,
    *,
    auth_response: Mapping[str, str],
    transaction_cookie: str | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    application: Any | None = None,
) -> tuple[dict[str, Any], str]:
    transaction = _decrypt_transaction(transaction_cookie)
    transaction_id = str(transaction.get("transaction_id") or "")
    flow = transaction.get("flow")
    if not transaction_id or not isinstance(flow, dict):
        raise ValueError("Invalid OIDC transaction cookie")

    row = conn.execute(
        "SELECT * FROM auth_oidc_transactions WHERE transaction_id=?",
        (transaction_id,),
    ).fetchone()
    if not row:
        raise ValueError("Unknown OIDC transaction")
    if row["used_at"]:
        raise ValueError("OIDC transaction has already been used")
    if row["expires_at"] <= db_timestamp():
        raise ValueError("OIDC transaction has expired")
    if not secrets.compare_digest(row["state_hash"], _hash(str(flow.get("state") or ""))):
        raise ValueError("OIDC state validation failed")
    if not secrets.compare_digest(row["nonce_hash"], _hash(str(flow.get("nonce") or ""))):
        raise ValueError("OIDC nonce validation failed")
    if not secrets.compare_digest(
        row["pkce_verifier_hash"], _hash(str(flow.get("code_verifier") or ""))
    ):
        raise ValueError("OIDC PKCE validation failed")
    if not secrets.compare_digest(str(auth_response.get("state") or ""), str(flow["state"])):
        raise ValueError("OIDC response state validation failed")

    conn.execute(
        "UPDATE auth_oidc_transactions SET used_at=datetime('now') WHERE transaction_id=?",
        (transaction_id,),
    )
    client = application or build_msal_application()
    try:
        result = client.acquire_token_by_auth_code_flow(flow, dict(auth_response))
    except ValueError as exc:
        record_security_event(
            conn,
            "entra.login_failed",
            severity="warning",
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={"reason": "state_or_nonce_validation"},
        )
        raise ValueError("Microsoft sign-in validation failed") from exc

    if result.get("error"):
        record_security_event(
            conn,
            "entra.login_failed",
            severity="warning",
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={"reason": str(result.get("error"))},
        )
        raise ValueError("Microsoft sign-in failed")

    claims = result.get("id_token_claims") or {}
    _validate_claims(claims)
    email = claims.get("email") or claims.get("preferred_username")
    display_name = claims.get("name") or email
    user = bind_entra_identity(
        conn,
        tenant_id=str(claims["tid"]),
        object_id=str(claims["oid"]),
        email=str(email) if email else None,
        display_name=str(display_name) if display_name else None,
    )
    if user.get("status") != "active":
        record_security_event(
            conn,
            "entra.disabled_user_blocked",
            severity="warning",
            subject_user_id=user.get("user_id"),
            ip_address=ip_address,
            user_agent=user_agent,
        )
        raise PermissionError("User account is disabled")
    record_security_event(
        conn,
        "entra.login_succeeded",
        actor_user_id=user["user_id"],
        subject_user_id=user["user_id"],
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return user, str(row["redirect_to"])


def _validate_claims(claims: Mapping[str, Any]) -> None:
    auth = CONFIG.dashboard_auth
    if str(claims.get("tid") or "") != auth.tenant_id:
        raise PermissionError("Microsoft tenant is not allowed")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if auth.client_id not in {str(item) for item in audiences if item}:
        raise PermissionError("Microsoft token audience is not allowed")
    if not claims.get("oid"):
        raise PermissionError("Microsoft token is missing object identity")
    roles = claims.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    if auth.app_role not in roles:
        raise PermissionError("Required Dashboard app role is missing")


def _safe_redirect(value: str | None) -> str:
    target = (value or "/").strip()
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encryption_key() -> bytes:
    secret = CONFIG.dashboard_auth.oidc_transaction_secret
    if len(secret) < 32:
        raise RuntimeError("dashboard_auth.oidc_transaction_secret must be at least 32 characters")
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _encrypt_transaction(payload: dict[str, Any]) -> str:
    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(_encryption_key()).encrypt(nonce, plaintext, None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")


def _decrypt_transaction(value: str | None) -> dict[str, Any]:
    if not value:
        raise ValueError("Missing OIDC transaction cookie")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded)
        plaintext = AESGCM(_encryption_key()).decrypt(raw[:12], raw[12:], None)
        payload = json.loads(plaintext)
    except Exception as exc:
        raise ValueError("Invalid OIDC transaction cookie") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid OIDC transaction cookie")
    return payload
