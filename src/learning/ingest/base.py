"""Deterministic, privacy-minimizing helpers shared by source adapters."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.learning.ingest.contracts import LearningObservation, ObservationSourceType, Sensitivity, TrustTier

_SECRET_KEY_RE = re.compile(r"(?i)(?:api[_-]?key|secret|password|passwd|token|authorization|bearer)")
_SECRET_VALUE_RE = re.compile(
    r'''(?ix)(?:\b(?:api[_-]?key|secret|password|passwd|token|authorization)\b\s*(?:[:=]\s*|"\s*:\s*")|\bbearer\s+)["']?[^\s,"'}]+'''
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")


class ForbiddenSecretPayload(ValueError):
    """Raised when source evidence contains credential material."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(*, source_type: ObservationSourceType, source_id: str, payload: dict[str, Any], normalized_text: str) -> str:
    material = canonical_json({"source_type": source_type.value, "source_id": source_id, "payload": payload, "text": normalized_text})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def idempotency_key(*, source_type: ObservationSourceType, source_id: str, content_digest: str) -> str:
    return hashlib.sha256(f"{source_type.value}\0{source_id}\0{content_digest}".encode("utf-8")).hexdigest()


def sanitize_text(value: str) -> tuple[str, Sensitivity]:
    if _SECRET_VALUE_RE.search(value):
        raise ForbiddenSecretPayload("secret-bearing learning observation payload is forbidden")
    redacted = _EMAIL_RE.sub("[email-redacted]", value)
    return redacted, Sensitivity.CONFIDENTIAL if redacted != value else Sensitivity.INTERNAL


def sanitize_payload(value: Any, *, key: str | None = None) -> tuple[Any, Sensitivity]:
    if key and _SECRET_KEY_RE.search(key):
        raise ForbiddenSecretPayload("secret-bearing learning observation payload is forbidden")
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        sanitized, levels = zip(*(sanitize_payload(item) for item in value), strict=False) if value else ((), ())
        return list(sanitized), Sensitivity.CONFIDENTIAL if Sensitivity.CONFIDENTIAL in levels else Sensitivity.INTERNAL
    if isinstance(value, dict):
        items = {str(item_key): sanitize_payload(item, key=str(item_key)) for item_key, item in value.items()}
        return {key: item[0] for key, item in items.items()}, (Sensitivity.CONFIDENTIAL if any(item[1] is Sensitivity.CONFIDENTIAL for item in items.values()) else Sensitivity.INTERNAL)
    return value, Sensitivity.INTERNAL


def canonicalize_observation(observation: LearningObservation) -> LearningObservation:
    """Rebuild untrusted contracts into the sole accepted canonical representation."""
    text, text_sensitivity = sanitize_text(observation.normalized_text)
    payload, payload_sensitivity = sanitize_payload(observation.raw_payload)
    digest = content_hash(source_type=observation.source_type, source_id=observation.source_id, payload=payload, normalized_text=text)
    sensitivity = Sensitivity.CONFIDENTIAL if Sensitivity.CONFIDENTIAL in (observation.sensitivity, text_sensitivity, payload_sensitivity) else Sensitivity.INTERNAL
    canonical = observation.model_copy(update={
        "observation_id": f"obs_{digest[:24]}", "normalized_text": text, "raw_payload": payload,
        "content_hash": digest, "idempotency_key": idempotency_key(source_type=observation.source_type, source_id=observation.source_id, content_digest=digest), "sensitivity": sensitivity,
    })
    return canonical


def capture_observation(conn: sqlite3.Connection, *, source_type: ObservationSourceType, source_id: str, operator_id: str, text: str, raw_payload: dict[str, Any] | None = None, trust_tier: TrustTier, project_id: str | None = None, contact_id: str | None = None, counterparty_id: str | None = None, artifact_type: str | None = None, artifact_id: str | None = None, trace_id: str | None = None, output_revision_id: str | None = None, occurred_at: datetime | None = None, is_tombstone: bool = False, tombstone_of_observation_id: str | None = None, tombstone_reason: str | None = None) -> LearningObservation:
    """Sanitize, hash, and append one source observation without external I/O."""
    provisional = LearningObservation(observation_id="obs_pending", source_type=source_type, source_id=source_id, idempotency_key="pending", operator_id=operator_id, project_id=project_id, contact_id=contact_id, counterparty_id=counterparty_id, artifact_type=artifact_type, artifact_id=artifact_id, trace_id=trace_id, output_revision_id=output_revision_id, raw_payload=raw_payload or {}, normalized_text=text, content_hash="pending", trust_tier=trust_tier, occurred_at=occurred_at or datetime.now(timezone.utc), is_tombstone=is_tombstone, tombstone_of_observation_id=tombstone_of_observation_id, tombstone_reason=tombstone_reason)
    from src.repositories.learning_observation_repo import insert_observation
    return insert_observation(conn, canonicalize_observation(provisional))
