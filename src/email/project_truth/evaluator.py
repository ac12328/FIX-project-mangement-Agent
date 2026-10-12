"""Deterministic project-state evaluation for validated email facts."""

from __future__ import annotations

import re

from src.email.project_truth.citations import unique_source_quotes
from src.email.project_truth.contracts import (
    EmailProjectRecommendation,
    EmailProjectTruthCase,
    ValidatedEmailEvidence,
)
from src.email.project_truth.fix_evidence import enrich_validated_fix_session_facts
from src.email.sender_trust import sender_org_type
from src.milestone.contracts import (
    get_contract_criterion,
    get_milestone_contract,
    load_milestone_contract_policy,
)
from src.models.enums import ProjectStatus, RiskLevel
from src.models.progress_assessment import (
    AssessmentCode,
    AssessmentDecision,
    CriterionResult,
    EvidenceModality,
    EvidencePolarity,
    ExtractedProgressFact,
    NextActionProvenance,
)
from src.project_state.evaluator import (
    ProjectStateInput,
    assess_project_state,
    proposed_field_subset,
)


def _normalize_contract_fact_predicates(
    facts: list[ExtractedProgressFact],
    *,
    policy,
) -> list[ExtractedProgressFact]:
    """Repair a semantic criterion predicate only when its cited quote names one alias.

    The milestone contract separates a criterion's semantic label from the concrete
    evidence predicates accepted by the deterministic evaluator. An extractor can
    occasionally return the semantic label even though the source quote contains a
    concrete allowed predicate. Repair only that source-grounded, unambiguous case;
    otherwise leave the fact untouched so evaluation fails closed.
    """
    aliases_by_semantic: dict[str, tuple[str, ...]] = {}
    for contract in policy.milestones:
        for criterion in contract.required_exit_criteria:
            if criterion.predicate:
                aliases_by_semantic[criterion.predicate] = tuple(
                    criterion.accepted_evidence_predicates
                )

    normalized: list[ExtractedProgressFact] = []
    for fact in facts:
        aliases = aliases_by_semantic.get(fact.predicate)
        if not aliases or fact.predicate in aliases:
            normalized.append(fact)
            continue
        matched = {
            alias
            for alias in aliases
            if any(
                re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])",
                    citation.quote,
                )
                for citation in fact.citations
            )
        }
        if len(matched) == 1:
            normalized.append(fact.model_copy(update={"predicate": matched.pop()}))
        else:
            normalized.append(fact)
    return normalized


def _missing_descriptions(case: EmailProjectTruthCase, assessment) -> list[str]:
    descriptions: list[str] = []
    milestone = case.project_snapshot.current_milestone
    if milestone is None:
        return descriptions
    for criterion in assessment.milestone_criteria:
        if criterion.result == CriterionResult.MET:
            continue
        try:
            description = get_contract_criterion(milestone, criterion.criterion_id).description
        except Exception:
            description = criterion.explanation
        if description and description not in descriptions:
            descriptions.append(description)
    return descriptions


def _state_change_coverage(assessment, proposed_fields: dict[str, str]) -> float:
    if not proposed_fields:
        return 1.0
    by_field = {
        "current_milestone": assessment.milestone_assessment,
        "project_status": assessment.status_assessment,
        "ball_with": assessment.ball_with_assessment,
        "next_action": assessment.next_action_assessment,
    }
    supported = 0
    for field in proposed_fields:
        item = by_field.get(field)
        if item is not None and item.citations:
            supported += 1
        elif field == "next_action" and getattr(item, "provenance", None) is not None:
            # Policy-derived follow-up is not a claim that email evidence completed a state.
            supported += 1
    return supported / len(proposed_fields)


