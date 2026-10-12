"""Facts-only LLM extraction for bounded inbound email evidence."""

from __future__ import annotations

import json
import time
from typing import Any

from src.email.project_truth.contracts import (
    EmailFactExtraction,
    EmailProjectTruthCase,
    PipelineCallMetadata,
)
from src.llm.client import StructuredLLMResponse, get_trace_ids, llm_call_structured
from src.email.project_truth.model_routing import normalize_research_model
from src.email.sender_trust import sender_org_type
from src.milestone.contracts import load_milestone_contract_policy


_SYSTEM_PROMPT = """You extract atomic observable facts from one bounded inbound email case.
Return facts only. Never recommend project fields, milestones, status, ownership, or writes.
Use only the supplied source documents. Every citation quote must be copied verbatim from its
specified source_ref and must support exactly one atomic fact. Every quote must be one contiguous
substring from that source. Never concatenate or splice non-contiguous source spans into one quote,
even when only an intervening line is omitted. Use separate citations and, when needed, separate
facts when supporting evidence appears in separate spans. Separate confirmed completion,
in-progress work, plans, conditions, negative evidence, quoted history, and uncertainty.
Do not convert scheduled or planned work into completion. Do not use quoted/forwarded history
as current confirmation. Resolve project identity only when the bounded case clearly supports it.
Use stable snake_case predicates that match the milestone contract when applicable. For milestone
evidence, the fact predicate MUST be exactly one of the criterion's allowed_fact_predicates. The
semantic_criterion_predicate describes the overall gate only; never emit it as an observed fact unless
it is also explicitly listed in allowed_fact_predicates. Prefer the most specific allowed predicate
directly supported by the cited words, and emit separate atomic facts for separate criteria.

Operational requests and commitments are observable facts, not project-field recommendations:
- Use explicit_obligation when the source directly asks an accountable party to perform a concrete
  action. Set subject to exactly internal, external, or shared. Set value to a concise, faithful
  action that preserves the requested object, material identifiers, observed result, and condition.
  When the authored request is generic (for example, "please check") but the same bounded source
  contains the concrete result being submitted for review, include that concrete result in value
  without inventing a remediation. The value must be an executable action, not a passive label
  such as "request received" or "exemption requested". For every direct request, always emit an
  explicit_obligation even when you also emit a domain-specific *_requested fact. For exemption,
  waiver, approval, or acceptance requests, the executable obligation is to review and decide the
  request; never say to grant, approve, accept, or waive unless that outcome is already explicitly
  confirmed by the source. If phrases such as "also" or "as well" extend the same request to another
  named item, include every named item in the obligation. A directly observed request uses citation
  modality confirmed because the request itself is confirmed; this does not mean the requested work
  or requested decision is complete.
- Preserve opaque identifiers, labels, and directional wording verbatim. Never infer what an
  unexplained number, queue, channel, route, environment, or label represents. For example, if the
  source reports one state "in 81" and asks to "go to 82", keep 81 and 82 as opaque labels and keep
  the observed statement separate from the requested move; do not reinterpret them as values of
  heartbeat or another technical field.
- Predicates prefixed with validated_ are system-owned deterministic results. Never emit
  validated_bidirectional_fix_session_log or claim L2 validation. For a supplied FIX heartbeat
  transcript, emit heartbeat_exchange_observed with exact source citations; the downstream
  validator independently checks complete messages, BodyLength, CheckSum, and reciprocity.
- Do not add uncertainty that the source does not contain. In particular, never insert "whether",
  "if", or an equivalent hedge unless the authored source is explicitly interrogative or uncertain.
  Distinguish an assertion or confirmation of completion from a request to verify completion. A
  terse confirmation followed by a declarative consequence or readiness statement is evidence being
  reported, not automatically a new obligation to reconfirm it. Create an explicit_obligation only
  when the bounded wording actually asks the recipient to check, verify, confirm, or decide.
- Use next_action_commitment when an accountable party explicitly commits to a concrete future
  action. Use the same subject vocabulary and keep planned/conditional modality where applicable.
- A request such as reviewing submitted evidence is an internal obligation when a direct external
  sender asks the internal recipient to check, review, investigate, approve, or proceed.
- For an internal-authored first-person status request such as "May we know the update" or "let us
  know the status", the response obligation is internal unless the authored text explicitly names
  an external/client/counterparty actor as responsible. Do not infer External merely because an
  update is being requested.
- A promise by a direct external sender to retry, send, confirm, or remediate is an external
  commitment. For an internal sender, determine the accountable party from the authored text. Do
  not assign external ownership merely from third-person pronouns or an indirect relay. If the same
  internal-authored message contains both an internal commitment and a request to another party,
  preserve both facts and use shared when sole accountability is not explicit.
- Do not use blocking_issue_confirmed or production_blocker_confirmed merely because one test,
  order, connection attempt, or validation failed. Use those predicates only when the bounded
  source explicitly establishes that the issue currently prevents required project progress or
  production readiness. Otherwise extract the concrete failed-result predicate only.

When historical_project_snapshot.action_plan is present and not truncated, also propose action_changes for explicitly evidenced changes to that plan. Use only its existing followup_id and action_revision for updates/completion/cancellation. New actions use distinct new: references; dependencies reference existing IDs or these new references. Preserve every distinct requested task rather than replacing the plan with one sentence. Each action change needs exact citations. Keep action descriptions close to the source wording and in its language; do not replace the requested verb with a different action. Identify an existing task with enough quoted detail to distinguish it from other tasks. Never equate a FIX order cancellation with cancellation of a PM task, or a passing test case with completion of all conformance work. A planned/conditional activity is not a completed task. Include due_date only for an explicit, unambiguous committed calendar date in the source, or today/tomorrow/day-after-tomorrow expressed with an explicit HKT/UTC zone and supplied authored_at. Never use processing time as the anchor, and do not invent dates from estimates, missing zones or missing authored_at. Do not supply changes to milestone, project status, permissions or source authority via action_changes. If no plan is supplied, the plan is truncated, or the target action is ambiguous, return action_changes=[] and describe the missing context in uncertainties when relevant.

Source authority, citation actor, and event time are system-owned metadata. Do not infer or return
them. The fact subject is only the operational accountable-party classification described above,
not the citation actor. If a source is marked ineligible_current_evidence, set its citation modality
to quoted_history. Do not include hidden reasoning; provide only the requested structured result."""


