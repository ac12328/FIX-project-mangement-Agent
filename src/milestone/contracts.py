"""Typed loader for the versioned milestone operational contract."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, model_validator

from config.settings import BASE_DIR
from src.milestone.state_machine import MILESTONE_SEQUENCE
from src.models.enums import BallWith, Milestone
from src.models.progress_assessment import EvidenceLevel


_CONTRACT_PATH = (
    BASE_DIR / "wiki" / "playbook" / "fix-connectivity" / "milestone-contracts.json"
)
_SUPPORTED_SCHEMA_VERSIONS = frozenset({"1"})
_SUPPORTED_POLICY_VERSIONS = frozenset({"1", "2"})


class MilestoneContractLoadError(RuntimeError):
    """The operational milestone policy cannot be loaded safely."""


class MilestoneContractLookupError(ValueError):
    """A requested milestone or criterion does not exist in the policy."""


class _ContractModel(BaseModel):
    # JSON enum fields accept their canonical string values; primitive contract
    # fields use StrictBool/StrictInt to reject coercion.
    model_config = ConfigDict(extra="forbid")


class EvidenceLevelDefinition(_ContractModel):
    rank: Annotated[StrictInt, Field(ge=1)]
    description: str = Field(min_length=1)


class CanonicalSemantics(_ContractModel):
    current_milestone_definition: str = Field(min_length=1)
    milestone_is_not_status: StrictBool
    milestone_is_not_ball_with: StrictBool
    milestone_is_not_next_action: StrictBool
    m8_does_not_imply_completed: StrictBool
    m8_implies_completed: StrictBool = False
    lower_layer_blockers_do_not_regress_milestone: StrictBool
    stage_defaults_are_advisory_only: StrictBool
    all_inferred_milestone_changes_require_operator_review: StrictBool


class GlobalEvidencePolicy(_ContractModel):
    acceptable_modalities_for_exit: list[str]
    insufficient_modalities_for_exit: list[str]
    source_requirements: list[str]


class InitialTransitionPolicy(_ContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_milestone: None = Field(alias="from")
    to: Milestone
    allowed: StrictBool


class RulePolicy(_ContractModel):
    rule: str = Field(min_length=1)


class GlobalRollback(_ContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_milestone: Milestone = Field(alias="from")
    to: Milestone
    reason: str = Field(min_length=1)
    operator_review_required: StrictBool


class RollbackPolicy(_ContractModel):
    allowed: list[GlobalRollback]


class BackwardTransitionPolicy(_ContractModel):
    allowed: StrictBool


class GlobalTransitionPolicy(_ContractModel):
    initial_transition: InitialTransitionPolicy
    normal_transition: RulePolicy
    jump_transition: RulePolicy
    rollback_transition: RollbackPolicy
    all_other_backward_transitions: BackwardTransitionPolicy


class PctNotApplicablePolicy(_ContractModel):
    per_step_record_required: StrictBool
    required_fields: list[str]
    blanket_not_applicable_for_all_steps_forbidden: StrictBool
    approval_authorities: list[str]


class PctScopePolicy(_ContractModel):
    scope_source_required: StrictBool
    accepted_scope_sources: list[str]
    standard_connectivity_steps: list[str]
    standard_order_flow_steps: list[str]
    not_applicable_policy: PctNotApplicablePolicy


class ContractCriterion(_ContractModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    predicate: str | None = None
    minimum_evidence_level: EvidenceLevel | None = None
    accepted_evidence_predicates: list[str] = Field(default_factory=list)
    named_authority_required_for_signoff: StrictBool | None = None
    signoff_authorities: list[str] = Field(default_factory=list)


class AcceptedEvidencePolicy(_ContractModel):
    minimum_exit_evidence_level: EvidenceLevel
    acceptable_sources: list[str]


class AcceptanceAuthority(_ContractModel):
    primary: list[str]
    external_confirmation_required: StrictBool
    operator_review_required_for_inferred_advance: StrictBool
    operator_approval_required_for_terminal_status: StrictBool | None = None


class RevertTransition(_ContractModel):
    to: Milestone
    condition: str = Field(min_length=1)
    operator_review_required: StrictBool


class AllowedTransitions(_ContractModel):
    next: list[Milestone]
    revert: list[RevertTransition] = Field(default_factory=list)


class MilestoneContract(_ContractModel):
    id: Milestone
    display_name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    entry_criteria: list[ContractCriterion]
    in_progress_signals: list[str]
    required_exit_criteria: list[ContractCriterion]
    optional_exit_criteria: list[ContractCriterion]
    close_criteria: list[ContractCriterion] = Field(default_factory=list)
    accepted_evidence: AcceptedEvidencePolicy
    insufficient_evidence_signals: list[str]
    contradiction_signals: list[str]
    acceptance_authority: AcceptanceAuthority
    typical_actions: list[str]
    typical_blockers: list[str]
    allowed_transitions: AllowedTransitions


class MilestoneContractPolicy(_ContractModel):
    schema_version: Literal["1"]
    policy_version: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    canonical_semantics: CanonicalSemantics
    evidence_levels: dict[EvidenceLevel, EvidenceLevelDefinition]
    global_evidence_policy: GlobalEvidencePolicy
    global_transition_policy: GlobalTransitionPolicy
    pct_scope_policy: PctScopePolicy
    milestones: list[MilestoneContract]

    @model_validator(mode="after")
    def _validate_policy_integrity(self) -> "MilestoneContractPolicy":
        if self.policy_version not in _SUPPORTED_POLICY_VERSIONS:
            raise ValueError(f"unsupported policy version: {self.policy_version}")

        transitions = self.global_transition_policy
        if (
            transitions.initial_transition.from_milestone is not None
            or transitions.initial_transition.to != Milestone.M1_NETWORK_SETUP
            or not transitions.initial_transition.allowed
        ):
            raise ValueError("initial transition must be allowed from None to M1")
        if transitions.all_other_backward_transitions.allowed:
            raise ValueError("backward milestone transitions must be blocked")
        if transitions.rollback_transition.allowed:
            raise ValueError("global policy must not contain rollback transitions")

        expected = set(Milestone)
        ids = [contract.id for contract in self.milestones]
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise ValueError("milestone contracts must contain each enum value exactly once")

        criterion_ids: list[str] = []
        known_levels = set(self.evidence_levels)
        for contract in self.milestones:
            groups = (
                contract.entry_criteria,
                contract.required_exit_criteria,
                contract.optional_exit_criteria,
                contract.close_criteria,
            )
            for criterion in (item for group in groups for item in group):
                criterion_ids.append(criterion.id)
                if (
                    criterion.minimum_evidence_level is not None
                    and criterion.minimum_evidence_level not in known_levels
                ):
                    raise ValueError(
                        f"unknown evidence level for criterion {criterion.id}"
                    )
            for criterion in contract.required_exit_criteria:
                if not criterion.predicate or not criterion.accepted_evidence_predicates:
                    raise ValueError(
                        f"required exit criterion {criterion.id} needs predicate evidence"
                    )
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("criterion IDs must be globally unique")

        by_id = {contract.id: contract for contract in self.milestones}
        for index, milestone in enumerate(MILESTONE_SEQUENCE):
            expected_next = (
                [MILESTONE_SEQUENCE[index + 1]]
                if index + 1 < len(MILESTONE_SEQUENCE)
                else []
            )
            if by_id[milestone].allowed_transitions.next != expected_next:
                raise ValueError(f"invalid next transition for {milestone.value}")

        reverts = [
            (contract.id, revert)
            for contract in self.milestones
            for revert in contract.allowed_transitions.revert
        ]
        if reverts:
            raise ValueError("backward milestone transitions are not allowed")

        m8 = by_id[Milestone.M8_GO_LIVE]
        if m8.required_exit_criteria or not m8.close_criteria:
            raise ValueError("M8 must have completion criteria and no normal exit criteria")
        if not self.canonical_semantics.m8_implies_completed:
            raise ValueError("M8 must imply Completed status")

        ranks = [definition.rank for definition in self.evidence_levels.values()]
        if len(ranks) != len(set(ranks)):
            raise ValueError("evidence-level ranks must be unique")
        return self


@lru_cache(maxsize=1)
def load_milestone_contract_policy() -> MilestoneContractPolicy:
    """Load and validate the process-lifetime operational policy snapshot."""
    try:
        raw = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
        schema_version = str(raw.get("schema_version", ""))
        if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported schema version: {schema_version}")
        return MilestoneContractPolicy.model_validate(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise MilestoneContractLoadError(
            f"Unable to load milestone contract from {_CONTRACT_PATH}: {exc}"
        ) from exc


def clear_milestone_contract_cache() -> None:
    load_milestone_contract_policy.cache_clear()


def _normalize_milestone(value: Milestone | str) -> Milestone:
    try:
        return value if isinstance(value, Milestone) else Milestone(value)
    except (TypeError, ValueError) as exc:
        raise MilestoneContractLookupError(f"Unknown milestone: {value!r}") from exc


def get_milestone_contract(value: Milestone | str) -> MilestoneContract:
    milestone = _normalize_milestone(value)
    for contract in load_milestone_contract_policy().milestones:
        if contract.id == milestone:
            return contract
    raise MilestoneContractLookupError(f"Contract missing for {milestone.value}")


def get_next_milestone_contract(
    value: Milestone | str,
) -> MilestoneContract | None:
    contract = get_milestone_contract(value)
    if not contract.allowed_transitions.next:
        return None
    return get_milestone_contract(contract.allowed_transitions.next[0])


def get_contract_criterion(
    value: Milestone | str,
    criterion_id: str,
) -> ContractCriterion:
    contract = get_milestone_contract(value)
    groups = (
        contract.entry_criteria,
        contract.required_exit_criteria,
        contract.optional_exit_criteria,
        contract.close_criteria,
    )
    for criterion in (item for group in groups for item in group):
        if criterion.id == criterion_id:
            return criterion
    raise MilestoneContractLookupError(
        f"Criterion {criterion_id!r} not found for {contract.id.value}"
    )

