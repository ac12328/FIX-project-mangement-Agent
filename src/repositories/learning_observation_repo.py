"""Append-only repository for canonical learning source evidence."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from src.learning.ingest.contracts import LearningObservation


class AppendOnlyViolation(ValueError):
    """Raised when a caller attempts to mutate immutable observation content."""


class ObservationProvenanceConflict(ValueError):
    """Raised when a duplicate identity is presented with incompatible scope."""


def _as_row(observation: LearningObservation) -> tuple:
    return (observation.observation_id, observation.source_type.value, observation.source_id, observation.idempotency_key, observation.operator_id, observation.project_id, observation.contact_id, observation.counterparty_id, observation.artifact_type, observation.artifact_id, observation.trace_id, observation.output_revision_id, json.dumps(observation.raw_payload, ensure_ascii=False, sort_keys=True), observation.normalized_text, observation.content_hash, observation.sensitivity.value, observation.trust_tier.value, observation.occurred_at.isoformat(), int(observation.is_tombstone), observation.tombstone_of_observation_id, observation.tombstone_reason)


def _from_row(row: sqlite3.Row | tuple) -> LearningObservation:
    value = dict(row) if isinstance(row, sqlite3.Row) else row
    return LearningObservation(observation_id=value["observation_id"], source_type=value["source_type"], source_id=value["source_id"], idempotency_key=value["idempotency_key"], operator_id=value["operator_id"], project_id=value["project_id"], contact_id=value["contact_id"], counterparty_id=value["counterparty_id"], artifact_type=value["artifact_type"], artifact_id=value["artifact_id"], trace_id=value["trace_id"], output_revision_id=value["output_revision_id"], raw_payload=json.loads(value["raw_payload_json"]), normalized_text=value["normalized_text"], content_hash=value["content_hash"], sensitivity=value["sensitivity"], trust_tier=value["trust_tier"], occurred_at=datetime.fromisoformat(value["occurred_at"]), created_at=datetime.fromisoformat(value["created_at"]), is_tombstone=bool(value["is_tombstone"]), tombstone_of_observation_id=value["tombstone_of_observation_id"], tombstone_reason=value["tombstone_reason"])


def _same_provenance(left: LearningObservation, right: LearningObservation) -> bool:
    fields = ("source_type", "source_id", "operator_id", "project_id", "contact_id", "counterparty_id", "artifact_type", "artifact_id", "trace_id", "output_revision_id", "trust_tier", "sensitivity", "is_tombstone", "tombstone_of_observation_id", "tombstone_reason")
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def insert_observation(conn: sqlite3.Connection, observation: LearningObservation) -> LearningObservation:
    """Canonicalize untrusted contracts then append once or reject provenance conflicts."""
    from src.learning.ingest.base import canonicalize_observation
    canonical = canonicalize_observation(observation)
    row = conn.execute("SELECT * FROM learning_observations WHERE source_type=? AND source_id=? AND content_hash=?", (canonical.source_type.value, canonical.source_id, canonical.content_hash)).fetchone()
    if row:
        existing = _from_row(row)
        if _same_provenance(existing, canonical):
            return existing
        raise ObservationProvenanceConflict("duplicate evidence has incompatible immutable provenance")
    try:
        conn.execute("""INSERT INTO learning_observations (observation_id, source_type, source_id, idempotency_key, operator_id, project_id, contact_id, counterparty_id, artifact_type, artifact_id, trace_id, output_revision_id, raw_payload_json, normalized_text, content_hash, sensitivity, trust_tier, occurred_at, is_tombstone, tombstone_of_observation_id, tombstone_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", _as_row(canonical))
    except sqlite3.IntegrityError as exc:
        existing_row = conn.execute("SELECT * FROM learning_observations WHERE idempotency_key=?", (canonical.idempotency_key,)).fetchone()
        if existing_row:
            existing = _from_row(existing_row)
            if _same_provenance(existing, canonical):
                return existing
            raise ObservationProvenanceConflict("idempotency identity has incompatible immutable provenance") from exc
        raise
    return get_observation(conn, canonical.observation_id)  # type: ignore[return-value]


def get_observation(conn: sqlite3.Connection, observation_id: str) -> LearningObservation | None:
    row = conn.execute("SELECT * FROM learning_observations WHERE observation_id=?", (observation_id,)).fetchone()
    return _from_row(row) if row else None


def tombstone_observation(conn: sqlite3.Connection, observation_id: str, *, actor_id: str, reason: str) -> LearningObservation:
    """Append a minimal audit tombstone; database triggers preserve the original row."""
    original = get_observation(conn, observation_id)
    if original is None:
        raise KeyError(observation_id)
    from src.learning.ingest.base import capture_observation
    from src.learning.ingest.contracts import ObservationSourceType, TrustTier
    return capture_observation(conn, source_type=ObservationSourceType.MIGRATION, source_id=f"tombstone:{observation_id}:{reason}", operator_id=actor_id, text="Observation tombstoned under retention policy.", raw_payload={"tombstone_of": observation_id, "reason": reason}, trust_tier=TrustTier.SYSTEM, project_id=original.project_id, artifact_type="learning_observation", artifact_id=observation_id, is_tombstone=True, tombstone_of_observation_id=observation_id, tombstone_reason=reason)
