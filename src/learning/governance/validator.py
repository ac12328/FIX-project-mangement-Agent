"""Deterministic validation and bounded immutable revision workflow."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from src.learning.governance.contracts import (
    CandidateLoadError, CandidateRecord, CandidateRevision, ValidationFinding, ValidationResult,
)
from src.learning.governance.injection_guard import indicator_codes
from src.learning.governance.policy import GovernancePolicy
from src.learning.governance.retention import retention_codes
from src.learning.governance.risk_policy import classify, severity_for
from src.learning.governance.scope_policy import scope_codes
from src.learning.governance.sensitivity import sensitivity_codes
from src.learning.ingest.contracts import LearningObservation
from src.repositories.learning_observation_repo import get_observation


_MESSAGES = {
    "invalid_schema": "Candidate content does not satisfy the governance schema.",
    "canonical_subject_mismatch": "Subject identity does not match the candidate scope.",
    "missing_evidence": "One or more evidence references do not exist.",
    "missing_source_observation": "The candidate original observation does not exist.",
    "tombstoned_source_observation": "The candidate original observation has been tombstoned.",
    "source_observation_not_evidence": "The candidate original observation is not cited as evidence.",
    "unauthorized_evidence": "Evidence does not authorize the candidate scope.",
    "insufficient_trust": "Evidence trust tier is insufficient for this target.",
    "insufficient_independent_sources": "The scope requires more independent source events.",
    "scope_too_broad": "Candidate scope is broader than its evidence permits.",
    "cross_project_scope": "Broad scope lacks evidence from enough independent projects.",
    "contains_secret": "Candidate contains a credential or secret indicator.",
    "contains_pii": "Candidate contains personally identifiable information.",
    "sensitivity_not_allowed": "Evidence sensitivity is not allowed for the target.",
    "possible_prompt_injection": "Candidate contains a prompt-injection indicator.",
    "unsupported_policy_change": "Learned candidates cannot change prompts, permissions, confirmations, or safety policy.",
    "exfiltration_instruction": "Candidate contains an exfiltration instruction.",
    "untrusted_instruction_as_procedure": "Untrusted instructions cannot become procedural memory.",
    "single_incident_procedure": "A single incident cannot establish a reusable procedure.",
    "missing_expiry": "The candidate requires an expiry or review date.",
    "invalid_expiry": "The candidate expiry is invalid.",
    "expired_before_validation": "The candidate expired before validation.",
}


def load_candidate(conn: sqlite3.Connection, candidate_id: str, candidate_version: int) -> CandidateRecord:
    row = conn.execute(
        "SELECT * FROM memory_candidates WHERE candidate_id=? AND candidate_version=?",
        (candidate_id, candidate_version),
    ).fetchone()
    if row is None:
        raise KeyError((candidate_id, candidate_version))
    value = dict(row)
    state = conn.execute(
        "SELECT disposition FROM candidate_governance_states WHERE candidate_id=? AND candidate_version=?",
        (candidate_id, candidate_version),
    ).fetchone()
    try:
        return CandidateRecord(
            candidate_id=value["candidate_id"], observation_id=value["observation_id"],
            candidate_version=value["candidate_version"], target_type=value["target_type"],
            subject_type=value["subject_type"], subject_id=value["subject_id"], predicate=value["predicate"],
            proposed_value=json.loads(value["proposed_value_json"]), scope_type=value["scope_type"],
            scope_id=value["scope_id"], confidence=value["confidence"],
            suggested_expiry=value["suggested_expiry"], reasoning_summary=value["reasoning_summary"],
            risk_signals=json.loads(value["risk_signals_json"]), evidence_refs=json.loads(value["evidence_refs_json"]),
            status=state["disposition"] if state else value["status"],
            extractor_version=value["extractor_version"], model_version=value["model_version"],
            prompt_version=value["prompt_version"], schema_version=value["schema_version"],
        )
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        field = "persisted candidate"
        if isinstance(exc, json.JSONDecodeError):
            field = next((name for name in ("proposed_value_json", "risk_signals_json", "evidence_refs_json")
                          if value.get(name) and _invalid_json(value[name])), field)
        raise CandidateLoadError(f"invalid {field} schema") from exc


def validate_candidate(conn: sqlite3.Connection, candidate_id: str, candidate_version: int,
                       *, validation_at: datetime,
                       policy: GovernancePolicy | None = None) -> ValidationResult:
    """Validate and persist a deterministic result; never approve or activate."""
    policy = policy or GovernancePolicy()
    validation_at = _as_utc(validation_at)
    try:
        candidate = load_candidate(conn, candidate_id, candidate_version)
    except CandidateLoadError:
        return _persist_invalid_schema_result(
            conn, candidate_id, candidate_version, validation_at=validation_at, policy=policy,
        )
    observations, missing = _load_evidence(conn, candidate.evidence_refs)
    codes: set[str] = set()
    codes.update(_source_provenance_codes(conn, candidate, observations))
    if missing:
        codes.add("missing_evidence")
    codes.update(_schema_codes(candidate))
    if observations:
        codes.update(scope_codes(
            candidate, observations,
            minimum_sources=policy.min_sources_by_scope.get(candidate.scope_type, 1),
        ))
        codes.update(_trust_codes(candidate, observations))
    text = _candidate_text(candidate)
    codes.update(sensitivity_codes(
        text, observation_sensitivities={item.sensitivity.value for item in observations},
        target_type=candidate.target_type,
        allowed=set(policy.allowed_sensitivity_by_target.get(candidate.target_type, frozenset())),
    ))
    codes.update(indicator_codes(text, target_type=candidate.target_type))
    codes.update(retention_codes(
        target_type=candidate.target_type, scope_type=candidate.scope_type,
        suggested_expiry=candidate.suggested_expiry,
        expiry_required_targets=set(policy.expiry_required_targets),
        expiry_required_scopes=set(policy.expiry_required_scopes),
        validation_at=validation_at,
    ))
    if _is_single_incident_procedure(candidate, observations):
        codes.add("single_incident_procedure")

    findings = [ValidationFinding(code=code, severity=severity_for(code), message=_MESSAGES[code])
                for code in sorted(codes)]
    requires_review = candidate.target_type in policy.review_required_targets or candidate.scope_type in policy.review_required_scopes
    requires_evaluation = candidate.target_type in policy.evaluation_required_targets or candidate.scope_type in policy.evaluation_required_scopes
    passed = not findings
    disposition = _disposition(candidate, codes, policy)
    risk_level, risk_score = classify(codes, target_type=candidate.target_type, scope_type=candidate.scope_type)
    input_snapshot = _input_snapshot(
        conn, candidate, observations, missing, validation_at=validation_at, policy=policy,
    )
    input_snapshot_json = _canonical_json(input_snapshot)
    input_hash = hashlib.sha256(input_snapshot_json.encode("utf-8")).hexdigest()
    findings_hash = _hash([item.model_dump(mode="json") for item in findings])
    validation_id = f"cv_{uuid.uuid4().hex}"
    created_at = datetime.now(timezone.utc)
    result = ValidationResult(
        validation_id=validation_id, candidate_id=candidate_id, candidate_version=candidate_version,
        validator_version=policy.validator_version, policy_version=policy.policy_version,
        policy_json=policy.model_dump_json(), passed=passed, findings=findings, risk_level=risk_level, risk_score=risk_score,
        input_hash=input_hash, input_snapshot_json=input_snapshot_json,
        findings_hash=findings_hash, disposition=disposition,
        requires_review=requires_review, requires_evaluation=requires_evaluation,
        created_at=created_at,
    )
    _persist_validation(conn, result, validation_at=validation_at)
    conn.execute(
        """INSERT INTO candidate_governance_states
           (candidate_id, candidate_version, disposition, validation_id, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(candidate_id, candidate_version) DO UPDATE SET
             disposition=excluded.disposition, validation_id=excluded.validation_id, updated_at=excluded.updated_at""",
        (candidate_id, candidate_version, disposition, validation_id, created_at.isoformat()),
    )
    return result


def persist_revision(conn: sqlite3.Connection, candidate_id: str, candidate_version: int,
                     revision: CandidateRevision, *, validation_id: str) -> CandidateRecord:
    """Append one immutable repaired version tied to the exact failed validation."""
    validation = conn.execute(
        """SELECT candidate_id, candidate_version, passed, findings_json, disposition, policy_json
           FROM candidate_validation_runs WHERE validation_id=?""",
        (validation_id,),
    ).fetchone()
    state = conn.execute(
        """SELECT disposition, validation_id FROM candidate_governance_states
           WHERE candidate_id=? AND candidate_version=?""",
        (candidate_id, candidate_version),
    ).fetchone()
    if validation is None or validation["candidate_id"] != candidate_id or validation["candidate_version"] != candidate_version:
        raise ValueError("validation does not belong to candidate version")
    if validation["passed"] or validation["disposition"] != "needs_revision":
        raise ValueError("repair requires a failed needs_revision validation")
    if state is None or state["validation_id"] != validation_id or state["disposition"] != "needs_revision":
        raise ValueError("repair requires the current failed validation and needs_revision status")
    candidate = load_candidate(conn, candidate_id, candidate_version)
    finding_codes = {item.get("code") for item in json.loads(validation["findings_json"])}
    validation_policy = GovernancePolicy.model_validate_json(validation["policy_json"])
    if not finding_codes or not finding_codes <= validation_policy.repairable_findings:
        raise ValueError("validation contains non-repairable findings")
    if candidate_version - 1 >= validation_policy.max_revisions:
        raise ValueError("maximum revisions reached")
    next_version = candidate_version + 1
    existing = conn.execute(
        "SELECT 1 FROM memory_candidates WHERE candidate_id=? AND candidate_version=?",
        (candidate_id, next_version),
    ).fetchone()
    if existing:
        raise ValueError("candidate revision already exists")
    values = revision.model_dump(exclude_none=True, mode="json")
    conn.execute(
        """INSERT INTO memory_candidates
           (candidate_id, observation_id, candidate_version, target_type, subject_type, subject_id,
            predicate, proposed_value_json, scope_type, scope_id, confidence, suggested_expiry,
            reasoning_summary, risk_signals_json, evidence_refs_json, status, extractor_version,
            model_version, prompt_version, schema_version, revision_of_validation_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?)""",
        (
            candidate_id, candidate.observation_id, next_version, candidate.target_type,
            values.get("subject_type", candidate.subject_type), values.get("subject_id", candidate.subject_id),
            values.get("predicate", candidate.predicate),
            json.dumps(revision.proposed_value, ensure_ascii=False, sort_keys=True, allow_nan=False),
            values.get("scope_type", candidate.scope_type), values.get("scope_id", candidate.scope_id),
            candidate.confidence, values.get("suggested_expiry"), revision.reasoning_summary,
            json.dumps(values.get("risk_signals", candidate.risk_signals), ensure_ascii=False, sort_keys=True),
            json.dumps(candidate.evidence_refs, ensure_ascii=False, sort_keys=True), candidate.extractor_version,
            candidate.model_version, candidate.prompt_version, candidate.schema_version, validation_id,
        ),
    )
    return load_candidate(conn, candidate_id, next_version)


def _load_evidence(conn: sqlite3.Connection, refs: list[str]) -> tuple[list[LearningObservation], set[str]]:
    observations: list[LearningObservation] = []
    missing: set[str] = set()
    for ref in refs:
        observation = get_observation(conn, ref)
        if observation is None or observation.is_tombstone:
            missing.add(ref)
        else:
            observations.append(observation)
    return observations, missing


def _schema_codes(candidate: CandidateRecord) -> set[str]:
    codes: set[str] = set()
    if not candidate.predicate.strip() or not candidate.evidence_refs:
        codes.add("invalid_schema")
    if candidate.scope_type != "global" and not candidate.scope_id:
        codes.add("invalid_schema")
    if candidate.subject_type != "global" and not candidate.subject_id:
        codes.add("invalid_schema")
    return codes


def _trust_codes(candidate: CandidateRecord, observations: list[LearningObservation]) -> set[str]:
    if candidate.target_type in {"procedure", "wiki_publication"}:
        if any(item.trust_tier.value in {"untrusted", "external"} for item in observations):
            return {"insufficient_trust"}
    return set()


def _candidate_text(candidate: CandidateRecord) -> str:
    return json.dumps({
        "predicate": candidate.predicate,
        "proposed_value": candidate.proposed_value,
        "reasoning_summary": candidate.reasoning_summary,
        "risk_signals": candidate.risk_signals,
    }, ensure_ascii=False, sort_keys=True)


def _is_single_incident_procedure(candidate: CandidateRecord, observations: list[LearningObservation]) -> bool:
    if candidate.target_type != "procedure" or len(observations) != 1:
        return False
    text = _candidate_text(candidate)
    return bool(re.search(r"\b(?:INC|TICKET|CASE)[-_ ]?\d+\b|\b(?:once|one[- ]off|workaround)\b", text, re.I))


def _disposition(candidate: CandidateRecord, codes: set[str], policy: GovernancePolicy) -> str:
    if codes & policy.reject_findings:
        return "rejected"
    if codes & policy.quarantine_findings:
        return "quarantined"
    if codes:
        if not codes <= policy.repairable_findings:
            return "needs_review"
        if candidate.candidate_version - 1 >= policy.max_revisions:
            return policy.terminal_after_repairs
        return "needs_revision"
    return "needs_review"


def _persist_invalid_schema_result(
    conn: sqlite3.Connection, candidate_id: str, candidate_version: int, *,
    validation_at: datetime, policy: GovernancePolicy,
) -> ValidationResult:
    row = conn.execute(
        "SELECT * FROM memory_candidates WHERE candidate_id=? AND candidate_version=?",
        (candidate_id, candidate_version),
    ).fetchone()
    if row is None:
        raise KeyError((candidate_id, candidate_version))
    finding = ValidationFinding(
        code="invalid_schema", severity=severity_for("invalid_schema"),
        message=_MESSAGES["invalid_schema"],
    )
    findings = [finding]
    created_at = datetime.now(timezone.utc)
    result = ValidationResult(
        validation_id=f"cv_{uuid.uuid4().hex}", candidate_id=candidate_id,
        candidate_version=candidate_version, validator_version=policy.validator_version,
        policy_version=policy.policy_version, policy_json=policy.model_dump_json(),
        passed=False, findings=findings,
        risk_level="medium", risk_score=classify({"invalid_schema"}, target_type="invalid", scope_type="invalid")[1],
        input_hash=_hash({
            "persisted_candidate": dict(row), "validation_at": validation_at.isoformat(),
            "validator_version": policy.validator_version, "policy": policy.model_dump(mode="json"),
        }),
        input_snapshot_json=_canonical_json({
            "persisted_candidate": dict(row), "validation_at": validation_at.isoformat(),
            "validator_version": policy.validator_version, "policy": policy.model_dump(mode="json"),
        }),
        findings_hash=_hash([finding.model_dump(mode="json")]), disposition="needs_review",
        requires_review=False, requires_evaluation=False, created_at=created_at,
    )
    _persist_validation(conn, result, validation_at=validation_at)
    conn.execute(
        """INSERT INTO candidate_governance_states
           (candidate_id, candidate_version, disposition, validation_id, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(candidate_id, candidate_version) DO UPDATE SET
             disposition=excluded.disposition, validation_id=excluded.validation_id,
             updated_at=excluded.updated_at""",
        (candidate_id, candidate_version, result.disposition, result.validation_id, created_at.isoformat()),
    )
    return result


def _persist_validation(conn: sqlite3.Connection, result: ValidationResult, *, validation_at: datetime) -> None:
    conn.execute(
        """INSERT INTO candidate_validation_runs
           (validation_id, candidate_id, candidate_version, validator_version, policy_version,
            policy_json, passed, findings_json, risk_level, risk_score, input_hash,
            input_snapshot_json, findings_hash, disposition, requires_review, requires_evaluation,
            validation_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            result.validation_id, result.candidate_id, result.candidate_version,
            result.validator_version, result.policy_version, result.policy_json, int(result.passed),
            json.dumps([item.model_dump(mode="json") for item in result.findings], ensure_ascii=False, sort_keys=True),
            result.risk_level, result.risk_score, result.input_hash,
            result.input_snapshot_json, result.findings_hash,
            result.disposition, int(result.requires_review), int(result.requires_evaluation),
            validation_at.isoformat(), result.created_at.isoformat(),
        ),
    )


def _input_snapshot(
    conn: sqlite3.Connection, candidate: CandidateRecord,
    observations: list[LearningObservation], missing: set[str], *,
    validation_at: datetime, policy: GovernancePolicy,
) -> dict[str, Any]:
    source = get_observation(conn, candidate.observation_id)
    tombstones = conn.execute(
        """SELECT observation_id, content_hash, is_tombstone, tombstone_of_observation_id
           FROM learning_observations
           WHERE is_tombstone=1 AND tombstone_of_observation_id=?
           ORDER BY observation_id""",
        (candidate.observation_id,),
    ).fetchall()
    return {
        "candidate": candidate.model_dump(exclude={"status"}, mode="json"),
        "evidence": [_evidence_snapshot(item) for item in sorted(observations, key=lambda item: item.observation_id)],
        "missing_evidence": sorted(missing),
        "source_observation": _evidence_snapshot(source) if source is not None else None,
        "source_tombstones": [dict(row) for row in tombstones],
        "validation_at": validation_at.isoformat(),
        "validator_version": policy.validator_version,
        "policy": policy.model_dump(mode="json"),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _evidence_snapshot(observation: LearningObservation) -> dict[str, Any]:
    return {
        "observation_id": observation.observation_id,
        "content_hash": observation.content_hash,
        "source_type": observation.source_type.value,
        "source_id": observation.source_id,
        "operator_id": observation.operator_id,
        "project_id": observation.project_id,
        "contact_id": observation.contact_id,
        "counterparty_id": observation.counterparty_id,
        "sensitivity": observation.sensitivity.value,
        "trust_tier": observation.trust_tier.value,
    }


def _source_provenance_codes(conn: sqlite3.Connection, candidate: CandidateRecord,
                             observations: list[LearningObservation]) -> set[str]:
    source = get_observation(conn, candidate.observation_id)
    if source is None:
        return {"missing_source_observation"}
    tombstone = conn.execute(
        "SELECT 1 FROM learning_observations WHERE is_tombstone=1 AND tombstone_of_observation_id=? LIMIT 1",
        (candidate.observation_id,),
    ).fetchone()
    codes: set[str] = set()
    if source.is_tombstone or tombstone:
        codes.add("tombstoned_source_observation")
    if candidate.observation_id not in candidate.evidence_refs or not any(
        item.observation_id == candidate.observation_id and item.content_hash == source.content_hash
        for item in observations
    ):
        codes.add("source_observation_not_evidence")
    return codes


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("validation_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _invalid_json(value: str) -> bool:
    try:
        json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return True
    return False


def _hash(value: Any) -> str:
    encoded = _canonical_json(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
