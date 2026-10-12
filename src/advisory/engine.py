"""
Advisory engine — generates context-aware recommendations for projects and support tickets.

Deterministic path first (static knowledge). LLM only for complex/ambiguous situations.
"""

import logging
import sqlite3

from src.advisory.knowledge import (
    get_support_advisory,
    match_event_to_advisory,
)
from src.advisory.pattern_matcher import enrich_advisory_with_patterns
from src.models.advisory import Advisory, DraftSuggestion, EscalationTarget, NextStep
from src.models.support_ticket import SupportTicket
from src.advisory.milestone_wiki import get_milestone_completion_criteria, get_milestone_guide
from src.advisory.pm_problem_index import get_pm_stall_guidance, primary_stall_action
from src.repositories.learned_rules_repo import format_rules_for_prompt, get_applicable_rules
from src.utils.enum_helpers import enum_val

logger = logging.getLogger(__name__)

_RULE58_STALL_BUSINESS_DAYS = 3


def _rule58_stall_next_step(
    conn: sqlite3.Connection,
    project_id: str,
    current_milestone: str,
    ball_with: str | None,
) -> NextStep | None:
    """Wiki SSOT stall recommendation when External inactive >3 business days."""
    if (ball_with or "").lower() != "external":
        return None
    from datetime import datetime, timezone

    from src.repositories.project_repo import get_project
    from src.utils.calendar import business_days_between

    project = get_project(conn, project_id)
    if not project or not project.updated_at:
        return None
    updated = project.updated_at
    if isinstance(updated, str):
        updated = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    days = business_days_between(updated, datetime.now(timezone.utc))
    if days <= _RULE58_STALL_BUSINESS_DAYS:
        return None

    guidance = get_pm_stall_guidance(current_milestone)
    action = primary_stall_action(guidance)
    if not action:
        action = (
            f"Follow-up email — External inactive {days} business days "
            f"(rule 58, {current_milestone})"
        )
    reason = (
        f"pm-problem-index stall policy; {days} business days since last update"
    )
    can_draft = guidance.get("draft_signal", "").startswith("stale_external")
    return NextStep(
        action=action,
        reason=reason,
        priority="now",
        can_auto_draft=can_draft,
    )


