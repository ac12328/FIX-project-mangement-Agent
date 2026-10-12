"""Expiry and review requirements for governed candidates."""
from __future__ import annotations

from datetime import datetime, timezone


def retention_codes(*, target_type: str, scope_type: str, suggested_expiry: str | None,
                    expiry_required_targets: set[str], expiry_required_scopes: set[str],
                    validation_at: datetime) -> set[str]:
    codes: set[str] = set()
    required = target_type in expiry_required_targets or scope_type in expiry_required_scopes
    if required and not suggested_expiry:
        codes.add("missing_expiry")
    elif suggested_expiry:
        try:
            expiry = datetime.fromisoformat(suggested_expiry.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= validation_at:
                codes.add("expired_before_validation")
        except ValueError:
            codes.add("invalid_expiry")
    return codes