def _merge_operational_obligations(
    facts: list[ExtractedProgressFact],
) -> list[ExtractedProgressFact]:
    """Combine complementary email requests for one accountable party into one action."""
    operational = {"explicit_obligation", "next_action_commitment"}
    grouped: dict[str, list[ExtractedProgressFact]] = {}
    passthrough: list[ExtractedProgressFact] = []
    for fact in facts:
        subject = (fact.subject or "").strip().casefold()
        if (
            fact.predicate in operational
            and subject in {"internal", "external", "shared"}
            and fact.citations
        ):
            grouped.setdefault(subject, []).append(fact)
        else:
            passthrough.append(fact)
    for subject, items in grouped.items():
        actions = list(dict.fromkeys(
            str(item.value).strip().rstrip(". ")
            for item in items
            if item.value is not None and str(item.value).strip()
        ))
        if not actions:
            passthrough.extend(items)
            continue
        passthrough.append(ExtractedProgressFact(
            predicate="explicit_obligation",
            value="; ".join(actions) + ".",
            subject=subject,
            citations=[citation for item in items for citation in item.citations],
            extraction_confidence=min(item.extraction_confidence for item in items),
        ))
    return passthrough


_GENERIC_ACTION_TOKENS = frozenset({
    "a", "an", "and", "as", "at", "advise", "check", "confirm", "email",
    "external", "for", "from", "in", "internal", "new", "next", "of", "on",
    "or", "order", "please", "project", "request", "result", "review", "showing",
    "submitted", "test", "that", "the", "then", "this", "to", "update", "with",
})
_PENDING_ACTION_TOKENS = frozenset({
    "arrange", "confirm", "execute", "obtain", "perform", "request", "run", "schedule",
    "send", "test", "validate", "wait",
})
_OBSERVED_RESULT_TOKENS = frozenset({
    "ack", "acknowledged", "cancelled", "completed", "connected", "heartbeat", "observed",
    "passed", "placed", "receipt", "received", "recorded",
})
_COMPLETION_MARKERS = frozenset({
    "accepted", "complete", "completed", "completion", "confirmed", "confirmation",
    "indicated", "verified",
})
_OPERATIONAL_PREDICATES = frozenset({"explicit_obligation", "next_action_commitment"})


def _action_tokens(value: str | None) -> set[str]:
    tokens: set[str] = set()
    for raw_token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value or ""):
        token = raw_token.casefold().strip("._-")
        if len(token) >= 2 and token not in _GENERIC_ACTION_TOKENS:
            tokens.add(token)
    return tokens


def _identifier_tokens(tokens: set[str]) -> set[str]:
    return {token for token in tokens if any(char.isdigit() for char in token)}


def _describes_progress_change(current: str, candidate: str) -> bool:
    current_words = set(re.findall(r"[a-z]+", current.casefold()))
    candidate_words = set(re.findall(r"[a-z]+", candidate.casefold()))
    return bool(current_words & _PENDING_ACTION_TOKENS) and bool(
        candidate_words & _OBSERVED_RESULT_TOKENS
    )


def _candidate_is_redundant_confirmation(current: str, candidate: str) -> bool:
    """Detect a weak reconfirmation when the current action already records confirmation."""
    candidate_text = candidate.strip().casefold()
    if not re.match(r"^(?:please\s+)?(?:confirm|check|verify)\b", candidate_text):
        return False
    current_words = set(re.findall(r"[a-z0-9]+", current.casefold()))
    if not current_words.intersection(_COMPLETION_MARKERS):
        return False
    candidate_topics = _action_tokens(candidate) - _COMPLETION_MARKERS
    current_topics = _action_tokens(current) - _COMPLETION_MARKERS
    return bool(candidate_topics & current_topics)


def _existing_action_is_more_specific(
    case: EmailProjectTruthCase,
    candidate_action: str,
) -> bool:
    """Keep a current client action only when it truly covers the newly supplied result."""
    current = case.project_snapshot.next_action or ""
    if not current:
        return False
    if _candidate_is_redundant_confirmation(current, candidate_action):
        return True
    if sender_org_type(case.sender_email) != "client":
        return False
    if _describes_progress_change(current, candidate_action):
        return False
    overlap = _action_tokens(current) & _action_tokens(candidate_action)
    has_identifier = bool(_identifier_tokens(overlap))
    # Require an identifier plus at least two meaningful shared domain tokens. Generic
    # glue words and one coincidental account ID must never suppress a progress update.
    return len(overlap) >= 3 and has_identifier and len(current) > len(candidate_action) * 0.6


