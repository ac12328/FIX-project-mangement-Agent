"""Three-phase deterministic baseline-vs-candidate evaluation orchestration."""
from __future__ import annotations

import hashlib
import json
import uuid
from types import MappingProxyType
from datetime import datetime, timezone
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.db.connection import scoped_read, scoped_write
from src.learning.evaluation.assertions import evaluate_assertions
from src.learning.evaluation.contracts import CaseOutcome, ReplayRequest, ReplayResponse, normalize_expiry
from src.learning.evaluation.fixtures import EvaluationCase, validate_case_manifest
from src.learning.evaluation.private_corpus import configured_private_corpus, load_configured_cases
from src.learning.evaluation.scoring import QualityPolicy, quality, score_case
from src.learning.evaluation.suites import REQUIRED_SUITE_NAMES, default_cases, suite_manifest
from src.repositories.candidate_evaluation_repo import EvaluationRecord, candidate_snapshot, insert_evaluation, latest_evaluation


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ReplayEngine(Protocol):
    implementation_version: str
    model_version: str
    runtime_config: dict

    def replay(self, request: ReplayRequest) -> ReplayResponse: ...


class EvaluationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    runner_version: str = "wp07-runner-v2"
    policy_version: str = "wp07-policy-v2"
    quality: QualityPolicy = Field(default_factory=QualityPolicy)
    required_suites_by_risk: dict[str, tuple[str, ...]] = Field(default_factory=lambda: {
        "low": (),
        "medium": (),
        "high": (),
        "critical": (),
    })


class EvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    evaluation_id: str
    candidate_id: str
    candidate_version: int
    runner_version: str
    policy_version: str
    suite_manifest: dict[str, str]
    outcomes: list[CaseOutcome]
    failure_codes: list[str]
    suite_hash: str
    config_hash: str
    replay_hash: str
    runtime_hash: str
    input_hash: str
    output_hash: str
    passed: bool
    evaluated_at: datetime
    created_at: datetime


class EvaluationReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)
    ready: bool
    reasons: list[str]
    evaluation_id: str | None = None


CANONICAL_RISK_REQUIREMENTS = MappingProxyType({
    "low": frozenset(),
    "medium": frozenset({"cross_project_isolation"}),
    "high": frozenset(REQUIRED_SUITE_NAMES),
    "critical": frozenset(REQUIRED_SUITE_NAMES),
})
TARGET_REQUIREMENTS = MappingProxyType({
    "preference": frozenset({"contact_preferences"}),
    "procedure": frozenset({"tool_recovery", "fix_domain_rules"}),
    "wiki_publication": frozenset({"wiki_publication"}),
    "semantic_fact": frozenset({"contradiction"}),
})


def _normalize_evaluation_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("evaluation time must be timezone-aware")
    return value.astimezone(timezone.utc)

def effective_required_suites(snapshot: dict, policy: EvaluationPolicy) -> set[str]:
    candidate = snapshot["candidate"]
    required = set(CANONICAL_RISK_REQUIREMENTS[snapshot["risk_level"]])
    required.update(TARGET_REQUIREMENTS.get(candidate.get("target_type"), set()))
    if candidate.get("scope_type") == "global":
        required.update({"cross_project_isolation", "contradiction", "prompt_injection"})
    required.update(policy.required_suites_by_risk.get(snapshot["risk_level"], ()))
    return required


def _selected_cases(all_cases: list[EvaluationCase], required: set[str]) -> list[EvaluationCase]:
    return sorted([case for case in all_cases if case.suite_name in required], key=lambda case: case.case_id)

def runtime_readiness_reason(latest, engine: ReplayEngine | None) -> str | None:
    if engine is None:
        return "evaluation_runtime_unverified"
    if latest is None:
        return None
    attested_replay = getattr(engine, "attested_replay_hash", None)
    attested_runtime = getattr(engine, "attested_runtime_hash", None)
    if attested_replay is not None or attested_runtime is not None:
        if latest.replay_hash != attested_replay or latest.runtime_hash != attested_runtime:
            return "evaluation_runtime_stale"
        return None
    replay_hash = _hash({"implementation": engine.implementation_version, "model": engine.model_version})
    if latest.replay_hash != replay_hash or latest.runtime_hash != _hash(engine.runtime_config):
        return "evaluation_runtime_stale"
    return None


def _eligibility(snapshot: dict, evaluated_at: datetime) -> list[str]:
    reasons = []
    expiry = normalize_expiry(snapshot["candidate"].get("suggested_expiry"), evaluated_at)
    if snapshot["disposition"] not in {"needs_review", "needs_revision"}:
        reasons.append("candidate_not_eligible")
    if not expiry.valid or expiry.expired:
        reasons.append(expiry.reason or "invalid_expiry")
    if snapshot["authority_blocked"]:
        reasons.append("authority_blocked")
    return sorted(set(reasons))