class AdvisoryEngine:
    """Generates Advisory objects for project events and support tickets."""

    def __init__(self, llm_fn=None):
        """
        Args:
            llm_fn: Optional LLM callable for complex cases.
                     Signature: (task, messages, schema, **kwargs) -> dict
        """
        self._llm_fn = llm_fn

    def advise_project(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        current_milestone: str,
        ball_with: str | None = None,
        event_summary: str = "",
        event_keywords: list[str] | None = None,
        assessment_detail: dict | None = None,
        operator_id: str | None = None,
        trusted_internal: bool = False,
    ) -> Advisory:
        """Generate advisory for a project event.

        Deterministic path when static knowledge has a strong match.
        Falls back to LLM for complex/ambiguous situations.
        """
        from src.context.governed_memory import authorize_project_operator

        authorized_operator = authorize_project_operator(
            conn, project_id, operator_id, trusted_internal=trusted_internal,
        )
        if not authorized_operator:
            return Advisory(
                work_type="project",
                situation_assessment="Unauthorized advisory request.",
                confidence=0.0,
            )

        next_steps: list[NextStep] = []
        escalation = None
        draft_suggestion = None
        assessment = ""
        confidence = 0.5
        evidence_ref: list[str] = []
        from src.milestone.contracts import load_milestone_contract_policy
        policy_ref = [
            f"milestone-contracts:policy-v{load_milestone_contract_policy().policy_version}"
        ]
        criterion_results: list[dict] = []
        db_conflicts: list[str] = []
        from src.context.engine import ContextEngine
        advisory_frame = ContextEngine(conn).build_for_advisory(
            project_id,
            event_summary or "project advisory",
            operator_id=authorized_operator,
            trusted_internal=False,
        )
        governed_memory = advisory_frame.memory if advisory_frame else ""

        # Try event-specific match first
        if event_keywords:
            event_match = match_event_to_advisory(current_milestone, event_keywords)
            if event_match:
                assessment = event_match.get("assessment", "")
                next_steps.append(NextStep(
                    action=event_match["next_action"],
                    reason=assessment,
                    priority=event_match.get("priority", "soon"),
                    can_auto_draft=event_match.get("can_auto_draft", False),
                ))
                if event_match.get("escalation_team"):
                    escalation = EscalationTarget(
                        team=event_match["escalation_team"],
                        reason=assessment,
                    )
                if event_match.get("can_auto_draft"):
                    draft_suggestion = DraftSuggestion(
                        purpose=event_match["next_action"],
                        key_points=[assessment],
                    )
                confidence = 0.85

        # Add milestone-level knowledge
        mk = get_milestone_guide(current_milestone)
        if mk and not next_steps:
            assessment = assessment or f"Project at {current_milestone}, ball with {ball_with or 'unknown'}."
            for action in mk.get("key_actions", [])[:2]:
                next_steps.append(NextStep(
                    action=action,
                    reason=f"Standard action for {current_milestone}",
                    priority="soon",
                ))
            confidence = max(confidence, 0.7)

        # Deterministic contract evaluation is authoritative. Without structured
        # cited facts, fail closed rather than pretending every criterion is missing.
        from src.agent.progress_evaluation import (
            citation_refs, evaluate_progress_detail, priority_criterion,
        )
        progress = evaluate_progress_detail(conn, project_id, assessment_detail)
        if progress:
            priority = priority_criterion(progress)
            evidence_ref = citation_refs(progress)
            criterion_results = [item.model_dump(mode="json") for item in progress.milestone_criteria]
            candidate = progress.milestone_assessment.candidate_value
            if candidate and candidate != current_milestone:
                db_conflicts.append(
                    f"current_milestone {current_milestone} -> {candidate}"
                )
            from src.models.progress_assessment import AssessmentCode
            later_stage = AssessmentCode.INVALID_STATE_TRANSITION in progress.assessment_codes
            if later_stage:
                reason = ""
                if progress.milestone_assessment and progress.milestone_assessment.reasons:
                    reason = progress.milestone_assessment.reasons[0]
                db_conflicts.append(
                    reason
                    or f"recorded milestone {current_milestone} lags later-stage email evidence"
                )
            confidence = max(confidence, 0.8)
            if later_stage:
                next_steps.insert(0, NextStep(
                    action="Review recorded milestone against later-stage email evidence",
                    reason=(
                        progress.milestone_assessment.reasons[0]
                        if progress.milestone_assessment and progress.milestone_assessment.reasons
                        else "Email evidence is later-stage than the recorded milestone."
                    ),
                    priority="now",
                ))
                assessment = (
                    "Later-stage email evidence conflicts with the recorded milestone; "
                    "do not keep earlier-stage busywork and do not auto-jump."
                )
            elif priority:
                label = "contradicted" if priority.result.value == "contradicted" else "missing"
                next_steps.insert(0, NextStep(
                    action=f"Resolve {priority.criterion_id}: {priority.explanation}",
                    reason=f"Highest-priority actual {label} criterion for {current_milestone}",
                    priority="now",
                ))
                assessment = (
                    f"Deterministic evaluation found {priority.criterion_id} {label}; "
                    "milestone, status, ball-with, and next action remain independent."
                )
            elif progress.milestone_assessment.candidate_value:
                candidate = progress.milestone_assessment.candidate_value
                next_steps.insert(0, NextStep(
                    action=f"Stage {candidate} for operator confirmation",
                    reason="All authoritative exit criteria are met",
                    priority="now",
                ))
                assessment = f"All criteria are met; adjacent candidate is {candidate}, not an automatic write."
        elif not next_steps:
            criteria = get_milestone_completion_criteria(current_milestone)
            assessment = (
                f"No structured cited facts are available to evaluate {current_milestone}; "
                f"abstain. Authoritative criteria: {'; '.join(criteria)}"
            )
            confidence = max(confidence, 0.7)

        stall_step = _rule58_stall_next_step(conn, project_id, current_milestone, ball_with)
        if stall_step:
            if progress and priority:
                next_steps.append(stall_step)
            else:
                next_steps.insert(0, stall_step)
            if not assessment or "rule 58" not in assessment.lower():
                assessment = (
                    (assessment + " ") if assessment else ""
                ) + (
                    "External stall per pm-problem-index (rule 58): "
                    f"{stall_step.action}"
                )
            confidence = max(confidence, 0.82)
            policy_ref = list(policy_ref) + ["playbook/fix-connectivity/pm-problem-index.md"]

        # If confidence is low and an event needs interpretation, enhance with
        # the injected callable or the production structured client.
        if confidence < 0.8 and event_summary:
            candidate_scopes = ()
            if advisory_frame:
                candidate_scopes = (advisory_frame.memory_exposure_scope(
                    operator_id=authorized_operator, surface="advisory",
                    task_type="advisory", project_id=project_id,
                ),)
            row = conn.execute(
                "SELECT market FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            market = row["market"] if row else None
            from src.memory.exposure_context import inference_scope

            with inference_scope(
                operator_id=authorized_operator, surface="advisory",
                task_type="advisory", project_id=project_id,
                candidate_scopes=candidate_scopes,
            ):
                llm_advisory = self._call_llm_project(
                    current_milestone, ball_with,
                    event_summary + (f"\n\n{governed_memory}" if governed_memory else ""), mk,
                    conn=conn, market=market, project_id=project_id,
                    operator_id=authorized_operator,
                )
            if llm_advisory:
                return llm_advisory

        if not assessment:
            assessment = f"Project {project_id} at {current_milestone}."

        advisory = Advisory(
            work_type="project",
            situation_assessment=assessment,
            next_steps=next_steps,
            escalation=escalation,
            draft_suggestion=draft_suggestion,
            similar_past_cases=[],
            confidence=confidence,
            evidence_ref=evidence_ref,
            policy_ref=policy_ref,
            criterion_results=criterion_results,
            db_conflicts=db_conflicts,
        )
        enrich_advisory_with_patterns(conn, advisory, keywords=event_keywords)
        return advisory

    def advise_support(
        self,
        conn: sqlite3.Connection,
        ticket: SupportTicket,
        event_summary: str = "",
    ) -> Advisory:
        """Generate advisory for a support ticket.

        Deterministic path when category knowledge provides clear guidance.
        Falls back to LLM for complex/ambiguous situations.
        """
        category = enum_val(ticket.category)
        knowledge = get_support_advisory(category)

        next_steps: list[NextStep] = []
        escalation = None
        draft_suggestion = None
        confidence = 0.5

        if knowledge:
            severity_str = enum_val(ticket.severity)
            assessment = (
                f"{severity_str.capitalize()} {category.replace('_', ' ')} issue"
                f"{' for ' + ticket.counterparty if ticket.counterparty else ''}. "
                f"Common causes: {', '.join(knowledge['common_causes'][:2])}."
            )

            # Add diagnostic steps as next_steps
            for i, step in enumerate(knowledge.get("diagnostic_steps", [])[:3]):
                next_steps.append(NextStep(
                    action=step,
                    reason=f"Standard diagnostic for {category}",
                    priority="now" if i == 0 else "soon",
                ))

            # Add resolution actions
            for action in knowledge.get("resolution_actions", [])[:2]:
                next_steps.append(NextStep(
                    action=action,
                    reason=f"Common resolution for {category}",
                    priority="soon",
                    can_auto_draft=True,
                ))

            # Escalation
            esc_team = knowledge.get("escalation_team")
            if esc_team and severity_str in ("critical", "high"):
                escalation = EscalationTarget(
                    team=esc_team,
                    reason=f"{severity_str.capitalize()} {category} issue — consider involving {esc_team}",
                )

            # Draft suggestion for high/critical
            if severity_str in ("critical", "high"):
                draft_suggestion = DraftSuggestion(
                    purpose=f"Acknowledge {category} issue and request diagnostic info",
                    key_points=knowledge.get("diagnostic_steps", [])[:3],
                    tone="professional_urgent" if severity_str == "critical" else "professional",
                )

            confidence = 0.85
        else:
            assessment = f"Support ticket: {ticket.title[:100]}."

        # Category confidence is an internal routing score, not calibrated case certainty.
        # Always ground the operator-facing advice in the actual reported issue.
        from src.advisory.support_message import grounded_support_guidance
        assessment, next_steps = grounded_support_guidance(ticket, event_summary)
        if not knowledge and self._llm_fn and event_summary:
            llm_advisory = self._call_llm_support(ticket, event_summary, knowledge, conn=conn)
            if llm_advisory:
                # Retain tailored investigation steps, but do not present a model
                # hypothesis as a source-confirmed diagnosis.
                llm_advisory.situation_assessment = assessment
                return llm_advisory

        advisory = Advisory(
            work_type="support",
            situation_assessment=assessment,
            next_steps=next_steps,
            escalation=escalation,
            draft_suggestion=draft_suggestion,
            similar_past_cases=[],
            confidence=confidence,
        )
        enrich_advisory_with_patterns(conn, advisory, ticket=ticket)
        return advisory

    # ------------------------------------------------------------------
    # LLM paths
    # ------------------------------------------------------------------

    def _call_llm_project(
        self,
        milestone: str,
        ball_with: str | None,
        event_summary: str,
        milestone_knowledge: dict,
        *,
        conn: sqlite3.Connection | None = None,
        market: str | None = None,
        project_id: str | None = None,
        operator_id: str | None = None,
    ) -> Advisory | None:
        """Call LLM for project advisory. Returns None on failure."""
        try:
            from src.utils.prompt_loader import load_prompt
            prompt_template = load_prompt("advisory/project_advisor.md")

            rules_block = ""
            if conn is not None:
                from datetime import datetime, timezone
                from src.memory.retriever import MemoryRetrievalRequest
                request = MemoryRetrievalRequest(operator_id=operator_id, project_id=project_id,
                    market=market, surface="advisory", task_type="project_advisory", at=datetime.now(timezone.utc))
                rules = get_applicable_rules(conn, market=market, memory_request=request)
                rules_block = format_rules_for_prompt(rules)

            prompt = (
                f"{prompt_template}\n\n"
                f"## Current State\n"
                f"Milestone: {milestone}\nBall with: {ball_with or 'unknown'}\n\n"
                f"## Milestone Knowledge\n{milestone_knowledge}\n\n"
            )
            if rules_block:
                prompt += f"## {rules_block}\n\n"
            prompt += f"## Recent Event\n{event_summary[:2000]}"
            schema = {
                "situation_assessment": "string",
                "next_steps": [{"action": "string", "reason": "string", "priority": "now|soon|when_ready"}],
                "escalation": None,
                "draft_suggestion": None,
                "confidence": 0.8,
            }
            llm_fn = self._llm_fn
            if llm_fn is None:
                from src.llm.client import llm_call_structured

                llm_fn = llm_call_structured
            result = llm_fn("advisory", [{"role": "user", "content": prompt}], schema, temperature=0.1)
            return self._parse_llm_advisory(result, "project")
        except Exception as e:
            logger.warning("LLM project advisory failed: %s", e)
            return None

    def _call_llm_support(
        self,
        ticket: SupportTicket,
        event_summary: str,
        knowledge: dict | None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> Advisory | None:
        """Call LLM for support advisory. Returns None on failure."""
        try:
            from src.utils.prompt_loader import load_prompt
            prompt_template = load_prompt("advisory/support_advisor.md")

            rules_block = ""
            if conn is not None:
                rules = get_applicable_rules(conn)
                rules_block = format_rules_for_prompt(rules)

            prompt = (
                f"{prompt_template}\n\n"
                f"## Ticket\nCategory: {ticket.category}\nSeverity: {ticket.severity}\n"
                f"Title: {ticket.title}\nDescription: {(ticket.description or '')[:1000]}\n\n"
                f"## Category Knowledge\n{knowledge}\n\n"
            )
            if rules_block:
                prompt += f"## {rules_block}\n\n"
            prompt += f"## Event Summary\n{event_summary[:2000]}"
            schema = {
                "situation_assessment": "string",
                "next_steps": [{"action": "string", "reason": "string", "priority": "now|soon|when_ready"}],
                "escalation": None,
                "draft_suggestion": None,
                "confidence": 0.8,
            }
            llm_fn = self._llm_fn
            if llm_fn is None:
                from src.llm.client import llm_call_structured

                llm_fn = llm_call_structured
            result = llm_fn("advisory", [{"role": "user", "content": prompt}], schema, temperature=0.1)
            return self._parse_llm_advisory(result, "support")
        except Exception as e:
            logger.warning("LLM support advisory failed: %s", e)
            return None

    @staticmethod
    def _parse_llm_advisory(result: dict, work_type: str) -> Advisory:
        """Parse LLM result dict into Advisory model."""
        next_steps = []
        for ns in result.get("next_steps", []):
            next_steps.append(NextStep(
                action=ns.get("action", ""),
                reason=ns.get("reason", ""),
                priority=ns.get("priority", "soon"),
                can_auto_draft=ns.get("can_auto_draft", False),
            ))

        escalation = None
        esc = result.get("escalation")
        if esc and isinstance(esc, dict):
            escalation = EscalationTarget(
                team=esc.get("team", ""),
                reason=esc.get("reason", ""),
                suggested_recipients=esc.get("suggested_recipients", []),
            )

        draft = None
        ds = result.get("draft_suggestion")
        if ds and isinstance(ds, dict):
            draft = DraftSuggestion(
                purpose=ds.get("purpose", ""),
                key_points=ds.get("key_points", []),
                tone=ds.get("tone", "professional"),
            )

        return Advisory(
            work_type=work_type,
            situation_assessment=result.get("situation_assessment", ""),
            next_steps=next_steps,
            escalation=escalation,
            draft_suggestion=draft,
            confidence=result.get("confidence", 0.7),
        )