_UNCERTAINTY_ACTION_PATTERN = re.compile(
    r"^(?:please\s+)?(?:confirm|check|verify)\s+(?:whether|if)\b",
    re.IGNORECASE,
)
_SOURCE_UNCERTAINTY_PATTERN = re.compile(
    r"(?:\?|\bwhether\b|\bif\b|\bunclear\b|\bnot\s+sure\b)",
    re.IGNORECASE,
)
_COMPLETION_OR_READINESS_PATTERN = re.compile(
    r"\b(?:complete|completed|confirmed|passed|approved|ready)\b|\bcan\s+go\s+live\b",
    re.IGNORECASE,
)
_INTERNAL_FIRST_PERSON_STATUS_REQUEST_PATTERN = re.compile(
    r"\b(?:may|can|could|would)\s+we\s+(?:please\s+)?"
    r"(?:know|have|get|receive)\b[^.!?\n]{0,160}\b(?:update|status|progress)\b",
    re.IGNORECASE,
)
_EXPLICIT_EXTERNAL_OWNER_PATTERN = re.compile(
    r"\b(?:client|customer|counterparty|vendor|they|them|their|your)\b",
    re.IGNORECASE,
)
_CONNECTIVITY_TOPIC_PATTERN = re.compile(
    r"\b(?:connection|connectivity|connect|session|vpn|tcp|fix)\b",
    re.IGNORECASE,
)
_FAILURE_STATE_PATTERN = re.compile(
    r"\b(?:fail|failed|failing|failure|unstable|instability|unavailable|down|error)\b|"
    r"\bcannot\s+connect\b|\bcan(?:not|'t)\s+connect\b",
    re.IGNORECASE,
)
_REMEDIATION_ACTION_PATTERN = re.compile(
    r"\b(?:investigate|restore|resolve|retry|repair|stabili[sz]e|troubleshoot|fix)\b",
    re.IGNORECASE,
)
_THIRD_PERSON_ACTOR_PATTERN = re.compile(
    r"\b(?:they|them|their|theirs)\b",
    re.IGNORECASE,
)
_DIRECT_ACCOUNTABLE_ACTOR_PATTERN = re.compile(
    r"\b(?:i|me|my|mine|we|us|our|ours|you|your|yours)\b",
    re.IGNORECASE,
)
_NAMED_INTERNAL_ROLE_PATTERN = re.compile(
    r"\b(?:b2b|fix|network|infrastructure|engineering|onboarding|operations?|ops|"
    r"project|settlement|support|trading)\s+(?:desk|manager|owner|team)\b|"
    r"\b(?:internal\s+(?:desk|owner|team)|project\s+manager)\b",
    re.IGNORECASE,
)


def _unsupported_reconfirmation_distortion(
    case: EmailProjectTruthCase,
    fact: ExtractedProgressFact,
) -> bool:
    """Reject an extractor-invented uncertainty when the source reports readiness.

    This is intentionally narrow: it only removes an operational obligation when the
    extracted action adds ``whether``/``if``, none of its exact citations contains
    uncertainty, and the authored message contains completion or readiness wording.
    The original validated fact remains available for source rendering and audit.
    """
    if fact.predicate not in _OPERATIONAL_PREDICATES or fact.value is None:
        return False
    if not _UNCERTAINTY_ACTION_PATTERN.match(str(fact.value).strip()):
        return False
    eligible_quotes = [
        citation.quote
        for citation in fact.citations
        if citation.modality != EvidenceModality.QUOTED_HISTORY
    ]
    if not eligible_quotes or any(_SOURCE_UNCERTAINTY_PATTERN.search(q) for q in eligible_quotes):
        return False
    return bool(_COMPLETION_OR_READINESS_PATTERN.search(case.newest_authored_body))


