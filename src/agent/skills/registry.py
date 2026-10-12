"""
Skill registry — declarative map of operator workflows.

Design:
  - Host orchestrator is READ + delegate only (no direct write tools).
  - Every write workflow is a Skill bound to a SpecialistType + tool allowlist.
  - Skills are the source of truth for specialist tool bundles.
  - CURATOR_ONLY_TOOLS are intentionally non-agent (wiki editor / outreach).
  - SURFACE_ONLY_TOOLS are exposed only by dedicated product-surface runtimes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from src.agent.specialists.contracts import SpecialistType
from src.tools.registry import ALL_TOOLS, TOOL_BY_NAME, _to_openai_schema


@dataclass(frozen=True)
class SkillDefinition:
    """One invokable workflow skill."""

    skill_id: str
    title: str
    description: str
    specialist_type: SpecialistType
    tools: frozenset[str]
    triggers: tuple[str, ...] = ()
    max_rounds: int = 4
    prompt_file: str | None = None  # relative to prompts/
    host_invokable: bool = True
    is_write: bool = True

    def matches(self, text: str) -> bool:
        if not text or not self.triggers:
            return False
        for pattern in self.triggers:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return True
        return False


# ── Tool bundles ─────────────────────────────────────────────────────

_SKILL_NEW_PROJECT_TOOLS = frozenset({
    "create_project", "confirm_create_project", "cancel_create_project",
    "query_projects", "get_project_status", "think",
})

_SKILL_PROJECT_WRITE_TOOLS = frozenset({
    "update_project", "update_project_batch", "confirm_update", "cancel_update",
    "undo_update", "add_note", "log_evidence",
    "get_project_status", "get_project_truth", "get_activity_log", "get_working_memory",
    "search_emails", "get_email_body", "get_project_emails", "think",
})

# Composite: email evidence → stage/confirm project truth in one specialist run
_SKILL_EMAIL_TO_TRUTH_TOOLS = frozenset({
    "project_reconcile", "get_project_truth", "think",
})

_SKILL_FOLLOWUP_TOOLS = frozenset({
    "manage_followup", "create_internal_action", "list_internal_actions",
    "complete_internal_action", "query_projects", "get_project_status", "think",
})

_SKILL_CONTACT_TOOLS = frozenset({
    "add_contact", "list_contacts", "find_internal_contact",
    "get_relationship_prefs", "get_project_status", "think",
})

_SKILL_DRAFT_TOOLS = frozenset({
    "create_email_draft", "draft_reply", "list_drafts", "get_draft", "edit_draft", "discard_draft",
    "list_reference_docs", "attach_reference_doc", "read_reference_doc",
    # Guidelines / FIX domain without a separate research hop
    "wiki_search", "wiki_read",
    "get_project_status", "get_project_truth",
    "search_emails", "get_email_body", "get_project_emails", "list_contacts",
    "find_internal_contact", "get_relationship_prefs", "think",
})

_SKILL_SUPPORT_TOOLS = frozenset({
    "create_ticket", "update_ticket", "search_tickets", "get_advisory",
    "get_ticket_evidence", "search_ticket_evidence", "record_resolution",
    "wiki_search", "wiki_read", "think",
})

_SKILL_RESEARCH_TOOLS = frozenset({
    "query_projects", "get_project_status", "get_project_truth",
    "wiki_search", "wiki_read", "load_project_context",
    "list_reference_docs", "read_reference_doc", "attach_reference_doc",
    "search_emails", "get_email_body", "get_project_emails",
    "get_ticket_evidence", "search_ticket_evidence", "list_contacts", "think",
})

_SKILL_STATUS_TOOLS = frozenset({
    "get_project_status", "get_project_truth", "query_projects",
    "get_activity_log", "get_working_memory", "get_advisory",
    "get_project_fix_sessions", "get_fix_session_history",
    "get_pending_fix_session_candidates", "get_fix_environment_resources",
    "wiki_search", "think",
})

_SKILL_PORTFOLIO_TOOLS = frozenset({
    "query_projects", "get_project_status", "manage_followup",
    "list_work_items", "list_internal_actions", "think",
})

_SKILL_EMAIL_TOOLS = frozenset({
    "report_email_analysis", "manage_followup", "load_project_context",
    "wiki_search", "get_project_status", "search_emails", "get_email_body", "think",
})

_SKILL_MEMORY_TOOLS = frozenset({"agent_memory", "think"})

_SKILL_PERSONAL_WORK_TOOLS = frozenset({
    "create_personal_work_item", "list_personal_work_items",
    "manage_personal_work_item", "list_calendar_events",
    "get_calendar_event", "prepare_meeting", "ingest_meeting_notes",
    "propose_calendar_action", "think",
})

# Host: read + orchestration only — NO write primitives.
_HOST_TOOLS = frozenset({
    "think", "delegate",
    "get_project_truth", "get_project_status", "query_projects",
    "search_emails", "get_email_body", "get_project_emails", "load_project_context",
    "get_activity_log", "get_working_memory",
    "session_search", "get_advisory", "search_tickets", "read_artifact",
})

# Explicitly non-agent / curator / external push — not silently dead
CURATOR_ONLY_TOOLS = frozenset({
    "wiki_create", "wiki_update", "wiki_link",
    "create_outreach_email_draft", "push_telegram_review",
})

# Dedicated AI Workspace runtime tools; intentionally excluded from host/specialist bundles.
SURFACE_ONLY_TOOLS = frozenset({
    "portfolio_attention",
    "search_project_parties",
    "query_portfolio",
    "get_project_brief",
    "search_project_timeline",
    "list_project_work",
    "list_unanswered_threads",
    "get_project_evidence_audit",
    "assess_milestone_readiness",
    "web_search",
})


# ── Registered skills ────────────────────────────────────────────────

SKILLS: tuple[SkillDefinition, ...] = (
    SkillDefinition(
        skill_id="new_project",
        title="New Project Intake",
        description=(
            "Create a new FIX onboarding project: stage with create_project, "
            "then confirm_create_project after operator OK."
        ),
        specialist_type=SpecialistType.WRITE,
        tools=_SKILL_NEW_PROJECT_TOOLS,
        triggers=(
            r"\bnew\s+project\b", r"\bsetup\s+(?:a\s+)?(?:new\s+)?project\b",
            r"\bcreate\s+(?:a\s+)?(?:new\s+)?project\b", r"\b/newproject\b",
            r"新專案|新建項目|建立專案|建立項目", r"confirm_create_project|確認建立",
        ),
        max_rounds=5,
        prompt_file="skills/new_project.md",
    ),
    SkillDefinition(
        skill_id="email_to_truth",
        title="Email → Project Truth",
        description=(
            "Load project emails (including unlinked subject tags), extract facts, "
            "stage update_project_batch/notes, confirm when operator already authorized."
        ),
        specialist_type=SpecialistType.WRITE,
        tools=_SKILL_EMAIL_TO_TRUTH_TOOLS,
        triggers=(
            r"base(?:d)?\s+on\s+(?:the\s+)?email",
            r"update\s+(?:the\s+)?project\s+info",
            r"根據.*(?:email|郵件)",
            r"用.*(?:email|郵件).*更新",
            r"from\s+(?:the\s+)?(?:provisioning|email)",
            r"email_to_truth",
        ),
        max_rounds=3,
        prompt_file="skills/email_to_truth.md",
    ),
    SkillDefinition(
        skill_id="project_write",
        title="Project Field Write",
        description=(
            "Update project truth fields. Stage then confirm_update. "
            "May read emails for evidence."
        ),
        specialist_type=SpecialistType.WRITE,
        tools=_SKILL_PROJECT_WRITE_TOOLS,
        triggers=(
            r"\bupdate_project\b", r"\bconfirm_update\b",
            r"\b(update|set|change|改|更新|推進)\b.+\b(milestone|ball_with|status|next_action)\b",
            r"ball\s*with", r"推進.*M[1-8]|milestone\s*→",
            r"結案|sign[\s-]?off|close\s+project|project_status",
        ),
        max_rounds=6,
        prompt_file="skills/project_write.md",
    ),
    SkillDefinition(
        skill_id="followup",
        title="Internal Follow-up",
        description="Create/complete follow-ups and internal actions.",
        specialist_type=SpecialistType.PORTFOLIO,
        tools=_SKILL_FOLLOWUP_TOOLS,
        triggers=(
            r"\b(?:create|add|open|complete|cancel|manage|track)\b.{0,30}\bfollow[\s-]?ups?\b",
            r"\bmanage_followup\b", r"\b/track\b",
            r"(?:建立|新增|完成|取消).{0,20}(?:跟進|追蹤事項)|internal_action",
        ),
        max_rounds=4,
        prompt_file="skills/followup.md",
    ),
    SkillDefinition(
        skill_id="contact",
        title="Contact Registry",
        description="Add/list contacts and relationship prefs.",
        specialist_type=SpecialistType.WRITE,
        tools=_SKILL_CONTACT_TOOLS,
        triggers=(r"\badd_contact\b", r"\blist_contacts\b", r"\bcontact\b", r"聯絡人|聯繫人"),
        max_rounds=3,
        prompt_file="skills/contact.md",
    ),
    SkillDefinition(
        skill_id="draft",
        title="Email Draft",
        description="Create ad-hoc or project email drafts, then list/edit/discard them.",
        specialist_type=SpecialistType.DRAFT,
        tools=_SKILL_DRAFT_TOOLS,
        triggers=(r"\bdraft\b", r"\bdraft_reply\b", r"草擬|草稿|寫封信|回信"),
        max_rounds=5,
        prompt_file="skills/draft.md",
    ),
    SkillDefinition(
        skill_id="support",
        title="Support Tickets",
        description="Create/update tickets, advisory, resolution records.",
        specialist_type=SpecialistType.SUPPORT,
        tools=_SKILL_SUPPORT_TOOLS,
        triggers=(r"\bticket\b", r"\b/ticket\b", r"\b/support\b", r"\b/resolve\b", r"工單|支援"),
        max_rounds=4,
        prompt_file="skills/support.md",
    ),
    SkillDefinition(
        skill_id="research",
        title="Research",
        description="Read-only research across wiki, emails, and project context.",
        specialist_type=SpecialistType.RESEARCH,
        tools=_SKILL_RESEARCH_TOOLS,
        triggers=(
            r"\bresearch\b", r"\bwiki\b", r"\bsearch\b.*\bemail",
            r"email.*search|搜.*email|搜.*郵件", r"點處理|how\s+to|guideline",
            r"conformance.{0,30}(?:requirements?|rules?|test cases?|guide|how|why)",
            r"(?:要求|規則|測試案例|點做).{0,30}conformance",
        ),
        max_rounds=6,
        prompt_file="specialists/research.md",
        is_write=False,
    ),
    SkillDefinition(
        skill_id="status",
        title="Project Status",
        description="Read project status, activity log, working memory, and FIX session state.",
        specialist_type=SpecialistType.STATUS,
        tools=_SKILL_STATUS_TOOLS,
        triggers=(r"\bstatus\b", r"\b/status\b", r"狀態|而家點"),
        max_rounds=3,
        prompt_file="skills/status.md",
        is_write=False,
    ),
    SkillDefinition(
        skill_id="portfolio",
        title="Portfolio Scan",
        description="Cross-project queries and work items.",
        specialist_type=SpecialistType.PORTFOLIO,
        tools=_SKILL_PORTFOLIO_TOOLS,
        triggers=(r"\bportfolio\b", r"\b/scan\b", r"所有項目|全部項目"),
        max_rounds=3,
        prompt_file="skills/portfolio.md",
        is_write=False,
    ),
    SkillDefinition(
        skill_id="email",
        title="Email Analysis",
        description="Inbound email classification specialist.",
        specialist_type=SpecialistType.EMAIL,
        tools=_SKILL_EMAIL_TOOLS,
        triggers=(r"\breport_email_analysis\b", r"郵件分析"),
        max_rounds=5,
        prompt_file="specialists/email.md",
        is_write=False,
    ),
    SkillDefinition(
        skill_id="memory",
        title="Operator Memory",
        description="Persist operator preferences via agent_memory (not project events).",
        specialist_type=SpecialistType.WRITE,
        tools=_SKILL_MEMORY_TOOLS,
        triggers=(r"\bagent_memory\b", r"remember\s+that|記住", r"preference"),
        max_rounds=2,
        prompt_file="skills/memory.md",
    ),
    SkillDefinition(
        skill_id="personal_work",
        title="Personal Work Management",
        description="Create and manage personal tasks, commitments, waiting-for items, and reminders.",
        specialist_type=SpecialistType.WRITE,
        tools=_SKILL_PERSONAL_WORK_TOOLS,
        triggers=(
            r"\btask\b", r"\bcommitment\b", r"waiting[\s-]?for",
            r"\bremind\s+me\b", r"\bcalendar\b", r"\bmeetings?\b",
            r"待辦|任務|承諾|等候|提醒我|行事曆|日曆|會議",
        ),
        max_rounds=4,
        prompt_file="skills/personal_work.md",
    ),
)

_SKILLS_BY_ID: dict[str, SkillDefinition] = {s.skill_id: s for s in SKILLS}

_SPECIALIST_TOOL_UNION: dict[SpecialistType, frozenset[str]] = {}
for _skill in SKILLS:
    prev = _SPECIALIST_TOOL_UNION.get(_skill.specialist_type, frozenset())
    _SPECIALIST_TOOL_UNION[_skill.specialist_type] = prev | _skill.tools


def list_skills() -> tuple[SkillDefinition, ...]:
    return SKILLS


def list_skill_ids() -> list[str]:
    return [s.skill_id for s in SKILLS if s.host_invokable]


def get_skill(skill_id: str) -> SkillDefinition | None:
    if not skill_id:
        return None
    return _SKILLS_BY_ID.get(skill_id.strip().lower())


def list_skills_for_specialist(specialist_type: SpecialistType | str) -> list[SkillDefinition]:
    if isinstance(specialist_type, str):
        try:
            specialist_type = SpecialistType(specialist_type)
        except ValueError:
            return []
    return [s for s in SKILLS if s.specialist_type == specialist_type]


def match_skills(text: str, *, write_only: bool | None = None) -> list[SkillDefinition]:
    hits = [s for s in SKILLS if s.matches(text)]
    if write_only is True:
        hits = [s for s in hits if s.is_write]
    elif write_only is False:
        hits = [s for s in hits if not s.is_write]
    # Prefer more specific skills (email_to_truth before project_write)
    priority = {"email_to_truth": 0, "new_project": 1, "draft": 2, "project_write": 3}
    hits.sort(key=lambda s: (priority.get(s.skill_id, 9), not s.is_write, s.skill_id))
    return hits


def get_host_tool_names() -> frozenset[str]:
    return _HOST_TOOLS


def get_specialist_tool_names(specialist_type: SpecialistType | str) -> frozenset[str]:
    if isinstance(specialist_type, str):
        try:
            specialist_type = SpecialistType(specialist_type)
        except ValueError:
            return frozenset({"think"})
    return _SPECIALIST_TOOL_UNION.get(specialist_type, frozenset({"think"}))


def get_tools_for_skill(skill_id: str) -> list[dict]:
    skill = get_skill(skill_id)
    if skill is None:
        return []
    return _schemas_for_names(skill.tools)


def _schemas_for_names(names: Iterable[str]) -> list[dict]:
    allowed = frozenset(names)
    contracts = [t for t in ALL_TOOLS if t.llm_visible and t.name in allowed and t.name in TOOL_BY_NAME]
    contracts.sort(key=lambda t: t.name)
    return _to_openai_schema(contracts)


def resolve_skill_or_specialist(
    *,
    skill_id: str | None = None,
    specialist_type: str | None = None,
) -> tuple[SpecialistType, SkillDefinition | None]:
    skill = get_skill(skill_id or "")
    if skill is not None:
        return skill.specialist_type, skill
    raw = (specialist_type or "research").strip().lower()
    try:
        st = SpecialistType(raw)
    except ValueError:
        st = SpecialistType.RESEARCH
    defaults = {
        SpecialistType.WRITE: "project_write",
        SpecialistType.DRAFT: "draft",
        SpecialistType.PORTFOLIO: "portfolio",
        SpecialistType.SUPPORT: "support",
        SpecialistType.RESEARCH: "research",
        SpecialistType.STATUS: "status",
        SpecialistType.EMAIL: "email",
    }
    return st, get_skill(defaults.get(st, ""))


def skill_catalog_for_prompt() -> str:
    lines = []
    for s in SKILLS:
        if not s.host_invokable:
            continue
        kind = "write" if s.is_write else "read"
        lines.append(f"- `{s.skill_id}` [{kind}/{s.specialist_type.value}]: {s.description}")
    return "\n".join(lines)


def audit_tool_coverage() -> dict[str, list[str]]:
    """Classify every LLM-visible tool by its intentional runtime exposure."""
    host = get_host_tool_names()
    skill_union: set[str] = set()
    for s in SKILLS:
        skill_union |= set(s.tools)
    visible = {t.name for t in ALL_TOOLS if t.llm_visible}
    covered = host | skill_union | CURATOR_ONLY_TOOLS | SURFACE_ONLY_TOOLS
    return {
        "host": sorted(host & visible),
        "skill": sorted(skill_union & visible),
        "curator_only": sorted(CURATOR_ONLY_TOOLS & visible),
        "surface_only": sorted(SURFACE_ONLY_TOOLS & visible),
        "uncovered": sorted(visible - covered),
    }
