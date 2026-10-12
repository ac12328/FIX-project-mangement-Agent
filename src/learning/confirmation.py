"""Action-specific confirmation tokens bound to immutable resource revisions."""
from __future__ import annotations

import hashlib
import hmac
import json


class ConfirmationError(ValueError):
    pass


def token_for(
    action: str,
    resource_id: str,
    expected_revision: str,
    *,
    resource_version: int | None = None,
    scope: str | None = None,
    risk: str | None = None,
    actor_id: str | None = None,
    reason: str | None = None,
) -> str:
    if all(value is None for value in (resource_version, scope, risk, actor_id, reason)):
        return f"{action}:{resource_id}:{expected_revision}"
    payload = json.dumps(
        {
            "action": action,
            "actor_id": actor_id,
            "expected_revision": expected_revision,
            "reason": reason,
            "resource_id": resource_id,
            "resource_version": resource_version,
            "risk": risk,
            "scope": scope,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "lcf_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_confirmation(*, action: str, resource_id: str, expected_revision: str,
                         supplied_revision: str | None, supplied_token: str | None,
                         confirmed: bool, resource_version: int | None = None,
                         scope: str | None = None, risk: str | None = None,
                         actor_id: str | None = None, reason: str | None = None) -> None:
    if not confirmed or not supplied_revision or not supplied_token:
        raise ConfirmationError("action-specific confirmation token and expected revision are required")
    if not hmac.compare_digest(supplied_revision, expected_revision):
        raise ConfirmationError("resource state changed; refresh before confirming")
    expected = token_for(
        action,
        resource_id,
        expected_revision,
        resource_version=resource_version,
        scope=scope,
        risk=risk,
        actor_id=actor_id,
        reason=reason,
    )
    if not hmac.compare_digest(supplied_token, expected):
        raise ConfirmationError("confirmation token does not match the requested action")