def _normalize_internal_status_request_ownership(
    case: EmailProjectTruthCase,
    facts: list[ExtractedProgressFact],
) -> tuple[list[ExtractedProgressFact], bool]:
    """Keep an internal first-person status request as an internal response obligation.

    A phrase such as ``May we know the update`` identifies the internal sender as the
    requester, not the external party as the next accountable actor. The repair is
    intentionally narrow and does not apply when the quote explicitly names an external
    owner or uses second/third-person external ownership language.
    """
    if sender_org_type(case.sender_email) != "internal":
        return facts, False

    normalized: list[ExtractedProgressFact] = []
    changed = False
    for fact in facts:
        eligible_quotes = [
            citation.quote
            for citation in fact.citations
            if citation.modality != EvidenceModality.QUOTED_HISTORY
        ]
        should_rebind = bool(
            fact.predicate in _OPERATIONAL_PREDICATES
            and (fact.subject or "").strip().casefold() == "external"
            and eligible_quotes
            and all(
                _INTERNAL_FIRST_PERSON_STATUS_REQUEST_PATTERN.search(quote)
                and not _EXPLICIT_EXTERNAL_OWNER_PATTERN.search(quote)
                for quote in eligible_quotes
            )
        )
        if should_rebind:
            normalized.append(fact.model_copy(update={"subject": "internal"}))
            changed = True
        else:
            normalized.append(fact)
    return normalized, changed


def _client_issue_confirms_existing_remediation(
    case: EmailProjectTruthCase,
    facts: list[ExtractedProgressFact],
) -> bool:
    """Recognize when direct negative evidence confirms an existing remediation remains valid.

    This does not invent a new action or field update. It only suppresses irrelevant
    milestone-gap boilerplate when a direct client report confirms the same connectivity
    problem that the historical next action already says to investigate or restore.
    """
    current_action = case.project_snapshot.next_action or ""
    if sender_org_type(case.sender_email) != "client" or not current_action:
        return False
    if not (
        _CONNECTIVITY_TOPIC_PATTERN.search(current_action)
        and _REMEDIATION_ACTION_PATTERN.search(current_action)
    ):
        return False

    for fact in facts:
        eligible_citations = [
            citation
            for citation in fact.citations
            if citation.modality == EvidenceModality.CONFIRMED
            and citation.modality != EvidenceModality.QUOTED_HISTORY
        ]
        if not eligible_citations:
            continue
        fact_text = " ".join([
            fact.predicate.replace("_", " "),
            str(fact.value or ""),
            *(citation.quote for citation in eligible_citations),
        ])
        negative = any(
            citation.polarity == EvidencePolarity.NEGATIVE
            for citation in eligible_citations
        )
        if (
            negative
            and _CONNECTIVITY_TOPIC_PATTERN.search(fact_text)
            and _FAILURE_STATE_PATTERN.search(fact_text)
        ):
            return True
    return False


def _transition_follow_up(
    current_milestone,
    proposed_milestone: str | None,
) -> str | None:
    """Return contract-derived work for the milestone that the evidence enters."""
    if not proposed_milestone or proposed_milestone == getattr(current_milestone, "value", current_milestone):
        return None
    contract = get_milestone_contract(proposed_milestone)
    parts = [
        _sentence(
            f"Review the supported project changes, then begin {contract.display_name}"
        )
    ]
    if contract.typical_actions:
        parts.append(_sentence(contract.typical_actions[0]))
    if contract.required_exit_criteria:
        criterion = contract.required_exit_criteria[0]
        parts.append(_sentence(f"Verify the first exit requirement: {criterion.description}"))
    return " ".join(parts)