def milestone_contract_payload() -> dict[str, Any]:
    policy = load_milestone_contract_policy()
    return {
        "policy_version": policy.policy_version,
        "milestones": [
            {
                "id": contract.id.value,
                "purpose": contract.purpose,
                "required_exit_criteria": [
                    {
                        "id": criterion.id,
                        "description": criterion.description,
                        "semantic_criterion_predicate": criterion.predicate,
                        "allowed_fact_predicates": criterion.accepted_evidence_predicates,
                    }
                    for criterion in contract.required_exit_criteria
                ],
                "close_criteria": [
                    {
                        "id": criterion.id,
                        "description": criterion.description,
                        "semantic_criterion_predicate": criterion.predicate,
                        "allowed_fact_predicates": criterion.accepted_evidence_predicates,
                    }
                    for criterion in contract.close_criteria
                ],
                "in_progress_signals": contract.in_progress_signals,
                "insufficient_evidence_signals": contract.insufficient_evidence_signals,
                "contradiction_signals": contract.contradiction_signals,
            }
            for contract in policy.milestones
        ],
        "additional_operational_predicates": [
            {
                "predicate": "explicit_obligation",
                "meaning": "A concrete action is directly requested from an accountable party.",
                "subject_values": ["internal", "external", "shared"],
                "value_rule": (
                    "Concise faithful action preserving every requested object and condition; "
                    "requests for approval or exemption must ask the actor to review and decide, "
                    "not assume the requested outcome."
                ),
                "modality_rule": "confirmed when the request itself is directly observed",
            },
            {
                "predicate": "next_action_commitment",
                "meaning": "An accountable party explicitly commits to a concrete future action.",
                "subject_values": ["internal", "external", "shared"],
            },
            {
                "predicate": "pending_explicit_obligation",
                "meaning": "A previously assigned concrete obligation is explicitly still pending.",
            },
            {
                "predicate": "work_in_progress_confirmed",
                "meaning": "The source explicitly confirms current work is actively progressing.",
            },
            {
                "predicate": "blocking_issue_confirmed",
                "meaning": "The source explicitly says an issue currently prevents required project progress.",
                "anti_example": "A single failed test or rejected order without stated project-level impact.",
            },
            {
                "predicate": "production_blocker_confirmed",
                "meaning": "The source explicitly says an issue currently prevents production readiness or operation.",
                "anti_example": "A single failed production test without an explicit readiness impact statement.",
            },
            {"predicate": "blocker_resolved_confirmed"},
            {"predicate": "project_completed_confirmed"},
            {"predicate": "project_cancelled_confirmed"},
            {"predicate": "project_archived_confirmed"},
        ],
    }


