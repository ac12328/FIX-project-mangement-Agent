"""Governed, deterministic backfill from legacy learning stores."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from src.learning.governance.contradiction import analyse_candidate_conflicts
from src.learning.governance.validator import validate_candidate
from src.learning.ingest.base import canonicalize_observation
from src.learning.ingest.contracts import LearningObservation, ObservationSourceType, TrustTier
from src.learning.migration.contracts import BackfillRowResult, BackfillSummary, LegacyStore

_EXTRACTOR_VERSION = "legacy-backfill-v1"
_MODEL_VERSION = "none-deterministic-migration"
_PROMPT_VERSION = "none"
_SCHEMA_VERSION = "legacy-backfill-v1"
_MAX_TEXT = 2000
_MAX_ITEMS = 20
_SUPPORTED_SCOPES = {"project", "counterparty", "contact", "organization"}
_SCOPE_ALIASES = {"organisation": "organization"}
_RESPONSE_FIELDS = (
    "preferred_greeting", "tone", "response_length_preference", "preferred_language",
    "avoid_phrases", "positive_signals",
)


def backfill_legacy_learning(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    dry_run: bool = False,
    validation_at: datetime | None = None,
) -> BackfillSummary:
    """Create observations and proposed candidates only; never approve or publish."""
    if not operator_id.strip():
        raise ValueError("operator_id is required for legacy ownership attribution")
    validation_at = validation_at or datetime.now(timezone.utc)
    if validation_at.tzinfo is None:
        raise ValueError("validation_at must be timezone-aware")
    results: list[BackfillRowResult] = []
    for spec in _iter_specs(conn, operator_id=operator_id):
        results.append(_process_spec(conn, spec, operator_id=operator_id, dry_run=dry_run, validation_at=validation_at))
    return BackfillSummary(dry_run=dry_run, operator_id=operator_id, results=results)


def _iter_specs(conn: sqlite3.Connection, *, operator_id: str) -> Iterable[dict[str, Any]]:
    for row in conn.execute("SELECT * FROM agent_notes ORDER BY id"):
        yield _agent_note_spec(dict(row), operator_id)
    for row in conn.execute("SELECT * FROM operator_memory ORDER BY id"):
        yield _operator_memory_spec(dict(row))
    for row in conn.execute("SELECT * FROM learned_rules ORDER BY rule_id"):
        yield _learned_rule_spec(dict(row), operator_id)
    for row in conn.execute("SELECT * FROM response_pattern_memory ORDER BY id"):
        yield from _response_specs(dict(row), operator_id)


def _agent_note_spec(row: dict[str, Any], operator_id: str) -> dict[str, Any]:
    risks = ["legacy_agent_note", "ownership_ambiguous", "review_only"]
    return _spec(
        "agent_notes", str(row["id"]), operator_id, row.get("note_value") or "",
        target="preference" if row.get("note_type") == "preference" else "semantic_fact",
        predicate=_legacy_predicate(row.get("note_key"), prefix="agent_note"), value=row.get("note_value"),
        scope_type="operator", scope_id=operator_id, confidence=0.5,
        metadata={"note_key": row.get("note_key"), "note_type": row.get("note_type"), "updated_at": row.get("updated_at")},
        risks=risks, occurred_at=row.get("updated_at"),
    )


def _operator_memory_spec(row: dict[str, Any]) -> dict[str, Any]:
    legacy_scope = str(row.get("scope") or "global")
    scope = _SCOPE_ALIASES.get(legacy_scope, legacy_scope)
    risks = ["legacy_operator_memory"]
    target = "procedure" if row.get("memory_type") == "procedural" else "semantic_fact"
    scope_id = row.get("scope_id")
    if row.get("memory_type") in {"forbidden", "episodic_snippet"}:
        risks += [f"legacy_{row.get('memory_type')}", "quarantine_safe", "review_only"]
        scope, scope_id = "operator", row["operator_id"]
    elif scope == "global" or scope not in _SUPPORTED_SCOPES:
        risks += ["unsupported_or_unscoped_scope", "scope_downgraded", "review_only"]
        scope, scope_id = "operator", row["operator_id"]
    elif not scope_id or scope_id == "*":
        risks += ["unscoped_scope_id", "scope_downgraded", "review_only"]
        scope, scope_id = "operator", row["operator_id"]
    provenance_scope = {
        "project_id": scope_id if scope == "project" else None,
        "contact_id": scope_id if scope == "contact" else None,
        "counterparty_id": scope_id if scope == "counterparty" else None,
    }
    return _spec(
        "operator_memory", str(row["id"]), row["operator_id"], row.get("memory_value") or "",
        target=target, predicate=_legacy_predicate(row.get("memory_key"), prefix="operator_memory"), value=row.get("memory_value"),
        scope_type=scope, scope_id=scope_id, confidence=0.5,
        metadata={key: row.get(key) for key in ("scope", "scope_id", "memory_type", "memory_key", "source", "active", "created_at", "updated_at")},
        risks=risks, occurred_at=row.get("updated_at"), **provenance_scope,
    )


def _learned_rule_spec(row: dict[str, Any], operator_id: str) -> dict[str, Any]:
    scope, scope_id = _rule_scope(row)
    risks = ["legacy_learned_rule", "legacy_status_not_v2_approval", "review_only"]
    if scope == "global":
        risks.append("legacy_global_scope")
    value = {"condition": row.get("condition"), "rule": row.get("rule"), "extra_knowledge": _bounded_json(row.get("extra_knowledge"))}
    return _spec(
        "learned_rules", str(row["rule_id"]), operator_id, row.get("rule") or row.get("condition") or "",
        target="procedure" if row.get("trigger_type") == "procedure" else "semantic_fact",
        predicate=f"learned_rule.{row.get('trigger_type') or 'rule'}", value=value,
        scope_type=scope, scope_id=scope_id, confidence=_confidence(row.get("confidence")),
        metadata={key: row.get(key) for key in ("trigger_type", "source_project_id", "scope", "applicable_to", "status", "conflict_with", "usage_count", "confidence", "expanded_by", "expanded_at", "written_to_kb", "kb_file", "kb_written_at", "created_at")},
        risks=risks, occurred_at=row.get("created_at"),
    )


def _response_specs(row: dict[str, Any], operator_id: str) -> Iterable[dict[str, Any]]:
    scope_type = "project" if row.get("project_id") else "contact"
    scope_id = row.get("project_id") or row["contact_id"]
    for field in _RESPONSE_FIELDS:
        for suffix, value in _split_field(field, row.get(field)):
            yield _spec(
                "response_pattern_memory", f"{row['id']}:{field}:{suffix}", operator_id, str(value),
                target="preference", predicate=f"response_pattern.{field}", value=value,
                scope_type=scope_type, scope_id=scope_id, confidence=_confidence(row.get("confidence")),
                metadata={"row_id": row["id"], "field": field, "item": suffix, "contact_id": row.get("contact_id"), "project_id": row.get("project_id"), "source": row.get("source"), "confidence": row.get("confidence"), "updated_at": row.get("updated_at")},
                risks=["legacy_response_pattern", "field_level_preference", "review_only"],
                occurred_at=row.get("updated_at"), contact_id=row.get("contact_id"), project_id=row.get("project_id"),
            )


def _process_spec(conn: sqlite3.Connection, spec: dict[str, Any], *, operator_id: str, dry_run: bool, validation_at: datetime) -> BackfillRowResult:
    store: LegacyStore = spec["store"]
    source_id = f"legacy:{store}:{spec['legacy_row_id']}"
    if not str(spec["text"]).strip():
        return BackfillRowResult(store=store, legacy_row_id=spec["legacy_row_id"], source_id=source_id, disposition="skipped_empty", detail="empty legacy value")
    observation = _observation(spec, source_id)
    candidate_id = _candidate_id(source_id, observation.content_hash)
    if dry_run:
        return BackfillRowResult(store=store, legacy_row_id=spec["legacy_row_id"], source_id=source_id, disposition="dry_run", observation_id=observation.observation_id, candidate_id=candidate_id, candidate_version=1)
    existed = conn.execute("SELECT 1 FROM memory_candidates WHERE candidate_id=? AND candidate_version=1", (candidate_id,)).fetchone() is not None
    from src.repositories.learning_observation_repo import insert_observation
    persisted = insert_observation(conn, observation)
    _insert_candidate(conn, candidate_id, persisted.observation_id, spec)
    validation = _current_or_validate(conn, candidate_id, validation_at)
    conflict = analyse_candidate_conflicts(conn, candidate_id, 1)
    return BackfillRowResult(
        store=store, legacy_row_id=spec["legacy_row_id"], source_id=source_id,
        disposition="existing" if existed else "created", observation_id=persisted.observation_id,
        candidate_id=candidate_id, candidate_version=1, governance_disposition=validation.disposition,
        conflict_match=conflict.match_type, conflict_id=conflict.conflict_id,
    )


def _current_or_validate(conn: sqlite3.Connection, candidate_id: str,
                         validation_at: datetime):
    row = conn.execute(
        """SELECT v.* FROM candidate_governance_states s
           JOIN candidate_validation_runs v ON v.validation_id=s.validation_id
           WHERE s.candidate_id=? AND s.candidate_version=1""",
        (candidate_id,),
    ).fetchone()
    if row:
        from src.learning.governance.contracts import ValidationFinding, ValidationResult
        return ValidationResult(
            validation_id=row["validation_id"], candidate_id=row["candidate_id"],
            candidate_version=row["candidate_version"], validator_version=row["validator_version"],
            policy_version=row["policy_version"], policy_json=row["policy_json"],
            passed=bool(row["passed"]),
            findings=[ValidationFinding.model_validate(item) for item in json.loads(row["findings_json"])],
            risk_level=row["risk_level"], risk_score=row["risk_score"], input_hash=row["input_hash"],
            input_snapshot_json=row["input_snapshot_json"], findings_hash=row["findings_hash"],
            disposition=row["disposition"], requires_review=bool(row["requires_review"]),
            requires_evaluation=bool(row["requires_evaluation"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
    return validate_candidate(conn, candidate_id, 1, validation_at=validation_at)


def _observation(spec: dict[str, Any], source_id: str) -> LearningObservation:
    occurred = _parse_datetime(spec.get("occurred_at"))
    provisional = LearningObservation(
        observation_id="obs_pending", source_type=ObservationSourceType.MIGRATION, source_id=source_id,
        idempotency_key="pending", operator_id=spec["operator_id"], project_id=spec.get("project_id"),
        contact_id=spec.get("contact_id"), artifact_type="legacy_learning_record",
        artifact_id=f"{spec['store']}:{spec['legacy_row_id']}", raw_payload=_bounded_payload(spec["metadata"]),
        normalized_text=_bounded_text(str(spec["text"])), content_hash="pending", trust_tier=TrustTier.SYSTEM,
        occurred_at=occurred,
    )
    return canonicalize_observation(provisional)


def _insert_candidate(conn: sqlite3.Connection, candidate_id: str, observation_id: str, spec: dict[str, Any]) -> None:
    values = (
        candidate_id, observation_id, 1, spec["target"], spec["scope_type"], spec["scope_id"],
        spec["predicate"], json.dumps(_bounded_value(spec["value"]), ensure_ascii=False, sort_keys=True),
        spec["scope_type"], spec["scope_id"], spec["confidence"], None,
        _bounded_text("Legacy migration proposal. " + json.dumps(_bounded_payload(spec["metadata"]), ensure_ascii=False, sort_keys=True)),
        json.dumps(spec["risks"], ensure_ascii=False, sort_keys=True), json.dumps([observation_id]), "proposed",
        _EXTRACTOR_VERSION, _MODEL_VERSION, _PROMPT_VERSION, _SCHEMA_VERSION,
    )
    conn.execute(
        """INSERT OR IGNORE INTO memory_candidates
        (candidate_id,observation_id,candidate_version,target_type,subject_type,subject_id,predicate,
         proposed_value_json,scope_type,scope_id,confidence,suggested_expiry,reasoning_summary,
         risk_signals_json,evidence_refs_json,status,extractor_version,model_version,prompt_version,schema_version)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values,
    )