def _internal_relay_ownership_is_ambiguous(
    case: EmailProjectTruthCase,
    facts: list[ExtractedProgressFact],
    proposed: dict[str, str],
) -> bool:
    """Avoid assigning external ownership from an internal relay with mixed actors."""
    if sender_org_type(case.sender_email) != "internal":
        return False
    actor_subjects = {
        (fact.subject or "").strip().casefold()
        for fact in facts
        if fact.predicate in _OPERATIONAL_PREDICATES
    }
    mixed_actors = "external" in actor_subjects and bool(
        actor_subjects & {"internal", "shared"}
    )
    if not mixed_actors or proposed.get("ball_with") not in {None, "External", "Shared"}:
        return False
    external_quotes = [
        citation.quote for fact in facts
        if fact.predicate in _OPERATIONAL_PREDICATES
        and (fact.subject or "").strip().casefold() == "external"
        for citation in fact.citations
        if citation.modality != EvidenceModality.QUOTED_HISTORY
    ]
    # Multiple explicit actors can be Shared. A pronoun-only relay does not
    # establish which external party actually owns the alleged second task.
    return not external_quotes or all(
        _THIRD_PERSON_ACTOR_PATTERN.search(quote)
        and not re.search(r"\b(?:client|customer|counterparty|external|vendor)\b", quote, re.I)
        for quote in external_quotes
    )


def _internal_pronoun_only_ownership_is_ambiguous(
    case: EmailProjectTruthCase,
    facts: list[ExtractedProgressFact],
    proposed: dict[str, str],
) -> bool:
    """Require explicit accountable-actor evidence for an internal ownership move.

    An internal-authored imperative such as ``Please let them ...`` identifies the
    affected third party but leaves the accountable internal actor implicit. The
    extractor's ``subject`` label alone is not sufficient to move ``ball_with``.
    Direct first/second-person accountability or a named internal role/team remains
    eligible evidence.
    """
    if (
        sender_org_type(case.sender_email) != "internal"
        or proposed.get("ball_with") != "Internal"
    ):
        return False

    supporting_quotes = [
        citation.quote
        for fact in facts
        if fact.predicate in _OPERATIONAL_PREDICATES
        and (fact.subject or "").strip().casefold() == "internal"
        for citation in fact.citations
        if citation.modality != EvidenceModality.QUOTED_HISTORY
    ]
    if not supporting_quotes:
        return False

    return all(
        _THIRD_PERSON_ACTOR_PATTERN.search(quote)
        and not _DIRECT_ACCOUNTABLE_ACTOR_PATTERN.search(quote)
        and not _NAMED_INTERNAL_ROLE_PATTERN.search(quote)
        for quote in supporting_quotes
    )


def _mark_ball_with_needs_review(assessment, reason: str):
    """Remove an unsupported ownership candidate while preserving its audit evidence."""
    item = assessment.ball_with_assessment
    if item is None:
        return assessment
    revised = item.model_copy(update={
        "candidate_value": None,
        "decision": AssessmentDecision.NEEDS_REVIEW,
        "reasons": [reason],
        "risk_level": RiskLevel.MEDIUM,
    })
    return assessment.model_copy(update={"ball_with_assessment": revised})


def _has_current_milestone_partial_progress(
    case: EmailProjectTruthCase,
    facts: list[ExtractedProgressFact],
) -> bool:
    milestone = case.project_snapshot.current_milestone
    if milestone is None:
        return False
    accepted_predicates = {
        predicate
        for criterion in get_milestone_contract(milestone).required_exit_criteria
        for predicate in criterion.accepted_evidence_predicates
    }
    return any(
        fact.predicate in accepted_predicates
        and any(
            citation.polarity == EvidencePolarity.POSITIVE
            and citation.modality == EvidenceModality.CONFIRMED
            for citation in fact.citations
        )
        for fact in facts
    )


def _all_source_quotes_are_history(source_quotes) -> bool:
    return bool(source_quotes) and all(
        citation.modality == EvidenceModality.QUOTED_HISTORY
        for citation in source_quotes
    )


def _is_uncontextualized_short_reply(
    case: EmailProjectTruthCase,
    evidence: ValidatedEmailEvidence,
) -> bool:
    if evidence.accepted_facts:
        return False
    normalized = re.sub(r"\s+", " ", case.newest_authored_body).strip().casefold()
    return bool(re.fullmatch(
        r"(?:yes|no|ok|okay|agreed|confirmed|correct|approved|noted)[.!?]*",
        normalized,
    ))


