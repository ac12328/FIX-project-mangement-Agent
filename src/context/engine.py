"""
Unified Context Engine (WS-2).

Single class with three entry points:
  build_for_chat()  — Telegram free-text agent loop
  build_for_draft() — email draft generation
  build_for_tool()  — on-demand tool context (status, etc.)

All return a ContextFrame. Replaces both builder.build_context()
and the inline context assembly in agent_chat._build_initial_messages().
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal

ContextProfile = Literal["full", "minimal", "bootstrap"]

from config import defaults
from config.defaults import (
    CONVERSATION_HISTORY_LIMIT,
    EVIDENCE_DISPLAY_LIMIT,
    KNOWLEDGE_CHAR_BUDGET,
)
from config.settings import BASE_DIR
from src.context.layer1_truth import load_project_truth
from src.context.layer2_working_memory import load_working_memory
from src.context.layer3_relationship import load_relationship
from src.context.layer4_evidence import load_evidence
from src.context.knowledge_loader import load_knowledge_for_project
from src.models.context_frame import ContextFrame
from src.wiki.retrieval import (
    WikiRetrievalMode,
    WikiSearchRequest,
    search_wiki,
)

logger = logging.getLogger(__name__)

_KNOWLEDGE_CHAR_BUDGET = KNOWLEDGE_CHAR_BUDGET

_LOW_PRIORITY_PREFIXES = (
    "<wiki-context>", "<wiki-topic>", "<wiki-project-context>", "<market-knowledge>",
)


def _trim_knowledge(parts: list[str], budget: int = _KNOWLEDGE_CHAR_BUDGET) -> list[str]:
    """Trim knowledge parts to fit within a character budget.

    Drops low-priority reference material (wiki, market) first,
    then truncates remaining parts if still over budget.
    """
    total = sum(len(p) for p in parts)
    if total <= budget:
        return parts

    high: list[str] = []
    low: list[str] = []
    for p in parts:
        if p.startswith(_LOW_PRIORITY_PREFIXES):
            low.append(p)
        else:
            high.append(p)

    high_total = sum(len(p) for p in high)
    if high_total >= budget:
        kept: list[str] = []
        used = 0
        for p in high:
            if used + len(p) > budget:
                remaining = budget - used
                if remaining > 200:
                    kept.append(p[:remaining] + "\n[...trimmed]")
                break
            kept.append(p)
            used += len(p)
        return kept

    remaining_budget = budget - high_total
    for p in low:
        if remaining_budget <= 0:
            break
        if len(p) <= remaining_budget:
            high.append(p)
            remaining_budget -= len(p)
        else:
            if remaining_budget > 200:
                high.append(p[:remaining_budget] + "\n[...trimmed]")
            remaining_budget = 0
    return high


class ContextEngine:
    """Assembles ContextFrame from all available context sources."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # ── Chat context ─────────────────────────────────────────────────

    def build_for_chat(
        self,
        user_text: str,
        chat_id: int,
        *,
        project_id: str | None = None,
        profile: ContextProfile = "full",
        operator_id: str | None = None,
        entity_refs: list[tuple[str, str]] | None = None,
    ) -> ContextFrame:
        """Build context for the Telegram agent chat loop."""
        identity = _load_system_prompt(profile)
        op_mem = self._load_operator_memory_text(
            project_id=project_id, chat_id=chat_id, operator_id=operator_id,
            entity_refs=entity_refs,
        )
        memory_decision = self._compose_governed_memory(
            op_mem, surface="chat", query=user_text, profile=profile,
            operator_id=operator_id, project_id=project_id,
        )

        knowledge_parts, project_truth, evidence_text, memory_text, relationship_text, missing_flags, confidence = (
            self._build_project_knowledge(
                user_text, project_id, profile=profile, chat_id=chat_id, operator_id=operator_id,
                suppress_legacy_authority=memory_decision.suppress_legacy_authority,
            )
        )

        # Operational state is independent from replaceable learned authority.
        operational_parts = [memory_text] if memory_text and profile != "bootstrap" else []
        learned_memory = memory_decision.text
        memory_parts: list[str] = []
        if operational_parts:
            memory_parts.extend(operational_parts)
        if learned_memory:
            memory_parts.append(learned_memory)

        conv_summary = self._get_conversation_summary(chat_id)

        if profile == "bootstrap":
            # Last 2–3 TG turns OR rolling summary — not both
            if conv_summary:
                memory_parts.append(conv_summary)
                conversation: list[dict[str, Any]] = []
            else:
                conversation = self._get_compressed_history(chat_id, limit=3)
        else:
            if conv_summary:
                memory_parts.append(conv_summary)

            if profile != "minimal":
                progress = self._get_progress(chat_id, active_project_id=project_id)
                if progress:
                    memory_parts.append(progress)

                prior_sessions = self._get_prior_sessions(chat_id, active_project_id=project_id)
                if prior_sessions:
                    memory_parts.append(prior_sessions)

            conversation = self._get_compressed_history(chat_id)

        return ContextFrame(
            identity=identity,
            project_truth=project_truth,
            relationship="" if memory_decision.suppress_legacy_authority else relationship_text,
            relationship_truth=(
                self._relationship_truth(relationship_text)
                if memory_decision.suppress_legacy_authority else ""
            ),
            evidence=evidence_text,
            knowledge="\n\n".join(knowledge_parts) if knowledge_parts else "",
            operational_memory="\n\n".join(operational_parts),
            learned_memory=learned_memory,
            memory_exposure_candidates=memory_decision.exposure_candidates,
            memory="\n\n".join(memory_parts) if memory_parts else "",
            conversation=conversation,
            instructions="",
            context_confidence=confidence,
            missing_flags=missing_flags,
        )

    # ── Web chat context ────────────────────────────────────────────

    def build_for_web_chat(
        self,
        user_text: str,
        *,
        project_id: str | None = None,
        project_ids: list[str] | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
        profile: ContextProfile = "full",
        operator_id: str | None = None,
        session_id: str | None = None,
        entity_refs: list[tuple[str, str]] | None = None,
    ) -> ContextFrame:
        """Build context for the dashboard web agent chat loop.

        Same project layers as build_for_chat() but skips Telegram-specific
        chat_id parts (conversation summary, progress doc, prior sessions,
        compressed telegram history). Uses *conversation_history* from the
        web chat repo instead.

        When *profile* is ``bootstrap``, uses orchestrator_host.md and L1
        snapshot only (mirrors Telegram bootstrap chat).
        """
        selected_project_ids = list(dict.fromkeys(
            str(item).strip() for item in (project_ids or []) if str(item).strip()
        ))
        if project_id and project_id not in selected_project_ids:
            selected_project_ids.insert(0, project_id)
        if len(selected_project_ids) > 1:
            frames = [
                self.build_for_web_chat(
                    user_text,
                    project_id=selected_id,
                    conversation_history=[],
                    profile=profile,
                    operator_id=operator_id,
                    session_id=f"{session_id or 'web'}:{selected_id}",
                    entity_refs=entity_refs,
                )
                for selected_id in selected_project_ids
            ]
            return ContextFrame(
                identity=frames[0].identity if frames else _load_system_prompt(profile),
                project_truth="\n\n".join(
                    f'<project-scope project_id="{selected_id}">\n{frame.project_truth}\n</project-scope>'
                    for selected_id, frame in zip(selected_project_ids, frames)
                    if frame.project_truth
                ),
                relationship="\n\n".join(frame.relationship for frame in frames if frame.relationship),
                relationship_truth="\n\n".join(
                    frame.relationship_truth for frame in frames if frame.relationship_truth
                ),
                evidence="\n\n".join(frame.evidence for frame in frames if frame.evidence),
                knowledge="\n\n".join(frame.knowledge for frame in frames if frame.knowledge),
                operational_memory="\n\n".join(
                    frame.operational_memory for frame in frames if frame.operational_memory
                ),
                learned_memory="\n\n".join(
                    frame.learned_memory for frame in frames if frame.learned_memory
                ),
                memory_exposure_candidates=[
                    candidate for frame in frames for candidate in frame.memory_exposure_candidates
                ],
                memory="\n\n".join(frame.memory for frame in frames if frame.memory),
                conversation=conversation_history or [],
                instructions=(
                    "The active dashboard scope contains multiple projects. Compare and synthesize "
                    "only from the explicitly fenced project scopes."
                ),
                context_confidence=(
                    sum(frame.context_confidence for frame in frames) / len(frames)
                    if frames else 1.0
                ),
                missing_flags=list(dict.fromkeys(
                    flag for frame in frames for flag in frame.missing_flags
                )),
            )

        if selected_project_ids:
            project_id = selected_project_ids[0]

        identity = _load_system_prompt(profile)

        op_mem = self._load_operator_memory_text(
            project_id=project_id,
            session_id=session_id or (f"web:{project_id}" if project_id else None),
            operator_id=operator_id,
            entity_refs=entity_refs, surface="web",
        )
        memory_decision = self._compose_governed_memory(
            op_mem, surface="web", query=user_text, profile=profile,
            operator_id=operator_id, project_id=project_id,
        )
        knowledge_parts, project_truth, evidence_text, memory_text, relationship_text, missing_flags, confidence = (
            self._build_project_knowledge(
                user_text, project_id, profile=profile, operator_id=operator_id,
                session_id=session_id, surface="web",
                suppress_legacy_authority=memory_decision.suppress_legacy_authority,
            )
        )
        combined_memory = "\n\n".join(part for part in (memory_text, memory_decision.text) if part)

        return ContextFrame(
            identity=identity,
            project_truth=project_truth,
            relationship="" if memory_decision.suppress_legacy_authority else relationship_text,
            relationship_truth=(
                self._relationship_truth(relationship_text)
                if memory_decision.suppress_legacy_authority else ""
            ),
            evidence=evidence_text,
            knowledge="\n\n".join(knowledge_parts) if knowledge_parts else "",
            operational_memory=memory_text,
            learned_memory=memory_decision.text,
            memory_exposure_candidates=memory_decision.exposure_candidates,
            memory=combined_memory,
            conversation=conversation_history or [],
            instructions="",
            context_confidence=confidence,
            missing_flags=missing_flags,
        )

    # ── Draft context ────────────────────────────────────────────────

    def build_for_draft(
        self,
        project_id: str,
        contact_id: str | None,
        task_type: str,
    ) -> ContextFrame | None:
        """Build context for draft generation. Returns None if project not found."""
        project, _ = load_project_truth(self._conn, project_id)
        if project is None:
            return None

        project_truth, evidence_text, memory_text, relationship_text, missing_flags, confidence = (
            self._load_project_layers(project_id, contact_id=contact_id,
                operator_id=self._project_operator_id(project_id), surface="draft", task_type=task_type)
        )

        knowledge = load_knowledge_for_project(project.market)
        legacy_rules = self._load_rules(operator_id=self._project_operator_id(project_id),
            project_id=project_id, contact_id=contact_id, surface="draft", task_type=task_type)
        decision = self._compose_governed_memory(
            legacy_rules, surface="draft", query=task_type, profile="full",
            operator_id=self._project_operator_id(project_id), project_id=project_id,
            contact_id=contact_id, task_type=task_type,
        )
        learned_memory = decision.text if decision.used_v2 else ""
        if not decision.used_v2 and legacy_rules:
            knowledge = (knowledge + "\n\n" + legacy_rules) if knowledge else legacy_rules
        combined_memory = "\n\n".join(
            part for part in (memory_text, learned_memory) if part
        )

        return ContextFrame(
            identity=_load_system_prompt(),
            project_truth=project_truth,
            relationship="" if decision.suppress_legacy_authority else relationship_text,
            relationship_truth=(
                self._relationship_truth(relationship_text)
                if decision.suppress_legacy_authority else ""
            ),
            evidence=evidence_text,
            knowledge=knowledge,
            operational_memory=memory_text,
            learned_memory=learned_memory,
            memory_exposure_candidates=decision.exposure_candidates,
            memory=combined_memory,
            instructions=f"Task: {task_type}. Generate a draft reply.",
            context_confidence=confidence,
            missing_flags=missing_flags,
        )

    # ── Tool context ─────────────────────────────────────────────────

    def build_for_tool(
        self,
        project_id: str,
        tool_name: str,
        *,
        operator_id: str | None,
        trusted_internal: bool = False,
    ) -> ContextFrame | None:
        """Build context for tool execution. Returns None if project not found."""
        project, _ = load_project_truth(self._conn, project_id)
        if project is None:
            return None
        authorized_operator = operator_id
        if not authorized_operator and trusted_internal:
            authorized_operator = project.operator_id
        if not authorized_operator or authorized_operator != project.operator_id:
            return ContextFrame(
                instructions=f"Context for tool: {tool_name}",
                missing_flags=["tool_operator_unauthorized"],
                context_confidence=0.0,
            )

        project_truth, evidence_text, memory_text, relationship_text, missing_flags, confidence = (
            self._load_project_layers(project_id, operator_id=authorized_operator,
                surface="tool", task_type=tool_name)
        )

        knowledge = load_knowledge_for_project(project.market)
        legacy_rules = self._load_rules(operator_id=authorized_operator,
            project_id=project_id, surface="tool", task_type=tool_name)
        decision = self._compose_governed_memory(
            legacy_rules, surface="tool", query=tool_name, profile="full",
            operator_id=authorized_operator, project_id=project_id, task_type=tool_name,
            trusted_project_operator_fallback=trusted_internal,
        )
        learned_memory = decision.text if decision.used_v2 else ""
        if not decision.used_v2 and legacy_rules:
            knowledge = (knowledge + "\n\n" + legacy_rules) if knowledge else legacy_rules
        combined_memory = "\n\n".join(
            part for part in (memory_text, learned_memory) if part
        )

        return ContextFrame(
            identity="",
            project_truth=project_truth,
            relationship="" if decision.suppress_legacy_authority else relationship_text,
            relationship_truth=(
                self._relationship_truth(relationship_text)
                if decision.suppress_legacy_authority else ""
            ),
            evidence=evidence_text,
            knowledge=knowledge,
            operational_memory=memory_text,
            learned_memory=learned_memory,
            memory_exposure_candidates=decision.exposure_candidates,
            memory=combined_memory,
            instructions=f"Context for tool: {tool_name}",
            context_confidence=confidence,
            missing_flags=missing_flags,
        )

    def build_for_advisory(
        self,
        project_id: str,
        query: str,
        *,
        operator_id: str | None = None,
        contact_id: str | None = None,
        counterparty_id: str | None = None,
        trusted_internal: bool = False,
    ) -> ContextFrame | None:
        """Build advisory context after authorizing the actual caller."""
        project, _ = load_project_truth(self._conn, project_id)
        if project is None:
            return None
        authorized_operator = operator_id
        if not authorized_operator and trusted_internal:
            authorized_operator = project.operator_id
        if not authorized_operator or authorized_operator != project.operator_id:
            return ContextFrame(
                instructions="Context for project advisory",
                missing_flags=["advisory_operator_unauthorized"],
                context_confidence=0.0,
            )
        project_truth, evidence, memory, relationship, missing, confidence = (
            self._load_project_layers(project_id, contact_id=contact_id,
                operator_id=authorized_operator, surface="advisory", task_type="project_advisory")
        )
        decision = self._compose_governed_memory(
            "", surface="advisory", query=query, profile="full",
            operator_id=authorized_operator, project_id=project_id,
            contact_id=contact_id, counterparty_id=counterparty_id,
            task_type="project_advisory",
            trusted_project_operator_fallback=trusted_internal,
        )
        combined_memory = "\n\n".join(part for part in (memory, decision.text) if part)
        return ContextFrame(
            identity="",
            project_truth=project_truth,
            relationship="" if decision.suppress_legacy_authority else relationship,
            relationship_truth=(
                self._relationship_truth(relationship)
                if decision.suppress_legacy_authority else ""
            ),
            evidence=evidence,
            knowledge=load_knowledge_for_project(project.market),
            operational_memory=memory,
            learned_memory=decision.text,
            memory_exposure_candidates=decision.exposure_candidates,
            memory=combined_memory,
            instructions="Context for project advisory",
            context_confidence=confidence,
            missing_flags=missing,
        )

    def _project_operator_id(self, project_id: str) -> str | None:
        row = self._conn.execute("SELECT operator_id FROM projects WHERE project_id=?", (project_id,)).fetchone()
        return row[0] if row and row[0] else None

    def _compose_governed_memory(
        self, legacy_text: str, *, surface: str, query: str, profile: ContextProfile,
        operator_id: str | None, project_id: str | None, contact_id: str | None = None,
        counterparty_id: str | None = None, market: str | None = None,
        organization_id: str | None = None, task_type: str | None = None,
        trusted_project_operator_fallback: bool = False,
    ):
        from src.context.governed_memory import compose_governed_memory

        return compose_governed_memory(
            self._conn, legacy_text, surface=surface, query=query, profile=profile,
            operator_id=operator_id, project_id=project_id, contact_id=contact_id,
            counterparty_id=counterparty_id, organization_id=organization_id,
            task_type=task_type,
            trusted_project_operator_fallback=trusted_project_operator_fallback,
        )

    def _inject_governed_memory(self, legacy_text: str, **kwargs) -> str:
        return self._compose_governed_memory(legacy_text, **kwargs).text

    @staticmethod
    def _relationship_truth(relationship_text: str) -> str:
        """Preserve non-authoritative contact/relationship observations only."""
        return "\n".join(
            line for line in relationship_text.splitlines()
            if line.startswith("Positive:")
        )

    # ── Shared layer loading ─────────────────────────────────────────

    def _build_project_knowledge(
        self,
        user_text: str,
        project_id: str | None,
        *,
        profile: ContextProfile = "full",
        chat_id: int | None = None,
        operator_id: str | None = None,
        session_id: str | None = None,
        suppress_legacy_authority: bool = False,
        surface: str = "chat",
    ) -> tuple[list[str], str, str, str, str, list[str], float]:
        """Build knowledge parts + project layers for both chat and web_chat.

        Returns (knowledge_parts, project_truth, evidence_text, memory_text,
                 relationship_text, missing_flags, confidence).
        """
        knowledge_parts: list[str] = []
        if not suppress_legacy_authority:
            rules = self._load_rules(operator_id=operator_id, project_id=project_id,
                                     surface=surface, task_type="chat")
            if rules:
                knowledge_parts.append(f"<rules>\n{rules}\n</rules>")

        if profile == "bootstrap":
            if not suppress_legacy_authority:
                agent_notes = self._load_agent_notes()
                if agent_notes:
                    knowledge_parts.append(agent_notes)

            project_truth = ""
            missing_flags: list[str] = []
            confidence = 1.0
            if project_id:
                project_truth, missing_flags, confidence = self._load_l1_snapshot(project_id)

            knowledge_parts = _trim_knowledge(knowledge_parts)
            return knowledge_parts, project_truth, "", "", "", missing_flags, confidence

        wiki = self._wiki_search(user_text, project_id=project_id)
        if wiki:
            knowledge_parts.append(f"<wiki-context>\n{wiki}\n</wiki-context>")

        if profile != "minimal":
            if not suppress_legacy_authority:
                agent_notes = self._load_agent_notes()
                if agent_notes:
                    knowledge_parts.append(agent_notes)

        if not project_id and profile == "full":
            portfolio_ctx = self._get_portfolio_scan_context()
            if portfolio_ctx:
                knowledge_parts.append(portfolio_ctx)

        project_truth = ""
        evidence_text = ""
        memory_text = ""
        relationship_text = ""
        missing_flags = []
        confidence = 1.0

        if project_id:
            project_truth, evidence_text, memory_text, relationship_text, missing_flags, confidence = (
                self._load_project_layers(project_id, operator_id=operator_id,
                    surface=surface, task_type="chat")
            )
            corrections_text = self._load_corrections(project_id)
            if corrections_text:
                evidence_text = (evidence_text + "\n\n" + corrections_text).strip()
            project_wiki = self._get_project_wiki(project_id)
            if project_wiki:
                knowledge_parts.append(project_wiki)
            from src.context.knowledge_loader import load_knowledge_for_project
            try:
                _mrow = self._conn.execute(
                    "SELECT market FROM projects WHERE project_id = ?", (project_id,)
                ).fetchone()
                _market = (_mrow["market"] if _mrow and _mrow["market"] else None)
            except Exception:
                _market = None
            market_knowledge = load_knowledge_for_project(_market)
            if market_knowledge:
                knowledge_parts.append(f"<market-knowledge>\n{market_knowledge}\n</market-knowledge>")
            skill_output = self._run_skills_for_project(project_id)
            if skill_output:
                knowledge_parts.append(skill_output)
            followup_text = self._load_open_followups(project_id)
            if followup_text:
                knowledge_parts.append(followup_text)
            sla_text = self._compute_sla_countdown(project_id)
            if sla_text:
                project_truth = project_truth + "\n" + sla_text

            # Topic-aware wiki search using project's next_action
            next_action = self._extract_next_action(project_id)
            if next_action and len(next_action) > 10:
                topic_kb = self._wiki_search(next_action, project_id=project_id)
                if topic_kb:
                    knowledge_parts.append(f"<wiki-topic>\n{topic_kb}\n</wiki-topic>")

        knowledge_parts = _trim_knowledge(knowledge_parts)
        return knowledge_parts, project_truth, evidence_text, memory_text, relationship_text, missing_flags, confidence

    def _load_project_layers(
        self,
        project_id: str,
        *,
        contact_id: str | None = None,
        operator_id: str | None = None,
        surface: str = "chat",
        task_type: str | None = None,
    ) -> tuple[str, str, str, str, list[str], float]:
        """Load all project context layers.

        Returns (project_truth, evidence, memory, relationship, missing_flags, confidence).
        """
        project, truth_missing = load_project_truth(self._conn, project_id)
        if project is None:
            return "", "", "", "", ["project_not_found"], 0.0

        # Format project truth as text block
        project_truth_lines = [
            f"Project: {project.project_id} ({project.client_name or 'Unknown'})",
            f"Status: {project.project_status.value} | Ball: {project.ball_with.value}",
            f"Milestone: {project.current_milestone.value if project.current_milestone else 'Not set'}",
            f"Next action: {project.next_action or 'Not set'}",
            f"Market: {project.market or 'Not set'}"
            + (f"  |  Tier: {project.project_tier}" if project.project_tier else ""),
        ]
        if project.client_email:
            project_truth_lines.append(f"Client email: {project.client_email}")
        project_truth = "\n".join(project_truth_lines)

        # Working memory → text
        wm, wm_missing = load_working_memory(self._conn, project_id)
        memory_parts: list[str] = []
        if wm.recent_thread_summary:
            memory_parts.append(f"Thread: {wm.recent_thread_summary}")
        if wm.pending_questions:
            memory_parts.append("Pending Qs: " + "; ".join(wm.pending_questions))
        if wm.open_decisions:
            memory_parts.append("Open decisions: " + "; ".join(wm.open_decisions))
        if wm.latest_operator_instruction:
            memory_parts.append(f"Operator instruction: {wm.latest_operator_instruction}")
        if wm.stale_keys:
            memory_parts.append(f"Stale keys: {', '.join(wm.stale_keys)}")
        memory_text = "\n".join(memory_parts)

        # Evidence → text (cap at 10 items)
        evidence_list, ev_missing = load_evidence(self._conn, project_id)
        evidence_text = "\n".join(
            f"[{e.evidence_type.value if hasattr(e.evidence_type, 'value') else e.evidence_type}] "
            f"{e.content_summary} ({e.evidence_at})"
            for e in evidence_list[:EVIDENCE_DISPLAY_LIMIT]
        )

        # Relationship → text
        from src.context.governed_memory import compatibility_memory_request
        request = compatibility_memory_request(self._conn, operator_id=operator_id,
            project_id=project_id, contact_id=contact_id, surface=surface, task_type=task_type)
        rel, rel_missing = load_relationship(self._conn, contact_id, project_id, memory_request=request)
        rel_parts: list[str] = []
        if rel.tone:
            rel_parts.append(f"Tone: {rel.tone}")
        if rel.preferred_language:
            rel_parts.append(f"Language: {rel.preferred_language}")
        if rel.preferred_greeting:
            rel_parts.append(f"Greeting: {rel.preferred_greeting}")
        if rel.avoid_phrases:
            rel_parts.append(f"Avoid: {', '.join(rel.avoid_phrases)}")
        if rel.positive_signals:
            rel_parts.append(f"Positive: {', '.join(rel.positive_signals[:5])}")
        relationship_text = "\n".join(rel_parts)

        # Confidence scoring (identical to builder.py)
        all_missing = truth_missing + wm_missing + rel_missing + ev_missing
        confidence = 1.0
        if "no_milestone_set" in all_missing:
            confidence -= 0.2
        if "no_next_action" in all_missing:
            confidence -= 0.1
        if "no_evidence" in all_missing:
            confidence -= 0.3
        if "no_working_memory" in all_missing:
            confidence -= 0.05
        if "no_response_pattern" in all_missing:
            confidence -= 0.05
        confidence = max(0.0, confidence)

        return project_truth, evidence_text, memory_text, relationship_text, all_missing, confidence

    def _load_l1_snapshot(
        self, project_id: str,
    ) -> tuple[str, list[str], float]:
        """L1 project truth only — seven-line snapshot for bootstrap profile."""
        project, missing = load_project_truth(self._conn, project_id)
        if project is None:
            return "", ["project_not_found"], 0.0

        project_truth_lines = [
            f"Project: {project.project_id} ({project.client_name or 'Unknown'})",
            f"Status: {project.project_status.value} | Ball: {project.ball_with.value}",
            f"Milestone: {project.current_milestone.value if project.current_milestone else 'Not set'}",
            f"Next action: {project.next_action or 'Not set'}",
            f"Market: {project.market or 'Not set'}"
            + (f"  |  Tier: {project.project_tier}" if project.project_tier else ""),
        ]
        if project.client_email:
            project_truth_lines.append(f"Client email: {project.client_email}")
        project_truth = "\n".join(project_truth_lines)

        confidence = 1.0
        if "no_milestone_set" in missing:
            confidence -= 0.2
        if "no_next_action" in missing:
            confidence -= 0.1
        return project_truth, missing, max(0.0, confidence)

    def _load_operator_memory_text(
        self,
        *,
        project_id: str | None = None,
        chat_id: int | None = None,
        session_id: str | None = None,
        operator_id: str | None = None,
        entity_refs: list[tuple[str, str]] | None = None,
        surface: str = "chat",
    ) -> str:
        """Load unified operator memory when OPERATOR_MEMORY_ENABLED."""
        from src.memory.reform_flags import operator_memory_enabled
        if not operator_memory_enabled():
            return ""
        from src.agent.operator_identity import resolve_operator_id
        from src.memory.operator_memory_store import OperatorMemoryStore

        op_id = operator_id or resolve_operator_id(self._conn, chat_id=chat_id)
        store = OperatorMemoryStore(self._conn, op_id)
        from src.context.governed_memory import resolve_authorization_context
        from src.memory.retriever import MemoryRetrievalRequest
        # Do not turn an unresolved context operator into projected authority.
        refs = {}
        for kind in ("contact", "counterparty", "organisation"):
            values = {value for scope, value in entity_refs or [] if scope == kind and value}
            if len(values) == 1:
                refs[kind] = next(iter(values))
        auth = resolve_authorization_context(self._conn, operator_id=operator_id,
            project_id=project_id, contact_id=refs.get("contact"),
            counterparty_id=refs.get("counterparty"), organization_id=refs.get("organisation"))
        request = MemoryRetrievalRequest(operator_id=auth.operator_id, project_id=auth.project_id,
            contact_id=auth.contact_id, counterparty_id=auth.counterparty_id,
            organization_id=auth.organization_id, market=auth.market,
            surface=surface, task_type="chat", at=datetime.now(timezone.utc))
        return store.format_for_prompt(
            project_id=project_id,
            chat_id=chat_id,
            session_id=session_id,
            entity_refs=entity_refs,
            memory_request=request,
        )

    # ── Conversation helpers ─────────────────────────────────────────

    def _get_portfolio_scan_context(self) -> str | None:
        """Heavy portfolio scan for full profile when no project is in scope."""
        try:
            from src.portfolio.portfolio_scan import scan_portfolio

            summary = scan_portfolio(self._conn)
            if not summary.items:
                return None
            lines = [
                f"Active projects: {summary.total_active}",
                f"Blocked: {summary.blocked_count}, Waiting: {summary.waiting_count}",
            ]
            for item in summary.items[:15]:
                flags = ", ".join(item.flags) if item.flags else "none"
                lines.append(
                    f"- {item.project_id}: score={item.priority_score:.0f}, "
                    f"status={item.project_status}, ball={item.ball_with or '?'}, "
                    f"flags={flags}"
                )
            return "<portfolio-scan>\n" + "\n".join(lines) + "\n</portfolio-scan>"
        except Exception:
            logger.debug("Portfolio scan failed", exc_info=True)
            return None

    def _get_conversation_summary(self, chat_id: int) -> str | None:
        """Load rolling conversation summary from working_memory."""
        from src.repositories.working_memory_repo import get_memory
        try:
            entry = get_memory(self._conn, "_system", f"conversation_summary_{chat_id}")
            if entry and entry["memory_value"]:
                return f"<conversation-history>\n{entry['memory_value']}\n</conversation-history>"
        except Exception:
            logger.debug("Failed to load conversation summary", exc_info=True)
        return None

    def _get_progress(self, chat_id: int, *, active_project_id: str | None = None) -> str | None:
        """Load structured progress document from working_memory.

        When *active_project_id* is set, only that project's entry is
        returned so the LLM does not act on unrelated projects.
        """
        from src.repositories.working_memory_repo import get_memory
        try:
            entry = get_memory(self._conn, "_system", f"agent_progress_{chat_id}")
            if not entry or not entry["memory_value"]:
                return None
            progress = json.loads(entry["memory_value"])
            if not isinstance(progress, dict) or not progress.get("projects"):
                return None
            if active_project_id and isinstance(progress["projects"], dict):
                filtered = {k: v for k, v in progress["projects"].items()
                            if k == active_project_id}
                if not filtered:
                    return None
                progress = {**progress, "projects": filtered}
            return f"<agent-progress>\n{json.dumps(progress, indent=2)}\n</agent-progress>"
        except Exception:
            logger.debug("Failed to load progress document", exc_info=True)
        return None

    def _get_prior_sessions(self, chat_id: int, *, active_project_id: str | None = None) -> str | None:
        """Load recent session summaries for cross-session continuity.

        When *active_project_id* is set, the ``Projects:`` tag on each
        session line is stripped so the LLM is not tempted to act on
        unrelated projects.
        """
        try:
            from src.repositories.session_summary_repo import get_recent_summaries
            rows = get_recent_summaries(self._conn, chat_id=chat_id, limit=3)
            if not rows:
                return None
            parts = []
            for r in rows:
                line = f"[{r['ended_at']}] {r['summary_text']}"
                if not active_project_id and r.get("projects_discussed"):
                    line += f" | Projects: {r['projects_discussed']}"
                parts.append(line)
            return "<prior-sessions>\n" + "\n".join(parts) + "\n</prior-sessions>"
        except Exception:
            logger.debug("Failed to load prior sessions", exc_info=True)
        return None

    def _get_compressed_history(self, chat_id: int, limit: int = CONVERSATION_HISTORY_LIMIT) -> list[dict[str, Any]]:
        """Load and compress recent conversation from telegram_messages."""
        from src.context.compression import micro_compact_tool_results, compress_conversation
        try:
            rows = self._conn.execute(
                """SELECT direction, text, message_kind
                   FROM telegram_messages
                   WHERE chat_id = ? AND text IS NOT NULL AND text != ''
                     AND message_kind IN ('text', 'system', 'command', 'command_reply')
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (chat_id, limit),
            ).fetchall()
            messages: list[dict[str, Any]] = []
            for row in reversed(rows):
                data = dict(row)
                role = "user" if data["direction"] == "inbound" else "assistant"
                messages.append({"role": role, "content": data["text"]})
            messages = micro_compact_tool_results(messages)
            messages = compress_conversation(messages)
            return messages
        except Exception:
            logger.debug("Failed to load conversation history", exc_info=True)
        return []

    # ── Knowledge helpers ────────────────────────────────────────────

    def _load_rules(self, *, operator_id=None, project_id=None, contact_id=None,
                    surface="chat", task_type=None) -> str:
        """Load learned rules formatted for LLM prompt."""
        try:
            from src.repositories.learned_rules_repo import get_applicable_rules, format_rules_for_prompt
            from src.llm.client import set_active_rules
            from src.memory.retriever import MemoryRetrievalRequest
            from src.context.governed_memory import resolve_authorization_context
            auth = resolve_authorization_context(self._conn, operator_id=operator_id,
                project_id=project_id, contact_id=contact_id)
            request = MemoryRetrievalRequest(operator_id=auth.operator_id, project_id=auth.project_id,
                contact_id=auth.contact_id, counterparty_id=auth.counterparty_id,
                organization_id=auth.organization_id, market=auth.market,
                surface=surface, task_type=task_type, at=datetime.now(timezone.utc))
            rules = get_applicable_rules(self._conn, memory_request=request)
            set_active_rules([r["rule_id"] for r in rules])
            return format_rules_for_prompt(rules)
        except Exception:
            logger.debug("Failed to load learned rules", exc_info=True)
        return ""

    def _load_agent_notes(self) -> str:
        """Load LLM-curated agent notes for injection into system prompt."""
        try:
            from src.learning.legacy_retirement import legacy_injection_allowed
            if not legacy_injection_allowed(self._conn, "agent_notes"):
                return ""
            from src.repositories.agent_notes_repo import list_notes, total_chars, CHAR_LIMIT
            notes = list_notes(self._conn)
            if not notes:
                return ""
            total = total_chars(self._conn)
            lines = [f"- {n['note_key']}: {n['note_value']}" for n in notes]
            return f"<agent-memory>\n" + "\n".join(lines) + "\n</agent-memory>"
        except Exception:
            logger.debug("Failed to load agent notes", exc_info=True)
        return ""

    def _load_corrections(self, project_id: str) -> str:
        """Load recent agent self-corrections for a project (L3 memory)."""
        try:
            from src.repositories.project_corrections_repo import list_corrections
            corrections = list_corrections(self._conn, project_id)
            if not corrections:
                return ""
            lines = ["[Known past corrections]"]
            for c in corrections:
                lines.append(
                    f"• {c['field']}: DB said {c['wrong_value']}, "
                    f"corrected to {c['correct_value']} via evidence "
                    f"({c['created_at'][:10]})"
                )
            return "\n".join(lines)
        except Exception:
            logger.exception("Failed to load corrections for %s", project_id)
            return ""

    _MILESTONE_DOMAIN: dict[str, str] = {
        "M1_NetworkSetup": "fix-connectivity",
        "M2_UATSessionConfiguration": "fix-connectivity",
        "M3_UATEnvironmentAccess": "conformance",
        "M4_ApplicationTesting": "fix-connectivity",
        "M5_ConformanceTestCases": "conformance",
        "M6_TestReportAndProductionPreparation": "conformance",
        "M7_LogReviewAndProductionSessionDelivery": "fix-connectivity",
        "M8_GoLive": "b2b",
    }

    def _wiki_search(self, query: str, *, project_id: str | None = None) -> str:
        """Search wiki index for relevant content, biasing domain from milestone when known."""
        if len((query or "").strip()) < 4:
            return ""
        domain_filter: str | None = None
        if project_id:
            try:
                row = self._conn.execute(
                    "SELECT current_milestone FROM projects WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if row:
                    domain_filter = self._MILESTONE_DOMAIN.get(row["current_milestone"])
            except Exception:
                pass
        try:
            market: str | None = None
            if project_id:
                try:
                    project_row = self._conn.execute(
                        "SELECT market FROM projects WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()
                    market = project_row["market"] if project_row else None
                except Exception:
                    pass
            result = search_wiki(WikiSearchRequest(
                query=query,
                mode=(
                    WikiRetrievalMode.PROJECT_CONTEXT
                    if project_id else WikiRetrievalMode.INTERNAL
                ),
                project_id=project_id,
                market=market,
                domains=(domain_filter,) if domain_filter else (),
                top_k=5,
                include_graph_neighbors=True,
            ))
            return result.formatted
        except Exception:
            logger.debug("Wiki search failed", exc_info=True)
        return ""

    def _extract_next_action(self, project_id: str) -> str:
        """Get project's next_action for topic-aware wiki search."""
        try:
            row = self._conn.execute(
                "SELECT next_action FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return (row["next_action"] or "") if row else ""
        except Exception:
            return ""

    def _get_project_wiki(self, project_id: str) -> str:
        """Load wiki pages related to this project via graph traversal."""
        try:
            graph = get_wiki_graph()
            pages = graph.related_to_project(project_id, self._conn)
            if not pages:
                return ""
            parts = [
                f"**{p['title']}** ({p['type']}, {p.get('status', 'active')})"
                for p in pages
            ]
            return "<wiki-project-context>\n" + "\n".join(parts) + "\n</wiki-project-context>"
        except Exception:
            logger.debug("Project wiki lookup failed", exc_info=True)
        return ""

    def _run_skills_for_project(self, project_id: str) -> str:
        """E3: Auto-run deterministic skills for the project and format as context block."""
        try:
            from datetime import datetime, timezone
            from src.context.layer1_truth import load_project_truth
            from src.skills.status_assessment import StatusAssessmentSkill
            from src.skills.aging_detection import AgingDetectionSkill

            project, _ = load_project_truth(self._conn, project_id)
            if project is None:
                return ""

            parts: list[str] = []

            # Status assessment
            status_skill = StatusAssessmentSkill()
            status_brief = status_skill.assess(project, self._conn)
            if status_brief.observations:
                parts.append("Status: " + "; ".join(status_brief.observations))
            if status_brief.recommendations:
                parts.append("Recommendations: " + "; ".join(status_brief.recommendations))

            # Aging detection — compute days_inactive from activity_log
            days_inactive = None
            try:
                row = self._conn.execute(
                    "SELECT MAX(created_at) as last_at FROM activity_log WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if row and row["last_at"]:
                    last_dt = datetime.fromisoformat(row["last_at"])
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    days_inactive = (datetime.now(timezone.utc) - last_dt).days
            except Exception:
                pass

            aging_skill = AgingDetectionSkill()
            aging_brief = aging_skill.classify_project_aging(project, days_inactive)
            if aging_brief.observations:
                parts.append("Aging: " + "; ".join(aging_brief.observations))
            if aging_brief.recommendations:
                parts.append("Aging action: " + "; ".join(aging_brief.recommendations))

            # FIX onboarding skill — uses learned durations from milestone_history
            from src.skills.fix_onboarding import FIXOnboardingSkill
            fix_skill = FIXOnboardingSkill()
            milestone_val = project.current_milestone.value if project.current_milestone else None
            ball_val = project.ball_with.value if project.ball_with else None
            fix_brief = fix_skill.assess(
                milestone_val, ball_val,
                days_since_last_activity=days_inactive,
                conn=self._conn,
            )
            if fix_brief.observations:
                parts.append("FIX onboarding: " + "; ".join(fix_brief.observations))
            if fix_brief.recommendations:
                parts.append("FIX actions: " + "; ".join(fix_brief.recommendations))

            if not parts:
                return ""
            return "<skill-assessment>\n" + "\n".join(parts) + "\n</skill-assessment>"
        except Exception:
            logger.debug("Skill auto-run failed for %s", project_id, exc_info=True)
            return ""

    def _load_open_followups(self, project_id: str) -> str:
        """Load open follow-ups for a project and format as XML block."""
        try:
            from src.portfolio.followup_tracker import get_open_followups
            from datetime import date

            items = get_open_followups(self._conn, project_id)
            if not items:
                return ""
            today = date.today().isoformat()
            lines = []
            for fup in items[:10]:
                due = fup.get("due_date") or "no date"
                overdue = ""
                if fup.get("due_date") and fup["due_date"] < today:
                    overdue = " [OVERDUE]"
                esc = fup.get("escalation_level", 0)
                lines.append(
                    f"  - [{fup['followup_id']}] {fup['description']} "
                    f"(due: {due}{overdue}, L{esc})"
                )
            return "<open-followups>\n" + "\n".join(lines) + "\n</open-followups>"
        except Exception:
            logger.debug("Failed to load follow-ups for %s", project_id, exc_info=True)
            return ""

    def _compute_sla_countdown(self, project_id: str) -> str:
        """Compute SLA countdown from target_go_live or sla_limit."""
        try:
            from datetime import date
            from src.repositories.project_repo import get_project_field

            target = get_project_field(self._conn, project_id, "target_go_live")
            if not target:
                return ""
            today = date.today()
            target_date = date.fromisoformat(str(target)[:10])
            days_remaining = (target_date - today).days
            if days_remaining < 0:
                return f"SLA: Go-live target {target} (OVERDUE by {-days_remaining} days)"
            return f"SLA: Go-live target {target} ({days_remaining} days remaining)"
        except Exception:
            logger.debug("Failed to compute SLA for %s", project_id, exc_info=True)
            return ""


def get_wiki_graph():
    """Lazy loader for WikiGraph — patchable at module level for tests."""
    from src.wiki.graph import get_wiki_graph as _get
    return _get()


@lru_cache(maxsize=2)
def _load_system_prompt(profile: ContextProfile = "full") -> str:
    """Load the system prompt for the given context profile (cached)."""
    if profile == "bootstrap":
        return (BASE_DIR / "prompts" / "orchestrator_host.md").read_text(encoding="utf-8")
    return (BASE_DIR / "prompts" / "unified_agent.md").read_text(encoding="utf-8")