def _spec(store: LegacyStore, legacy_row_id: str, operator_id: str, text: str, **kwargs: Any) -> dict[str, Any]:
    return {"store": store, "legacy_row_id": legacy_row_id, "operator_id": operator_id, "text": text, **kwargs}


def _candidate_id(source_id: str, digest: str) -> str:
    return "mc_" + hashlib.sha256(f"{source_id}\0{digest}\0{_EXTRACTOR_VERSION}".encode()).hexdigest()[:24]


def _legacy_predicate(value: Any, *, prefix: str) -> str:
    normalized = str(value or "value").strip().casefold().replace(" ", "_")
    if normalized in {"external_sharing", "auto_send"}:
        return normalized
    return f"{prefix}.{normalized}"


def _rule_scope(row: dict[str, Any]) -> tuple[str, str | None]:
    return "global", None


def _split_field(field: str, raw: Any) -> Iterable[tuple[str, Any]]:
    if raw is None or raw == "":
        return []
    if field in {"avoid_phrases", "positive_signals"}:
        parsed = _bounded_json(raw)
        items = parsed if isinstance(parsed, list) else [parsed]
        return [(f"{index:03d}", _bounded_value(item)) for index, item in enumerate(items[:_MAX_ITEMS]) if str(item).strip()]
    return [("000", _bounded_value(raw))]


def _bounded_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return _bounded_text(value)


def _bounded_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key)[:100]: _bounded_value(item) for key, item in list(sorted(value.items()))[:_MAX_ITEMS]}


def _bounded_value(value: Any, depth: int = 0) -> Any:
    if depth >= 3:
        return _bounded_text(str(value))
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, list):
        return [_bounded_value(item, depth + 1) for item in value[:_MAX_ITEMS]]
    if isinstance(value, dict):
        return {str(key)[:100]: _bounded_value(item, depth + 1) for key, item in list(sorted(value.items()))[:_MAX_ITEMS]}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _bounded_text(str(value))


def _bounded_text(value: str) -> str:
    return value[:_MAX_TEXT]


def _confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _parse_datetime(value: Any) -> datetime:
    if not value:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