def _sentence(value: str) -> str:
    normalized = re.sub(r"[ 	]+", " ", value).strip()
    normalized = re.sub(r"\.{2,}$", ".", normalized)
    if not normalized:
        return normalized
    if normalized[-1] not in ".?!":
        normalized += "."
    return normalized


def _changes_summary(proposed: dict[str, str]) -> str:
    changes = ", ".join(
        f"{key} -> {str(value).strip().rstrip('.!?')}"
        for key, value in proposed.items()
    )
    return _sentence(f"The email supports these reviewable project changes: {changes}")


def _preserved_state_lines(snapshot) -> list[str]:
    """Render retained state fields without duplicating the historical next action."""
    fields = (
        ("project_status", snapshot.project_status),
        ("current_milestone", snapshot.current_milestone),
        ("ball_with", snapshot.ball_with),
    )
    lines = ["Preserved project state:"]
    for name, value in fields:
        rendered = getattr(value, "value", value)
        if rendered is not None and str(rendered).strip():
            lines.append(f"- {name}: {rendered}")
    return lines


def _terminal_source_context(case: EmailProjectTruthCase):
    """Return bounded current source context without promoting it to an accepted fact."""
    return next((
        source
        for source in case.sources
        if source.source_role == "newest_authored_email"
        and source.eligible_current_evidence
        and source.text.strip()
    ), None)


