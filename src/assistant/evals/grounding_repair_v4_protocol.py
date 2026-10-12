"""Immutable contracts for Policy-v4 grounding-repair research screening.

Artifacts defined here are metadata-only unless a field is explicitly an encrypted blob.
Questions, answers, evidence, sources, rationales, and per-case feature rows must never be
written as plaintext by this protocol.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .artifacts import content_hash

POLICY_V4 = "workspace-grounding-repair-certificate-v4"
QUESTION_COUNT = 420
REPAIR_CASE_TARGET = 200


class V4Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResearchScreenStatus(str, Enum):
    PASSED = "research_screen_passed_shadow_research_only"
    FAILED_SAFETY = "research_screen_failed_safety"
    INCONCLUSIVE = "inconclusive_or_utility_failed"
    INCOMPLETE = "incomplete_no_decision"


class EncryptedBlob(V4Model):
    ciphertext: str = Field(min_length=1)
    nonce: str = Field(min_length=1)
    key_version: str = Field(min_length=1)
    payload_hash: str = Field(min_length=32)


class HistoricalExclusionManifest(V4Model):
    schema_version: Literal[1] = 1
    generated_at: datetime
    database_fingerprint: str = Field(min_length=8)
    artifact_root_hashes: list[str] = Field(default_factory=list)
    request_hashes: list[str]
    unreadable_encrypted_run_start_count: int = Field(default=0, ge=0)
    content_hash: str = ""

    @field_validator("request_hashes")
    @classmethod
    def unique_sorted_hashes(cls, value: list[str]) -> list[str]:
        normalized = sorted(set(value))
        if normalized != value:
            raise ValueError("historical request hashes must be unique and sorted")
        return value


class QuestionIndexItem(V4Model):
    order: int = Field(ge=1, le=QUESTION_COUNT)
    question_id: str = Field(pattern=r"^v4q_[a-f0-9]{16}$")
    question_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    request_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    encrypted_payload_hash: str = Field(min_length=32)


class QuestionManifest(V4Model):
    schema_version: Literal[1] = 1
    question_set_id: str = Field(min_length=3)
    generated_at: datetime
    collection_class: Literal[
        "operator_authorized_controlled_live_production_workspace_simulation_v4"
    ] = "operator_authorized_controlled_live_production_workspace_simulation_v4"
    evidence_origin: Literal["controlled"] = "controlled"
    web_enabled: Literal[False] = False
    deterministic_order: Literal[True] = True
    question_count: Literal[420] = QUESTION_COUNT
    database_fingerprint: str = Field(min_length=8)
    historical_exclusion_hash: str = Field(min_length=8)
    encrypted_payload_artifact_hash: str = Field(min_length=8)
    questions: list[QuestionIndexItem]
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_layout(self) -> "QuestionManifest":
        if len(self.questions) != QUESTION_COUNT:
            raise ValueError("Policy-v4 question manifest must contain exactly 420 turns")
        if [item.order for item in self.questions] != list(range(1, QUESTION_COUNT + 1)):
            raise ValueError("Policy-v4 questions must use a contiguous sealed order")
        for field in ("question_id", "question_hash", "request_hash"):
            values = [getattr(item, field) for item in self.questions]
            if len(values) != len(set(values)):
                raise ValueError(f"Policy-v4 questions contain duplicate {field}")
        return self


class EncryptedQuestionEntry(V4Model):
    order: int = Field(ge=1, le=QUESTION_COUNT)
    question_id: str = Field(pattern=r"^v4q_[a-f0-9]{16}$")
    payload: EncryptedBlob


class EncryptedQuestionSet(V4Model):
    schema_version: Literal[1] = 1
    question_set_id: str = Field(min_length=3)
    generated_at: datetime
    entries: list[EncryptedQuestionEntry]
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_entries(self) -> "EncryptedQuestionSet":
        if len(self.entries) != QUESTION_COUNT:
            raise ValueError("encrypted Policy-v4 question set must contain exactly 420 entries")
        if [item.order for item in self.entries] != list(range(1, QUESTION_COUNT + 1)):
            raise ValueError("encrypted question order must be contiguous")
        return self


class GateDefinition(V4Model):
    repair_case_target: Literal[200] = REPAIR_CASE_TARGET
    distinct_request_target: Literal[200] = REPAIR_CASE_TARGET
    automated_review_coverage: Literal[1.0] = 1.0
    minimum_selected_cases: Literal[20] = 20
    minimum_projected_repair_reduction: Literal[0.10] = 0.10
    maximum_false_positives: Literal[0] = 0
    maximum_selected_candidate_issue_count: Literal[0] = 0
    maximum_certificate_p95_ms: Literal[5.0] = 5.0
    maximum_full_replay_p95_ms: Literal[5.0] = 5.0
    candidate_minimum_score: Literal[80] = 80
    candidate_noninferiority_margin: Literal[3] = 3


class Preregistration(V4Model):
    schema_version: Literal[1] = 1
    experiment_id: str = Field(min_length=3)
    sealed_at: datetime
    policy_version: Literal[POLICY_V4] = POLICY_V4
    research_only: Literal[True] = True
    serving_change_authorized: Literal[False] = False
    sample_rule: Literal[
        "first_200_complete_repair_triggered_cases_from_exactly_420_sealed_turns"
    ] = "first_200_complete_repair_triggered_cases_from_exactly_420_sealed_turns"
    insufficient_sample_rule: Literal[
        "insufficient_sample_no_same_experiment_question_generation"
    ] = "insufficient_sample_no_same_experiment_question_generation"
    safe_label_rule: Literal[
        "candidate_score_gte_80_and_no_candidate_issues_and_candidate_gte_baseline_minus_3"
    ] = "candidate_score_gte_80_and_no_candidate_issues_and_candidate_gte_baseline_minus_3"
    implementation_hash: str = Field(min_length=8)
    rubric_hash: str = Field(min_length=8)
    reference_prompt_hashes: list[str] = Field(min_length=1)
    judge_prompt_hash: str = Field(min_length=8)
    model_identity_hash: str = Field(min_length=8)
    question_manifest_hash: str = Field(min_length=8)
    historical_exclusion_hash: str = Field(min_length=8)
    gates: GateDefinition = Field(default_factory=GateDefinition)
    content_hash: str = ""


class HoldoutCase(V4Model):
    sealed_order: int = Field(ge=1, le=QUESTION_COUNT)
    question_id: str
    question_hash: str
    request_hash: str
    attempt_count: int = Field(ge=1)
    complete: bool
    repair_triggered: bool
    encrypted_case_payload: EncryptedBlob | None = None
    case_payload_hash: str | None = None
    run_id_hash: str | None = None
    turn_id_hash: str | None = None
    trace_id_hash: str | None = None
    event_id_hash: str | None = None
    served_answer_legacy: bool = False
    user_visible_behavior_unchanged: bool = False
    business_persistence_unchanged: bool = False
    thread_created_count: int = Field(default=0, ge=0)
    thread_archived_count: int = Field(default=0, ge=0)
    thread_archived: bool = False
    temporary_session_revoked: bool = False
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_complete_case(self) -> "HoldoutCase":
        if self.complete and (self.encrypted_case_payload is None or not self.case_payload_hash):
            raise ValueError("complete holdout cases require encrypted payload and hash")
        return self


class HoldoutCollection(V4Model):
    schema_version: Literal[1] = 1
    experiment_id: str
    question_manifest_hash: str
    preregistration_hash: str
    database_clone_fingerprint: str
    started_at: datetime
    completed_at: datetime
    case_count: Literal[420] = QUESTION_COUNT
    business_fingerprint_before: str
    business_fingerprint_after: str
    business_persistence_unchanged: bool
    temporary_session_count: int = Field(ge=0)
    temporary_session_revoked_count: int = Field(ge=0)
    thread_created_count: int = Field(ge=0)
    thread_archived_count: int = Field(ge=0)
    cases: list[HoldoutCase]
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_cases(self) -> "HoldoutCollection":
        if len(self.cases) != QUESTION_COUNT:
            raise ValueError("Policy-v4 holdout must account for all 420 sealed turns")
        if [item.sealed_order for item in self.cases] != list(range(1, QUESTION_COUNT + 1)):
            raise ValueError("holdout cases must preserve sealed order")
        return self


class SelectionManifest(V4Model):
    schema_version: Literal[1] = 1
    experiment_id: str
    generated_at: datetime
    holdout_hash: str
    selection_rule: Literal["first_200_complete_repair_triggered_in_sealed_order"] = (
        "first_200_complete_repair_triggered_in_sealed_order"
    )
    status: Literal["complete", "insufficient_sample"]
    eligible_repair_case_count: int = Field(ge=0)
    selected_case_count: int = Field(ge=0, le=REPAIR_CASE_TARGET)
    selected_question_hashes: list[str]
    selected_case_payload_hashes: list[str]
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_selection(self) -> "SelectionManifest":
        expected = REPAIR_CASE_TARGET if self.status == "complete" else min(
            REPAIR_CASE_TARGET, self.eligible_repair_case_count
        )
        if self.selected_case_count != expected:
            raise ValueError("selected case count does not match locked sample rule")
        if len(self.selected_question_hashes) != self.selected_case_count:
            raise ValueError("selected question hash count mismatch")
        if len(self.selected_case_payload_hashes) != self.selected_case_count:
            raise ValueError("selected payload hash count mismatch")
        return self


class AutomatedCaseReview(V4Model):
    question_hash: str
    case_payload_hash: str
    complete: bool
    candidate_score: float | None = Field(default=None, ge=0, le=100)
    baseline_score: float | None = Field(default=None, ge=0, le=100)
    candidate_issue_codes: list[str] = Field(default_factory=list)
    baseline_issue_codes: list[str] = Field(default_factory=list)
    preferred_answer: Literal["candidate", "baseline", "tie", "incomplete"]
    reference_hash: str | None = None
    evaluator_output_hash: str | None = None
    assignment_hash: str | None = None
    model_identity_hash: str
    execution_model_hash: str | None = None
    reference_mode: str | None = None
    reference_chunk_count: int = Field(default=0, ge=0)
    attempt_count: int = Field(default=0, ge=0)
    transport_retry_count: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)
    latency_ms: float = Field(default=0, ge=0)
    error_code: str | None = None


class AutomatedReviewBatch(V4Model):
    schema_version: Literal[1] = 1
    experiment_id: str
    generated_at: datetime
    selection_hash: str
    review_count: int = Field(ge=0, le=REPAIR_CASE_TARGET)
    reviews: list[AutomatedCaseReview]
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_reviews(self) -> "AutomatedReviewBatch":
        if len(self.reviews) != self.review_count:
            raise ValueError("review count mismatch")
        hashes = [item.question_hash for item in self.reviews]
        if len(hashes) != len(set(hashes)):
            raise ValueError("duplicate reviewed question hash")
        return self


class ExperimentReport(V4Model):
    schema_version: Literal[1] = 1
    experiment_id: str
    generated_at: datetime
    status: ResearchScreenStatus
    preregistration_hash: str
    question_manifest_hash: str
    historical_exclusion_hash: str
    holdout_hash: str
    selection_hash: str
    review_hash: str | None = None
    metrics: dict[str, int | float | bool | None]
    gates: dict[str, bool]
    research_only: Literal[True] = True
    serving_change_authorized: Literal[False] = False
    content_hash: str = ""


class VerificationReport(V4Model):
    schema_version: Literal[1] = 1
    experiment_id: str
    generated_at: datetime
    report_hash: str
    checks: dict[str, bool]
    all_passed: bool
    failure_codes: list[str] = Field(default_factory=list)
    content_hash: str = ""


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized_request_hash(value: str) -> str:
    return sha256_text(" ".join(str(value).split()))


def stamp_v4(model: V4Model) -> V4Model:
    return model.model_copy(update={"content_hash": content_hash(model)})


def verify_v4(model: V4Model) -> bool:
    stored = str(getattr(model, "content_hash", "") or "")
    return bool(stored) and stored == content_hash(model)


def hash_identity(value: Any) -> str:
    return sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


__all__ = [
    "AutomatedCaseReview", "AutomatedReviewBatch", "EncryptedBlob",
    "EncryptedQuestionEntry", "EncryptedQuestionSet", "ExperimentReport",
    "GateDefinition", "HistoricalExclusionManifest", "HoldoutCase",
    "HoldoutCollection", "POLICY_V4", "Preregistration", "QUESTION_COUNT",
    "QuestionIndexItem", "QuestionManifest", "REPAIR_CASE_TARGET",
    "ResearchScreenStatus", "SelectionManifest", "VerificationReport",
    "hash_identity", "normalized_request_hash", "sha256_text", "stamp_v4", "verify_v4",
]