def extraction_messages(case: EmailProjectTruthCase) -> list[dict[str, str]]:
    payload = {
        "case": {
            "project_id": case.project_id,
            "subject": case.subject,
            "sender_email": case.sender_email,
            "sender_org_type": sender_org_type(case.sender_email),
            "received_at": case.received_at.isoformat() if case.received_at else None,
            "authored_at": case.authored_at.isoformat() if case.authored_at else None,
            "quoted_history": case.quoted_history,
            "forwarded_attachment": case.forwarded_attachment,
        },
        "historical_project_snapshot": case.project_snapshot.model_dump(mode="json"),
        "bounded_sources": [source.model_dump(mode="json") for source in case.sources],
        "milestone_contract": milestone_contract_payload(),
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def normalize_action_change_deltas(payload, case):
    """Remove model echo fields, never relax the operator mutation contract.

Terminal operations may echo the captured task but cannot combine real changes.
For updates, ambiguous null removal of a populated field remains reviewable.
"""
    import copy
    if not isinstance(payload, dict):
        return payload
    result = copy.deepcopy(payload)
    plan = case.project_snapshot.action_plan or {}
    existing = {row["followup_id"]: row for row in plan.get("actions", [])}
    for item in result.get("action_changes", []):
        change = item.get("change") if isinstance(item, dict) else None
        if not isinstance(change, dict):
            continue
        row = existing.get(change.get("action_id"))
        operation = change.get("operation")
        for field in ("description", "owner_side", "due_date", "reminder_due_at", "depends_on"):
            if field not in change:
                continue
            value = change[field]
            if row is not None and value == row.get(field):
                change.pop(field)
            elif value is None and (operation in {"complete", "cancel", "create"} or field == "depends_on"):
                change.pop(field)
    result["action_changes"] = [item for item in result.get("action_changes", []) if not (
        isinstance(item, dict) and isinstance(item.get("change"), dict)
        and item["change"].get("operation") == "update"
        and set(item["change"]) <= {"action_id", "operation", "expected_revision"}
        and item["change"].get("action_id") in existing
        and item["change"].get("expected_revision") == existing[item["change"]["action_id"]].get("action_revision")
    )]
    return result


def run_fact_extraction(
    case: EmailProjectTruthCase,
    *,
    model_override: str | None = None,
    timeout_sec: int = 120,
) -> tuple[EmailFactExtraction, PipelineCallMetadata]:
    started = time.perf_counter()
    response = llm_call_structured(
        "reasoning",
        extraction_messages(case),
        EmailFactExtraction.model_json_schema(),
        model_override=normalize_research_model(model_override),
        temperature=0,
        timeout_sec=timeout_sec,
        return_metadata=True,
        notify_on_error=False,
    )
    if not isinstance(response, StructuredLLMResponse):
        raise TypeError("structured extraction did not return metadata")
    extraction = EmailFactExtraction.model_validate(normalize_action_change_deltas(response.data, case))
    inference = response.inference
    return extraction, PipelineCallMetadata(
        model=response.model,
        latency_ms=round((time.perf_counter() - started) * 1000),
        trace_ids=get_trace_ids(),
        inference_trace_id=inference.trace_id if inference else None,
        output_revision_id=inference.output_revision_id if inference else None,
    )
