"""Typed contracts for controlled live Workspace simulation questions."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import EvalCase, QuestionReviewDecision

COLLECTION_CLASS = "operator_authorized_controlled_live_production_workspace_simulation"
EVIDENCE_ORIGIN = "controlled"


class SimulationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SimulationScenarioClass(str, Enum):
    NORMAL = "normal"
    STRESS_GROUNDING = "stress_grounding"


class SimulationSourceKind(str, Enum):
    APPROVED_ANCHOR = "approved_anchor"
    MINED_ROOT = "mined_root"
    GENERATED_FOLLOW_UP = "generated_follow_up"


def question_hash(question: str) -> str:
    normalized = " ".join(question.split())
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _require_aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


class SimulationMiningRound(SimulationModel):
    round_index: int = Field(ge=1, le=3)
    batch_id: str = Field(min_length=3)
    cases: list[EvalCase]

    @model_validator(mode="after")
    def validate_round(self) -> "SimulationMiningRound":
        if len(self.cases) != 12:
            raise ValueError("each simulation mining round must contain exactly 12 cases")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("simulation mining round contains duplicate case IDs")
        return self


class SimulationCandidateBatch(SimulationModel):
    schema_version: Literal[1] = 1
    question_set_id: str = Field(min_length=3)
    as_of: datetime
    created_at: datetime
    fixture_fingerprint: str = Field(min_length=8)
    anchor_source_content_hash: str = Field(min_length=8)
    rounds: list[SimulationMiningRound]
    content_hash: str = ""

    @field_validator("as_of", "created_at")
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        return _require_aware(value, "simulation candidate timestamp")

    @model_validator(mode="after")
    def validate_candidates(self) -> "SimulationCandidateBatch":
        if [item.round_index for item in self.rounds] != [1, 2, 3]:
            raise ValueError("simulation candidate batch requires mining rounds 1, 2, and 3")
        cases = [case for item in self.rounds for case in item.cases]
        if len(cases) != 36:
            raise ValueError("simulation candidate batch must contain exactly 36 roots")
        ids = [case.case_id for case in cases]
        questions = [" ".join(case.question.casefold().split()) for case in cases]
        if len(ids) != len(set(ids)) or len(questions) != len(set(questions)):
            raise ValueError("simulation candidates must have unique IDs and questions")
        return self


class SimulationTurn(SimulationModel):
    schema_version: Literal[1] = 1
    turn_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,99}$")
    conversation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,99}$")
    turn_index: int = Field(ge=1, le=3)
    question: str = Field(min_length=3)
    question_hash: str = Field(min_length=8)
    source_kind: SimulationSourceKind
    source_case_id: str = Field(min_length=3)
    root_turn_id: str | None = None
    scenario_class: SimulationScenarioClass
    scope_mode: Literal["portfolio", "projects"] = "portfolio"
    project_ids: list[str] = Field(default_factory=list)
    web_enabled: Literal[False] = False
    intent_type: Literal["aggregate_query"] = "aggregate_query"
    expected_tool: str | None = None
    source_signal_ids: list[str] = Field(default_factory=list)

    @field_validator("project_ids", "source_signal_ids")
    @classmethod
    def unique_strings(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @model_validator(mode="after")
    def validate_turn(self) -> "SimulationTurn":
        if self.question_hash != question_hash(self.question):
            raise ValueError("question_hash does not match question")
        if self.scope_mode == "portfolio" and self.project_ids:
            raise ValueError("portfolio simulation turns cannot pin project_ids")
        if self.scope_mode == "projects" and not self.project_ids:
            raise ValueError("project-scoped simulation turns require project_ids")
        if self.turn_index == 1:
            if self.source_kind == SimulationSourceKind.GENERATED_FOLLOW_UP:
                raise ValueError("conversation roots cannot be follow-ups")
            if self.root_turn_id not in (None, self.turn_id):
                raise ValueError("root_turn_id must be absent or self-referential on roots")
        else:
            if self.source_kind != SimulationSourceKind.GENERATED_FOLLOW_UP:
                raise ValueError("turns after the root must be generated follow-ups")
            if not self.root_turn_id:
                raise ValueError("follow-up turns require root_turn_id")
        return self


class SimulationConversation(SimulationModel):
    schema_version: Literal[1] = 1
    conversation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,99}$")
    scenario_class: SimulationScenarioClass
    root_case_id: str = Field(min_length=3)
    turns: list[SimulationTurn]

    @model_validator(mode="after")
    def validate_conversation(self) -> "SimulationConversation":
        if len(self.turns) not in {1, 3}:
            raise ValueError("simulation conversations must contain one or three turns")
        if [turn.turn_index for turn in self.turns] != list(range(1, len(self.turns) + 1)):
            raise ValueError("simulation turn indexes must be contiguous and ordered")
        if any(turn.conversation_id != self.conversation_id for turn in self.turns):
            raise ValueError("turn conversation_id does not match its parent")
        if any(turn.scenario_class != self.scenario_class for turn in self.turns):
            raise ValueError("all turns in a conversation must share scenario_class")
        root = self.turns[0]
        if root.source_case_id != self.root_case_id:
            raise ValueError("conversation root_case_id does not match its root turn")
        if any(turn.root_turn_id != root.turn_id for turn in self.turns[1:]):
            raise ValueError("follow-up root_turn_id does not match the conversation root")
        return self


def validate_question_layout(conversations: list[SimulationConversation]) -> None:
    if len(conversations) != 42:
        raise ValueError("simulation question set must contain exactly 42 conversations")
    turns = [turn for conversation in conversations for turn in conversation.turns]
    if len(turns) != 54:
        raise ValueError("simulation question set must contain exactly 54 turns")
    if sum(len(item.turns) == 3 for item in conversations) != 6:
        raise ValueError("simulation question set must contain exactly six three-turn conversations")
    source_counts = {
        kind: sum(turn.source_kind == kind for turn in turns)
        for kind in SimulationSourceKind
    }
    if source_counts[SimulationSourceKind.APPROVED_ANCHOR] != 12:
        raise ValueError("simulation question set must contain exactly 12 approved anchors")
    if source_counts[SimulationSourceKind.MINED_ROOT] != 30:
        raise ValueError("simulation question set must contain exactly 30 mined roots")
    if source_counts[SimulationSourceKind.GENERATED_FOLLOW_UP] != 12:
        raise ValueError("simulation question set must contain exactly 12 generated follow-ups")
    scenario_counts = {
        scenario: sum(turn.scenario_class == scenario for turn in turns)
        for scenario in SimulationScenarioClass
    }
    if scenario_counts != {
        SimulationScenarioClass.NORMAL: 38,
        SimulationScenarioClass.STRESS_GROUNDING: 16,
    }:
        raise ValueError("simulation turns must preserve the agreed 38/16 normal-to-stress mix")
    ids = [turn.turn_id for turn in turns]
    hashes = [turn.question_hash for turn in turns]
    if len(ids) != len(set(ids)) or len(hashes) != len(set(hashes)):
        raise ValueError("simulation turns must have unique IDs and question hashes")


class SimulationQuestionDraftBatch(SimulationModel):
    schema_version: Literal[1] = 1
    question_set_id: str = Field(min_length=3)
    as_of: datetime
    created_at: datetime
    anchor_source_content_hash: str = Field(min_length=8)
    candidate_content_hash: str = Field(min_length=8)
    conversations: list[SimulationConversation]
    content_hash: str = ""

    @field_validator("as_of", "created_at")
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        return _require_aware(value, "simulation draft timestamp")

    @model_validator(mode="after")
    def validate_layout(self) -> "SimulationQuestionDraftBatch":
        validate_question_layout(self.conversations)
        return self


class SimulationQuestionReviewItem(SimulationModel):
    turn_id: str
    question: str = Field(min_length=3)
    question_hash: str = Field(min_length=8)
    decision: QuestionReviewDecision = QuestionReviewDecision.PENDING
    edited_question: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def validate_review_item(self) -> "SimulationQuestionReviewItem":
        if self.question_hash != question_hash(self.question):
            raise ValueError("review question_hash does not match question")
        if self.decision == QuestionReviewDecision.EDIT and not str(self.edited_question or "").strip():
            raise ValueError("edited_question is required when decision is edit")
        return self


class SimulationQuestionReviewBatch(SimulationModel):
    schema_version: Literal[1] = 1
    question_set_id: str
    source_content_hash: str
    reviewed_by: str = ""
    reviewed_at: datetime | None = None
    review_notes: str = ""
    decisions: list[SimulationQuestionReviewItem]
    content_hash: str = ""

    @field_validator("reviewed_at")
    @classmethod
    def aware_review_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value, "simulation review timestamp")

    @model_validator(mode="after")
    def validate_decisions(self) -> "SimulationQuestionReviewBatch":
        if len(self.decisions) != 42:
            raise ValueError("simulation review must contain exactly 42 newly generated turns")
        ids = [item.turn_id for item in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("simulation review contains duplicate turn IDs")
        return self


class ControlledLiveSimulationQuestionSet(SimulationModel):
    schema_version: Literal[1] = 1
    collection_class: Literal[COLLECTION_CLASS] = COLLECTION_CLASS
    evidence_origin: Literal[EVIDENCE_ORIGIN] = EVIDENCE_ORIGIN
    question_set_id: str = Field(min_length=3)
    as_of: datetime
    approved_at: datetime
    approved_by: str = Field(min_length=1)
    review_notes: str = ""
    source_content_hash: str = Field(min_length=8)
    review_content_hash: str = Field(min_length=8)
    conversations: list[SimulationConversation]
    content_hash: str = ""

    @field_validator("as_of", "approved_at")
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        return _require_aware(value, "approved simulation timestamp")

    @model_validator(mode="after")
    def validate_layout(self) -> "ControlledLiveSimulationQuestionSet":
        validate_question_layout(self.conversations)
        return self
