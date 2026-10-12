"""Encryption helpers for sensitive trace payloads."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from typing import Any

_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token|cookie)\b[\"']?"
    r"\s*([:=])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_REASONING_KEY_RE = re.compile(
    r"(?i)^(reasoning(?:_content)?|thinking(?:_content|_blocks)?|"
    r"chain[_-]?of[_-]?thought|cot|hidden[_-]?reasoning)$"
)
_THINK_BLOCK_RE = re.compile(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>")
_CREDENTIAL_KEY_NAMES = frozenset({
    "api_key",
    "api_token",
    "access_token",
    "auth_token",
    "authorization",
    "authorization_header",
    "bearer_token",
    "bot_token",
    "client_secret",
    "code_verifier",
    "cookie",
    "cookies",
    "csrf_token",
    "id_token",
    "password",
    "private_key",
    "proxy_authorization",
    "refresh_token",
    "secret",
    "session_token",
    "set_cookie",
    "token",
})
_CREDENTIAL_KEY_SUFFIXES = (
    "_api_key",
    "_api_token",
    "_access_token",
    "_auth_token",
    "_authorization",
    "_authorization_header",
    "_bearer_token",
    "_bot_token",
    "_client_secret",
    "_code_verifier",
    "_cookie",
    "_csrf_token",
    "_id_token",
    "_password",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_session_token",
)


def _is_credential_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
    return normalized in _CREDENTIAL_KEY_NAMES or normalized.endswith(
        _CREDENTIAL_KEY_SUFFIXES
    )


def filter_secrets(value: Any) -> Any:
    """Remove credentials and hidden model reasoning from trace payloads."""
    if isinstance(value, dict):
        filtered: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _REASONING_KEY_RE.search(key_text):
                filtered[key] = "[REDACTED_REASONING]"
            elif _is_credential_key(key_text):
                filtered[key] = "[REDACTED]"
            else:
                filtered[key] = filter_secrets(item)
        return filtered
    if isinstance(value, list):
        return [filter_secrets(v) for v in value]
    if isinstance(value, tuple):
        return [filter_secrets(v) for v in value]
    if isinstance(value, str):
        without_thinking = _THINK_BLOCK_RE.sub("[REDACTED_REASONING]", value)
        without_bearer = _BEARER_RE.sub("Bearer [REDACTED]", without_thinking)
        return _SECRET_RE.sub(
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
            without_bearer,
        )
    return value


def _key() -> bytes:
    raw = os.environ.get("AGENTFIX_DEMO_TRACE_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise ValueError("AGENTFIX_DEMO_TRACE_ENCRYPTION_KEY is required for full trace payload capture")
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:
        raise ValueError("AGENTFIX_DEMO_TRACE_ENCRYPTION_KEY must be URL-safe base64") from exc
    if len(key) != 32:
        raise ValueError("AGENTFIX_DEMO_TRACE_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def validate_encryption_key() -> None:
    _key()


def encrypt_payload(payload: Any) -> dict[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    clean = filter_secrets(payload)
    plaintext = json.dumps(clean, ensure_ascii=False, default=str).encode("utf-8")
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key()).encrypt(nonce, plaintext, b"agentfix-trace-v1")
    return {
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "key_version": "v1",
        "payload_hash": hashlib.sha256(plaintext).hexdigest(),
    }


def decrypt_payload(ciphertext: str, nonce: str, key_version: str = "v1") -> Any:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if key_version != "v1":
        raise ValueError(f"Unsupported trace key version: {key_version}")
    plaintext = AESGCM(_key()).decrypt(base64.b64decode(nonce), base64.b64decode(ciphertext), b"agentfix-trace-v1")
    return json.loads(plaintext.decode("utf-8"))
