"""
Progress advisory harness — proactive PM guidance from project_advisor.md + wiki only.

Triggers on single-project status (A) and portfolio follow-up (B).
Single recommendation + ask operator; no auto draft; M8 → sign-off when ready.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from src.agent.active_project import get_active_project_id
from src.agent.intent import detect_project_id, is_portfolio_query
from src.advisory.milestone_wiki import get_milestone_wiki_excerpt, search_milestone_context
from src.models.enums import Milestone, ProjectStatus
from src.repositories.project_repo import get_project
from src.utils.calendar import business_days_between
from src.utils.operator_language import prose_label

_SESSION_ONLINE_RE = re.compile(
    r"production session|session.*online|daily (?:sequence )?reset|"
    r"\bPCT\b.*complet|connectivity test.*complet|session will be effective|"
    r"production connectivity.*complet|FIX session.*effective|"
    r"connectivity test is completed",
    re.IGNORECASE,
)

_STATUS_QUERY_RE = re.compile(
    r"(what\s+about|status|狀態|進度|how\s+is|點樣|情況|應該點|點做|我應該"
    r"|should\s+i\s+(?:advance|move|push|proceed)"
    r"|can\s+i\s+(?:advance|move|push|proceed)"
    r"|advance\s+to\s+M[1-8]|move\s+to\s+M[1-8]|push\s+to\s+M[1-8]"
    r"|(?:advance|move|push)\s+(?:the\s+)?(?:milestone\s+)?to\s+M[1-8])",
    re.IGNORECASE,
)

_MILESTONE_ADVANCE_QUERY_RE = re.compile(
    r"(推進|推到|推.*到\s*m[1-8]\b|到\s*m[1-8]\b|advance\s+to\s+m[1-8]"
    r"|move\s+to\s+m[1-8]|push\s+to\s+m[1-8]|里程碑.*(?:到|推))",
    re.IGNORECASE,
)

_M8_HANDOFF_QUERY_RE = re.compile(
    r"(production\s+session|session.*online|PCT\s*完成|已\s*online).*(launch|上線|go-?live)"
    r"|(launch|上線|go-?live).*(下個月|delay|延遲|自己)",
    re.IGNORECASE,
)

_SIGN_OFF_INTENT_RE = re.compile(
    r"("
    r"sign[\s-]?off|signed\s+off"
    r"|confirm(?:ed|ing)?\s+(?:on\s+)?production"
    r"|on\s+production|已上線|確認上\s*production|確認已上線"
    r"|(?:is\s+)?completed(?:\s+and\s+(?:can\s+be\s+)?clos(?:e|ed|ing))?"
    r"|can\s+be\s+clos(?:e|ed|ing)"
    r"|clos(?:e|ed|ing)\s+(?:the\s+)?project"
    r"|結案|關閉項目|可以關閉|項目.*(?:完成|結束)"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProgressContext:
    project_id: str
    milestone: str
    ball_with: str | None
    days_inactive: int | None
    wiki_excerpt: str
    m8_signoff_ready: bool


def _resolve_project_id(
    conn: sqlite3.Connection,
    user_text: str,
    *,
    effective_project_id: str | None = None,
    chat_id: int | None = None,
) -> str | None:
    if effective_project_id:
        return effective_project_id
    explicit = detect_project_id(user_text)
    if explicit:
        return explicit
    if chat_id is not None:
        return get_active_project_id(conn, chat_id)
    return None


def _days_inactive(project) -> int | None:
    if not project.updated_at:
        return None
    updated = project.updated_at
    if isinstance(updated, str):
        updated = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return business_days_between(updated, datetime.now(timezone.utc))


def is_m8_signoff_ready(project) -> bool:
    """Production session online + PCT signals — PM handling complete (R1 B)."""
    if project.current_milestone.value not in (
        Milestone.M8_GO_LIVE.value,
        Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY.value,
    ):
        return False
    if project.project_status in ProjectStatus.terminal_statuses():
        return False
    combined = f"{project.notes or ''} {project.next_action or ''}"
    return bool(_SESSION_ONLINE_RE.search(combined))


def _is_portfolio_followup_query(user_text: str) -> bool:
    return is_portfolio_query(user_text) and bool(
        re.search(r"跟進|brief|attention|今日", user_text, re.IGNORECASE)
    )


def should_inject_progress_advisory(user_text: str) -> bool:
    if not user_text or not user_text.strip():
        return False
    if _SIGN_OFF_INTENT_RE.search(user_text):
        return False
    if _is_portfolio_followup_query(user_text):
        return True
    if _M8_HANDOFF_QUERY_RE.search(user_text):
        return True
    if _MILESTONE_ADVANCE_QUERY_RE.search(user_text):
        return True
    return bool(_STATUS_QUERY_RE.search(user_text))


def progress_required_tools(conn: sqlite3.Connection | None = None) -> frozenset[str]:
    """Tools that must run before final reply on progress-advisory turns."""
    from src.agent.harness_settings import require_wiki_on_progress

    if not require_wiki_on_progress(conn):
        return frozenset()
    return frozenset({"wiki_search"})


def build_progress_context(conn: sqlite3.Connection, project_id: str) -> ProgressContext | None:
    project = get_project(conn, project_id)
    if not project or project.project_status in ProjectStatus.terminal_statuses():
        return None
    milestone = project.current_milestone.value
    wiki = search_milestone_context(milestone, project.market)
    if not wiki:
        wiki = get_milestone_wiki_excerpt(milestone)
    return ProgressContext(
        project_id=project_id,
        milestone=milestone,
        ball_with=project.ball_with.value if project.ball_with else None,
        days_inactive=_days_inactive(project),
        wiki_excerpt=wiki,
        m8_signoff_ready=is_m8_signoff_ready(project),
    )


def build_m8_signoff_guard(ctx: ProgressContext) -> dict:
    return {
        "role": "system",
        "content": (
            f"<m8-signoff-ready-guard>"
            f"Project {ctx.project_id} at {ctx.milestone}: production session online / "
            f"PCT complete per wiki playbook "
            f"playbook/b2b/fix-production-session-launch-handoff.md. "
            "PM onboarding handling is COMPLETE (R1). "
            "Per project_advisor.md + wiki — do NOT chase launch dates; rule 58 does not apply.\n"
            f"Reply ({prose_label()}): summarise status; recommend operator **sign-off → Completed** "
            "ONLY if no open production blockers remain (e.g. Tag 129 missing, client waiting for "
            "prod logs, FTP not open, B2B launch not coordinated). If blockers exist: recommend "
            "Active/Blocked + specific next_action, NOT Completed.\n"
            "ask: offer to record sign-off and mark project Completed (operator language).\n"
            "If operator confirms and no blockers: add_note → update_project_batch "
            "(project_status=Completed, ball_with=Internal, clear next_action) → confirm_update.\n"
            "FORBIDDEN: draft_reply unless operator explicitly requests an email."
            f"</m8-signoff-ready-guard>"
        ),
    }


def build_single_project_guard(ctx: ProgressContext) -> dict:
    stale_hint = ""
    if ctx.ball_with == "External" and ctx.days_inactive is not None and ctx.days_inactive > 3:
        stale_hint = (
            f"External + {ctx.days_inactive} business days inactive — "
            "project_advisor rule 58: top recommendation MAY be follow-up email. "
        )

    from src.advisory.milestone_wiki import get_milestone_completion_criteria

    criteria = get_milestone_completion_criteria(ctx.milestone)
    advance_hint = ""
    if criteria:
        next_contract_hint = ""
        if ctx.milestone == Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY.value:
            next_contract_hint = (
                " If all criteria are met, the candidate is M8_GoLive; ready to go live "
                "does not mean Completed."
            )
        advance_hint = (
            "Milestone advance gate (versioned operational contract): report a "
            "criterion-by-criterion result (met/missing/contradicted) before any candidate. "
            f"Criteria for {ctx.milestone}: {'; '.join(criteria)}. "
            "Recommend the single most important missing criterion, not generic evidence. "
            "Only stage a milestone candidate when deterministic evaluation permits; "
            "wiki retrieval may supplement context but cannot override the contract."
            f"{next_contract_hint}\n"
        )

    return {
        "role": "system",
        "content": (
            f"<progress-advisory-guard>"
            f"Project {ctx.project_id} | {ctx.milestone} | ball={ctx.ball_with or 'unknown'}. "
            f"{stale_hint}{advance_hint}"
            "Contract/evaluator source is authoritative. Wiki context is supplemental and never a gate.\n"
            "PM problem hub: playbook/fix-connectivity/pm-problem-index.md\n"
            "Before final reply: call wiki_search (milestone topic) and get_advisory(project_id) "
            "if not already done this turn.\n"
            f"Wiki context:\n{ctx.wiki_excerpt[:900]}\n"
            f"Response format ({prose_label()}):\n"
            "1. Status summary from project context\n"
            "2. ONE top recommendation from advisory (not a list of options)\n"
            "3. End with a short offer to perform the concrete next action for the operator\n"
            "FORBIDDEN: auto draft_reply; A/B option menus; generic chase without advisory; "
            "inventing policies not in wiki or project_advisor.md."
            f"</progress-advisory-guard>"
        ),
    }


def build_portfolio_guard() -> dict:
    return {
        "role": "system",
        "content": (
            "<portfolio-progress-advisory-guard>"
            "Portfolio / 今日跟進 query. After query_projects:\n"
            "For each red/yellow project (max 5), use project_advisor.md + wiki_search per "
            "milestone to give ONE line: project_id + top recommendation.\n"
            "Do not invent projects. End with offer to act on one specific project.\n"
            "FORBIDDEN: auto draft_reply for all projects."
            "</portfolio-progress-advisory-guard>"
        ),
    }


def collect_progress_guards(
    conn: sqlite3.Connection | None,
    user_text: str,
    *,
    effective_project_id: str | None = None,
    chat_id: int | None = None,
) -> list[dict]:
    if conn is None or not should_inject_progress_advisory(user_text):
        return []

    if _is_portfolio_followup_query(user_text):
        return [build_portfolio_guard()]

    project_id = _resolve_project_id(
        conn, user_text,
        effective_project_id=effective_project_id,
        chat_id=chat_id,
    )
    if not project_id:
        return []

    ctx = build_progress_context(conn, project_id)
    if ctx is None:
        return []

    if ctx.m8_signoff_ready:
        return [build_m8_signoff_guard(ctx)]
    return [build_single_project_guard(ctx)]


def progress_blocked_tools(
    conn: sqlite3.Connection | None,
    user_text: str,
    *,
    effective_project_id: str | None = None,
    chat_id: int | None = None,
) -> frozenset[str]:
    """Block auto-draft on M8 sign-off ready unless operator explicitly asks."""
    if conn is None or not is_m8_signoff_ready_path(conn, user_text, effective_project_id, chat_id):
        return frozenset()
    if re.search(r"draft|草擬|email|郵件", user_text, re.IGNORECASE):
        return frozenset()
    return frozenset({
        "draft_reply",
        "create_outreach_email_draft",
        "send_outreach_email",
    })


def is_m8_signoff_ready_path(
    conn: sqlite3.Connection,
    user_text: str,
    effective_project_id: str | None,
    chat_id: int | None,
) -> bool:
    pid = _resolve_project_id(
        conn, user_text,
        effective_project_id=effective_project_id,
        chat_id=chat_id,
    )
    if not pid:
        return False
    project = get_project(conn, pid)
    return project is not None and is_m8_signoff_ready(project)