def evaluate_validated_evidence(
    case: EmailProjectTruthCase,
    evidence: ValidatedEmailEvidence,
) -> EmailProjectRecommendation:
    snapshot = case.project_snapshot
    policy = load_milestone_contract_policy()
    normalized_facts = enrich_validated_fix_session_facts(
        case,
        _normalize_contract_fact_predicates(
            evidence.accepted_facts,
            policy=policy,
        ),
    )
    normalized_facts, internal_status_request_rebound = (
        _normalize_internal_status_request_ownership(case, normalized_facts)
    )
    distorted_reconfirmations = [
        fact for fact in normalized_facts
        if _unsupported_reconfirmation_distortion(case, fact)
    ]
    evaluation_facts = _merge_operational_obligations([
        fact for fact in normalized_facts
        if fact not in distorted_reconfirmations
    ])
    assessment = assess_project_state(
        ProjectStateInput(
            project_id=case.project_id if evidence.project_identity_resolved else None,
            source_type=case.sources[0].source_type,
            source_ref=case.source_ref,
            current_milestone=snapshot.current_milestone,
            current_status=snapshot.project_status,
            current_ball_with=snapshot.ball_with,
            current_next_action=snapshot.next_action,
            project_identity_resolved=evidence.project_identity_resolved,
            facts=tuple(evaluation_facts),
        ),
        policy=policy,
    )
    proposed = proposed_field_subset(assessment)
    terminal_protected = snapshot.project_status in ProjectStatus.terminal_statuses()
    policy_only_next_action = bool(
        assessment.next_action_assessment is not None
        and assessment.next_action_assessment.provenance
        == NextActionProvenance.POLICY_DERIVED_FOLLOW_UP
    )
    if policy_only_next_action:
        # Contract-derived follow-ups are useful advice, but are not email-evidenced DB fields.
        proposed.pop("next_action", None)

    ambiguous_internal_relay = _internal_relay_ownership_is_ambiguous(
        case,
        normalized_facts,
        proposed,
    )
    ambiguous_pronoun_only_owner = _internal_pronoun_only_ownership_is_ambiguous(
        case,
        normalized_facts,
        proposed,
    )
    if ambiguous_internal_relay:
        reason = (
            "The internal-authored message names mixed accountable parties, so ownership remains unchanged."
        )
        proposed.pop("ball_with", None)
        proposed.pop("next_action", None)
        assessment = _mark_ball_with_needs_review(assessment, reason)
        if assessment.next_action_assessment is not None:
            assessment = assessment.model_copy(update={"next_action_assessment":
                assessment.next_action_assessment.model_copy(update={
                    "candidate_value": None,
                    "decision": AssessmentDecision.CLARIFICATION_REQUIRED,
                    "reasons": [reason],
                })})
    if ambiguous_pronoun_only_owner:
        reason = (
            "The internal-authored message refers only to a third party and does not explicitly identify the accountable internal actor, so ownership remains unchanged."
        )
        proposed.pop("ball_with", None)
        assessment = _mark_ball_with_needs_review(assessment, reason)

    existing_action_confirmed = bool(
        proposed.get("next_action")
        and _existing_action_is_more_specific(case, proposed["next_action"])
    )
    if existing_action_confirmed:
        proposed.pop("next_action", None)
    if assessment.contradictions or not evidence.project_identity_resolved or terminal_protected:
        # Email project-truth is fail-closed for ambiguous, contradictory, and terminal state.
        proposed = {}

    missing = _missing_descriptions(case, assessment)
    source_quotes = unique_source_quotes(evidence.accepted_facts)
    terminal_context = _terminal_source_context(case) if terminal_protected else None
    quoted_history_only = _all_source_quotes_are_history(source_quotes)
    short_reply_without_context = _is_uncontextualized_short_reply(case, evidence)
    partial_progress = _has_current_milestone_partial_progress(case, normalized_facts)
    existing_issue_action_confirmed = _client_issue_confirms_existing_remediation(
        case,
        normalized_facts,
    )

    review_reasons: list[str] = []
    for item in (
        assessment.milestone_assessment,
        assessment.status_assessment,
        assessment.ball_with_assessment,
        assessment.next_action_assessment,
    ):
        if item is None:
            continue
        if item.decision in {
            AssessmentDecision.NEEDS_REVIEW,
            AssessmentDecision.CLARIFICATION_REQUIRED,
            AssessmentDecision.CONTRADICTION,
        }:
            review_reasons.extend(item.reasons or [code.value for code in item.codes])
    if ambiguous_internal_relay:
        review_reasons.append(
            "The internal-authored message names mixed accountable parties, so ownership remains unchanged."
        )
    if ambiguous_pronoun_only_owner:
        review_reasons.append(
            "The internal-authored message refers only to a third party and does not explicitly identify the accountable internal actor, so ownership remains unchanged."
        )
    if distorted_reconfirmations:
        review_reasons.append(
            "An extracted reconfirmation was omitted because it introduced uncertainty that the source did not state."
        )
    if internal_status_request_rebound:
        review_reasons.append(
            "An internal-authored first-person status request is an internal response obligation, not external ownership."
        )

    transition_action = _transition_follow_up(
        snapshot.current_milestone,
        proposed.get("current_milestone"),
    )

    later_stage_lag = AssessmentCode.INVALID_STATE_TRANSITION in (
        set(assessment.assessment_codes)
        | set(assessment.milestone_assessment.codes if assessment.milestone_assessment else [])
    )

    if not evidence.project_identity_resolved:
        summary = "Project identity is not reliable enough to recommend a project update."
    elif terminal_protected:
        summary = (
            "The project is already terminal. The current email is retained as post-go-live "
            "support context and does not justify regressing the onboarding milestone or "
            "changing terminal project fields."
        )
    elif later_stage_lag:
        summary = (
            "The email describes later-stage work than the recorded milestone, "
            "so no project-state change is recommended until the operator reviews the lag."
        )
    elif assessment.contradictions:
        summary = "The current evidence is contradictory, so no project-state change is recommended."
    elif quoted_history_only:
        summary = (
            "The quoted or forwarded material is retained as historical context but is not "
            "eligible current evidence for a project-state update."
        )
    elif short_reply_without_context:
        summary = (
            "The standalone reply lacks the preceding question or context needed to support a "
            "project-state update."
        )
    elif existing_action_confirmed:
        summary = "The email provides new evidence that confirms the existing next action remains appropriate."
    elif existing_issue_action_confirmed:
        summary = (
            "No project-field change is needed: the email confirms the reported connectivity "
            "issue remains active, and the recorded remediation already covers it."
        )
    elif proposed:
        summary = _changes_summary(proposed)
    elif partial_progress:
        summary = (
            "The email records partial milestone progress, but it does not yet support a "
            "project-state transition."
        )
    else:
        summary = "The email adds context but does not yet prove a project-state transition."

    if not evidence.project_identity_resolved:
        action = "Confirm which project this email belongs to before taking action."
    elif terminal_protected:
        action = (
            "Handle the operational issue outside onboarding project-state fields. Require "
            "explicit operator approval and qualifying contract evidence before any "
            "terminal-state change."
        )
    elif later_stage_lag:
        action = (
            "Review the recorded milestone against the later-stage email evidence "
            "before updating any project field. Do not keep earlier-stage busywork "
            "that the email has already passed."
        )
    elif assessment.contradictions:
        action = "Resolve the conflicting evidence with a current authoritative source before updating any project field."
    elif quoted_history_only:
        action = "Obtain current eligible evidence before changing project state."
    elif short_reply_without_context:
        action = "Obtain the preceding message or an explicit clarification before proposing an update."
    elif existing_action_confirmed or existing_issue_action_confirmed:
        action = snapshot.next_action or "Record the new evidence and keep the existing project state."
    elif proposed.get("next_action"):
        action = proposed["next_action"]
    elif transition_action:
        action = transition_action
    elif snapshot.next_action:
        action = f"Keep the existing next action: {snapshot.next_action}"
    elif proposed:
        changes = ", ".join(
            f"{key} -> {str(value).strip().rstrip('.!?')}"
            for key, value in proposed.items()
        )
        action = _sentence(f"Review the supported project changes: {changes}")
    elif missing:
        action = _sentence(f"Obtain confirmation that: {missing[0]}")
    else:
        action = "Record the email as evidence and keep the existing project state."

    show_transition_gaps = bool(
        missing
        and not assessment.contradictions
        and evidence.project_identity_resolved
        and not terminal_protected
        and not existing_action_confirmed
        and not existing_issue_action_confirmed
        and not short_reply_without_context
        and (not proposed or partial_progress)
    )
    if partial_progress and show_transition_gaps and proposed.get("next_action"):
        action = _sentence(action)
        action = f"{action} Then verify the remaining milestone requirements listed below."

    rendered_lines = [summary]
    if proposed:
        rendered_lines.append("Recommended changes:")
        rendered_lines.extend(f"- {key}: {value}" for key, value in proposed.items())
    else:
        rendered_lines.extend(_preserved_state_lines(snapshot))
    if show_transition_gaps:
        if partial_progress:
            rendered_lines.append(
                "The email records partial progress, but milestone transition still requires:"
            )
        else:
            rendered_lines.append(
                "No milestone transition is proposed because the contract still requires:"
            )
        rendered_lines.extend(f"- {item}" for item in missing)
    action_label = (
        "Existing project next action (historical snapshot)"
        if existing_action_confirmed or existing_issue_action_confirmed
        else "Next action"
    )
    rendered_lines.append(f"{action_label}: {_sentence(action)}")
    if source_quotes:
        rendered_lines.append(
            "Quoted/history context:"
            if quoted_history_only
            else "Source evidence:"
        )
        rendered_lines.extend(
            f'- [{citation.source_ref}] "{citation.quote}"' for citation in source_quotes
        )
    elif terminal_context is not None:
        rendered_lines.append("Bounded source context (not accepted as a state-change fact):")
        rendered_lines.append(
            f'- [{terminal_context.source_ref}] "{terminal_context.text.strip()}"'
        )

    return EmailProjectRecommendation(
        assessment=assessment,
        proposed_fields=proposed,
        state_change_citation_coverage=_state_change_coverage(assessment, proposed),
        missing_evidence=missing,
        review_reasons=list(dict.fromkeys(review_reasons)),
        summary=summary,
        suggested_action=_sentence(action),
        rendered_answer="\n".join(rendered_lines),
        source_quotes=source_quotes,
    )