def evaluate_candidate(candidate_id: str, candidate_version: int, *, cases: list[EvaluationCase], engine: ReplayEngine,
                       evaluated_at: datetime, policy: EvaluationPolicy | None = None) -> EvaluationResult:
    """Read snapshot, replay outside DB scopes, then persist in a short write."""
    policy = policy or EvaluationPolicy()
    private_cases = load_configured_cases(configured_private_corpus())
    cases = list(cases) + private_cases
    validate_case_manifest(cases)
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    evaluated_at = _normalize_evaluation_time(evaluated_at)
    with scoped_read() as conn:
        snapshot = candidate_snapshot(conn, candidate_id, candidate_version)

    required = effective_required_suites(snapshot, policy)
    ordered = _selected_cases(cases, required)
    eligibility = _eligibility(snapshot, evaluated_at)
    outcomes = []
    raw = []
    for case in ordered:
        baseline = engine.replay(ReplayRequest(case_id=case.case_id, input_payload=case.input_payload))
        candidate = engine.replay(ReplayRequest(case_id=case.case_id, input_payload=case.input_payload,
                                                candidate=snapshot["candidate"]))
        assertions = evaluate_assertions(case, candidate,
                                         allowed_project_ids={case.project_id} if case.project_id else set(),
                                         candidate_eligible=not eligibility)
        score = score_case(quality(baseline, case), quality(candidate, case), policy.quality)
        outcomes.append(CaseOutcome(case_id=case.case_id, baseline_quality=score.baseline,
                                    candidate_quality=score.candidate, quality_delta=score.delta,
                                    passed=score.passed and all(item.passed for item in assertions),
                                    assertions=assertions))
        raw.append({"case_id": case.case_id, "baseline": baseline.model_dump(mode="json"),
                    "candidate": candidate.model_dump(mode="json")})

    failures = list(eligibility)
    if not required <= {case.suite_name for case in ordered}:
        failures.append("missing_required_suite")
    if any(not outcome.passed for outcome in outcomes):
        failures.append("case_failed")
    failures = sorted(set(failures))
    manifest = {case.suite_name: case.suite_version for case in ordered}
    suite_hash = _hash({"manifest": manifest, "cases": [case.model_dump(mode="json") for case in ordered]})
    config_hash = _hash(policy.model_dump(mode="json"))
    replay_hash = _hash({"implementation": engine.implementation_version, "model": engine.model_version})
    runtime_hash = _hash(engine.runtime_config)
    input_hash = _hash({"snapshot": snapshot, "suite_hash": suite_hash, "config_hash": config_hash,
                        "replay_hash": replay_hash, "runtime_hash": runtime_hash,
                        "evaluated_at": evaluated_at.isoformat()})
    output_hash = _hash({"raw": raw, "outcomes": [o.model_dump(mode="json") for o in outcomes],
                         "failures": failures})
    created_at = datetime.now(timezone.utc)
    evaluation_id = f"ce_{uuid.uuid4().hex}"
    result = EvaluationResult(evaluation_id=evaluation_id, candidate_id=candidate_id,
                              candidate_version=candidate_version, runner_version=policy.runner_version,
                              policy_version=policy.policy_version, suite_manifest=manifest, outcomes=outcomes,
                              failure_codes=failures, suite_hash=suite_hash, config_hash=config_hash,
                              replay_hash=replay_hash, runtime_hash=runtime_hash, input_hash=input_hash,
                              output_hash=output_hash, passed=not failures, evaluated_at=evaluated_at,
                              created_at=created_at)
    record = EvaluationRecord(
        evaluation_id=evaluation_id, candidate_id=candidate_id, candidate_version=candidate_version,
        runner_version=policy.runner_version, policy_version=policy.policy_version,
        suite_manifest_json=_canonical_json(manifest),
        baseline_outputs_json=_canonical_json([item["baseline"] for item in raw]),
        candidate_outputs_json=_canonical_json([item["candidate"] for item in raw]),
        scores_json=_canonical_json([outcome.model_dump(mode="json") for outcome in outcomes]),
        safety_assertions_json=_canonical_json([[a.model_dump(mode="json") for a in o.assertions] for o in outcomes]),
        failure_codes_json=_canonical_json(failures), suite_hash=suite_hash, config_hash=config_hash,
        replay_hash=replay_hash, runtime_hash=runtime_hash, input_hash=input_hash, output_hash=output_hash,
        passed=not failures, evaluated_at=evaluated_at.isoformat(), created_at=created_at.isoformat())
    with scoped_write() as conn:
        insert_evaluation(conn, record)
    return result


def evaluation_readiness(candidate_id: str, candidate_version: int, *, policy: EvaluationPolicy | None = None,
                         at: datetime | None = None, engine: ReplayEngine | None = None) -> EvaluationReadiness:
    """Validate the latest immutable evaluation against current governance and policy state."""
    policy = policy or EvaluationPolicy()
    at = _normalize_evaluation_time(at or datetime.now(timezone.utc))
    with scoped_read() as conn:
        snapshot = candidate_snapshot(conn, candidate_id, candidate_version)
        latest = latest_evaluation(conn, candidate_id, candidate_version)
    reasons = _eligibility(snapshot, at)
    if latest is None or not latest.passed:
        reasons.append("no_passing_required_evaluation")
    else:
        if latest.runner_version != policy.runner_version or latest.policy_version != policy.policy_version:
            reasons.append("evaluation_version_stale")
        if latest.config_hash != _hash(policy.model_dump(mode="json")):
            reasons.append("evaluation_config_stale")
        runtime_reason = runtime_readiness_reason(latest, engine)
        if runtime_reason:
            reasons.append(runtime_reason)
        stored = json.loads(latest.suite_manifest_json)
        required = effective_required_suites(snapshot, policy)
        all_current_cases = default_cases() + load_configured_cases(configured_private_corpus())
        validate_case_manifest(all_current_cases)
        current_cases = _selected_cases(all_current_cases, required)
        current_manifest = {case.suite_name: case.suite_version for case in current_cases}
        current_suite_hash = _hash({"manifest": current_manifest, "cases": [case.model_dump(mode="json") for case in current_cases]})
        if not required <= set(stored) or latest.suite_hash != current_suite_hash:
            reasons.append("required_suite_stale")
    return EvaluationReadiness(ready=not reasons, reasons=sorted(set(reasons)), evaluation_id=latest.evaluation_id if latest and not reasons else None)
