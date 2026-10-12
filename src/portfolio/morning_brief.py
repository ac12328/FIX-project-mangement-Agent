"""
Morning Brief service (Blueprint Section 14 / Plan Step 3.1).
Daily 09:00 HKT push with point-based urgency scoring (Red/Yellow/Green tiers).
Blueprint scoring: additive points per signal, Red >=50 (max 3), Yellow 20-49, Green <20.
"""

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.models.project import Project
from src.models.enums import BallWith, Milestone, ProjectStatus
from src.repositories.project_repo import get_active_projects_for_operator
from src.portfolio.email_attention import assess_email_attention, format_email_evidence
from src.portfolio.scoring import score_operator_flag, score_project_tier
from src.support.desk_brief import DeskBriefSnapshot, collect_desk_snapshot, format_desk_brief_section
from src.telegram.i18n import t


@dataclass
class BriefItem:
    """A single project entry in the morning brief."""
    project_id: str
    client_name: str | None
    current_milestone: str | None
    ball_with: str | None
    next_action: str | None
    urgency: str  # red | yellow | green
    urgency_score: int = 0
    urgency_reasons: list[str] = field(default_factory=list)
    days_inactive: int | None = None
    pending_drafts: int = 0
    open_followups: int = 0
    # Cached per-project email assessment (reused by _collect_action_items).
    likely_email_count: int = 0
    review_email_count: int = 0
    has_old_actionable_email: bool = False
    email_evidence: list[str] = field(default_factory=list)
    overdue_followup_count: int = 0
    overdue_internal_actions: int = 0


@dataclass
class ActionItem:
    """A single actionable item for the operator's to-do list."""
    project_id: str
    action_type: str
    count: int
    detail: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class MorningBrief:
    """The assembled morning brief."""
    generated_at: str
    total_active: int
    operator_inactive_days: int | None = None  # per Section 34.2 — warning banner
    red_items: list[BriefItem] = field(default_factory=list)
    yellow_items: list[BriefItem] = field(default_factory=list)
    green_items: list[BriefItem] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    desk: DeskBriefSnapshot = field(default_factory=DeskBriefSnapshot)


# Blueprint tier thresholds
RED_THRESHOLD = 50
YELLOW_THRESHOLD = 20
RED_MAX_ITEMS = 3

_GOLIVE_ELIGIBLE_MILESTONES = frozenset({
    Milestone.M6_TEST_REPORT_AND_PRODUCTION_PREPARATION,
    Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY,
    Milestone.M8_GO_LIVE,
})


def generate_morning_brief(conn: sqlite3.Connection, operator_id: str | None = None) -> MorningBrief:
    """
    Generate the daily morning brief. Deterministic — no LLM needed.
    Scans all active projects and scores urgency using blueprint point system.
    Includes operator inactivity warning per Section 34.2.
    """
    projects = get_active_projects_for_operator(conn, operator_id)
    now = datetime.now(timezone.utc)

    # Section 34.2: check if operator is inactive and include warning banner
    inactive_days = _get_operator_inactive_days(conn, now)

    brief = MorningBrief(
        generated_at=now.isoformat(),
        total_active=len(projects),
        operator_inactive_days=inactive_days,
    )

    items = [_score_project(conn, project, now) for project in projects]

    # Sort by score descending
    items.sort(key=lambda x: x.urgency_score, reverse=True)

    for item in items:
        if item.urgency_score >= RED_THRESHOLD and len(brief.red_items) < RED_MAX_ITEMS:
            item.urgency = "red"
            brief.red_items.append(item)
        elif item.urgency_score >= YELLOW_THRESHOLD:
            # Items that scored Red but exceeded max 3 cap → Yellow
            item.urgency = "yellow"
            brief.yellow_items.append(item)
        else:
            item.urgency = "green"
            brief.green_items.append(item)

    brief.action_items = _collect_action_items(items)
    brief.desk = collect_desk_snapshot(conn, list_queue="needs_me", limit=5)

    return brief


def _score_project(
    conn: sqlite3.Connection, project: Project, now: datetime
) -> BriefItem:
    """Score a project's urgency using blueprint additive point system."""
    score = 0
    reasons = []
    days_inactive = _days_since_activity(project, now)

    # ── Signal: Deadline proximity ────────────────────────────────
    # Skip for Waiting projects — deadline is deferred pending client action.
    if (project.target_go_live
            and project.current_milestone in _GOLIVE_ELIGIBLE_MILESTONES
            and project.project_status != ProjectStatus.WAITING):
        days_to_golive = _days_until(project.target_go_live, now)
        if days_to_golive is not None:
            if days_to_golive < 0:
                score += 30
                reasons.append(f"Go-live overdue by {abs(days_to_golive)}d (+30)")
            elif days_to_golive <= 2:
                score += 30
                reasons.append(f"Go-live in {days_to_golive}d (+30)")
            elif days_to_golive <= 7:
                score += 15
                reasons.append(f"Go-live in {days_to_golive}d (+15)")

    # ── Signal: Ball with External aging ──────────────────────────
    # Skip for Waiting projects — we are intentionally waiting on the client.
    if (project.ball_with == BallWith.EXTERNAL
            and project.project_status != ProjectStatus.WAITING
            and days_inactive is not None):
        if days_inactive >= 7:
            score += 25
            reasons.append(f"External {days_inactive}d no response (+25)")
        elif days_inactive >= 3:
            score += 10
            reasons.append(f"External {days_inactive}d no response (+10)")

    # ── Signal: Overdue follow-ups ────────────────────────────────
    overdue_followups = _count_overdue_followups(conn, project.project_id, now)
    if overdue_followups > 0:
        score += 20
        reasons.append(f"{overdue_followups} overdue follow-up(s) (+20)")

    # ── Signal: next_action stale (proxy for "next_action overdue 3d+") ───
    if days_inactive is not None and days_inactive >= 3 and project.next_action:
        score += 20
        reasons.append(f"next_action stale {days_inactive}d (+20)")

    # ── Signal: External email attention ─────────────────────────
    email_attention = assess_email_attention(conn, project.project_id, now)
    likely_email_count = len(email_attention.likely)
    review_email_count = len(email_attention.review)
    if likely_email_count > 0:
        score += 30
        reasons.append(f"{likely_email_count} likely email action(s) (+30)")
        if email_attention.has_old_likely:
            score += 15
            reasons.append("Likely email action >24h (+15)")
    elif review_email_count > 0:
        score += 5
        reasons.append(f"{review_email_count} external email thread(s) to review (+5)")

    # ── Signal: Operator flag ─────────────────────────────────────
    flag_value = _get_operator_flag_value(conn, project.project_id)
    flag_pts = score_operator_flag(flag_value)
    if flag_pts > 0:
        score += flag_pts
        reasons.append(f"Operator flagged {flag_value} (+{flag_pts})")

    # ── Signal: Project tier ──────────────────────────────────────
    tier_pts = score_project_tier(project.project_tier)
    if tier_pts > 0:
        score += tier_pts
        reasons.append(f"{(project.project_tier or '').capitalize()}-tier project (+{tier_pts})")

    # ── Signal: SLA limit (per-milestone) ────────────────────────
    if project.sla_limit and project.sla_limit > 0:
        sla_remaining = _sla_limit_remaining(conn, project, now)
        if sla_remaining is not None:
            if sla_remaining <= 0:
                score += 25
                reasons.append(f"SLA limit breached ({abs(sla_remaining)} biz days over) (+25)")
            elif sla_remaining <= 2:
                score += 10
                reasons.append(f"SLA limit near ({sla_remaining} biz days left) (+10)")

    # ── Signal: Milestone dwell time (F15) ───────────────────────
    dwell = _get_milestone_dwell_days(conn, project.project_id, now)
    if dwell is not None:
        if dwell >= 14:
            score += 15
            reasons.append(f"里程碑停滯 {dwell}d (+15)")
        elif dwell >= 7:
            score += 5
            reasons.append(f"里程碑停滯 {dwell}d (+5)")

    # ── Signal: Overdue internal actions ─────────────────────────
    overdue_internal = _count_overdue_internal_actions(conn, project.project_id, now)
    if overdue_internal > 0:
        score += 15
        reasons.append(f"{overdue_internal} overdue internal action(s) (+15)")

    # ── Signal: Pending operator approvals (F15) ─────────────────
    pending_approvals = _count_pending_approvals(conn, project.project_id)
    if pending_approvals > 0:
        score += 10
        reasons.append(f"{pending_approvals} pending approval(s) (+10)")

    # ── Informational: pending drafts and open follow-ups ─────────
    pending_drafts = _count_pending_drafts(conn, project.project_id)
    open_followups = _count_open_followups(conn, project.project_id)

    if not reasons:
        reasons.append("On track")

    return BriefItem(
        project_id=project.project_id,
        client_name=project.client_name,
        current_milestone=project.current_milestone.value if project.current_milestone else None,
        ball_with=project.ball_with.value if project.ball_with else None,
        next_action=project.next_action,
        urgency="green",  # will be set by generate_morning_brief
        urgency_score=score,
        urgency_reasons=reasons,
        days_inactive=days_inactive,
        pending_drafts=pending_drafts,
        open_followups=open_followups,
        likely_email_count=likely_email_count,
        review_email_count=review_email_count,
        has_old_actionable_email=email_attention.has_old_likely,
        email_evidence=format_email_evidence(email_attention),
        overdue_followup_count=overdue_followups,
        overdue_internal_actions=overdue_internal,
    )


def _collect_action_items(
    items: list[BriefItem],
) -> list[ActionItem]:
    """Aggregate actionable items across all active projects.
    Reads from pre-computed BriefItem fields — zero additional DB queries.
    """
    action_items: list[ActionItem] = []
    for brief_item in items:
        pid = brief_item.project_id

        # External email attention — already computed in _score_project.
        if brief_item.likely_email_count > 0:
            age = " · >24h" if brief_item.has_old_actionable_email else ""
            action_items.append(ActionItem(
                pid,
                "email_action_likely",
                brief_item.likely_email_count,
                f"{brief_item.likely_email_count} likely action{age}",
                brief_item.email_evidence,
            ))
        elif brief_item.review_email_count > 0:
            action_items.append(ActionItem(
                pid,
                "email_review",
                brief_item.review_email_count,
                f"{brief_item.review_email_count} recent inbound(s) to glance",
                brief_item.email_evidence,
            ))

        # Pending drafts — already computed in _score_project
        if brief_item.pending_drafts > 0:
            action_items.append(ActionItem(pid, "pending_draft", brief_item.pending_drafts, f"{brief_item.pending_drafts}份待審"))

        # Overdue followups — already computed in _score_project
        if brief_item.overdue_followup_count > 0:
            action_items.append(ActionItem(pid, "overdue_followup", brief_item.overdue_followup_count, f"{brief_item.overdue_followup_count}個逾期"))

        # Overdue internal actions — already computed in _score_project
        if brief_item.overdue_internal_actions > 0:
            action_items.append(ActionItem(pid, "overdue_internal_action", brief_item.overdue_internal_actions, f"{brief_item.overdue_internal_actions}個內部任務逾期"))

    action_items.sort(key=lambda a: (a.action_type, a.project_id))
    return action_items


def format_brief_text(brief: MorningBrief) -> str:
    """Format morning brief as text (for Telegram or console)."""
    lines = []

    # Section 34.2 warning banner — operator inactive
    if brief.operator_inactive_days is not None:
        lines.append(
            t("brief.operator_inactive").format(days=brief.operator_inactive_days)
        )
        lines.append("")

    # ── Section 1: Action Items ──
    lines.append(t("brief.action_header"))
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
    if brief.action_items:
        _ACTION_LABELS = {
            "email_action_likely": t("brief.action_email_likely"),
            "email_review": t("brief.action_email_review"),
            "pending_draft": t("brief.action_draft"),
            "overdue_followup": t("brief.action_followup"),
            "overdue_internal_action": t("brief.action_internal"),
        }
        by_type: dict[str, list[ActionItem]] = {}
        for ai in brief.action_items:
            label = _ACTION_LABELS.get(ai.action_type, ai.action_type)
            by_type.setdefault(label, []).append(ai)
        for label, entries in by_type.items():
            lines.append(f"{label}:")
            for ai in entries:
                lines.append(f"- {ai.project_id} — {ai.detail}")
                lines.extend(f"  • {evidence}" for evidence in ai.evidence)
    else:
        lines.append(t("brief.no_actions"))
    lines.append("")

    lines.extend(format_desk_brief_section(
        brief.desk,
        header=t("brief.desk_header"),
        counts=t("brief.desk_counts"),
        hint=t("brief.desk_stale_hint"),
        empty=t("brief.desk_empty"),
    ))
    lines.append("")

    # ── Section 2: Portfolio ──
    lines.append(t("brief.portfolio_header").format(total=brief.total_active))
    lines.append("")

    if brief.red_items:
        lines.append(t("brief.needs_attention").format(count=len(brief.red_items)))
        for i, item in enumerate(brief.red_items):
            prefix = "├" if i < len(brief.red_items) - 1 else "└"
            milestone = f" · {item.current_milestone}" if item.current_milestone else ""
            top_reason = item.urgency_reasons[0] if item.urgency_reasons else "urgent"
            clean_reason = re.sub(r"\s*\(\+\d+\)$", "", top_reason)
            lines.append(f"{prefix} **{item.project_id}**{milestone}")
            lines.append(f"│  {clean_reason}")
            if item.next_action:
                lines.append(f"│  → {item.next_action[:60]}")
        lines.append("")

    if brief.yellow_items:
        lines.append(t("brief.monitor").format(count=len(brief.yellow_items)))
        for i, item in enumerate(brief.yellow_items):
            prefix = "├" if i < len(brief.yellow_items) - 1 else "└"
            milestone = item.current_milestone or "—"
            ball = item.ball_with or "—"
            stale = f"{item.days_inactive}d" if item.days_inactive is not None else "—"
            lines.append(f"{prefix} {item.project_id} · {milestone} · Ball:{ball} · {stale}")
        lines.append("")

    if brief.green_items:
        lines.append(t("brief.on_track").format(count=len(brief.green_items)))
        for i, item in enumerate(brief.green_items):
            prefix = "├" if i < len(brief.green_items) - 1 else "└"
            milestone = item.current_milestone or "—"
            ball = item.ball_with or "—"
            lines.append(f"{prefix} {item.project_id} · {milestone} · Ball:{ball}")
        lines.append("")

    lines.append("/portfolio  /brief")

    return "\n".join(lines)


def _days_since_activity(project: Project, now: datetime) -> int | None:
    if not project.last_activity_at:
        return None
    last = project.last_activity_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).days


def _sla_limit_remaining(
    conn: sqlite3.Connection, project: Project, now: datetime,
) -> int | None:
    """Compute remaining business days of SLA limit at current milestone."""
    if not project.sla_limit or not project.current_milestone:
        return None
    row = conn.execute(
        """SELECT changed_at FROM milestone_history
           WHERE project_id = ? ORDER BY changed_at DESC LIMIT 1""",
        (project.project_id,),
    ).fetchone()
    if not row:
        return None
    try:
        from src.utils.calendar import business_days_between

        changed = datetime.fromisoformat(row["changed_at"])
        elapsed = business_days_between(changed.date(), now.date())
        return int(project.sla_limit) - elapsed
    except Exception:
        return None


def _get_milestone_dwell_days(
    conn: sqlite3.Connection, project_id: str, now: datetime,
) -> int | None:
    """Days since last milestone change for a project."""
    row = conn.execute(
        """SELECT changed_at FROM milestone_history
           WHERE project_id = ? ORDER BY changed_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    if not row:
        return None
    try:
        changed = datetime.fromisoformat(row["changed_at"])
        if changed.tzinfo is None:
            changed = changed.replace(tzinfo=timezone.utc)
        return (now - changed).days
    except (ValueError, TypeError):
        return None


def _count_pending_approvals(conn: sqlite3.Connection, project_id: str) -> int:
    """Count proposed_updates awaiting operator decision."""
    row = conn.execute(
        "SELECT COUNT(*) as c FROM proposed_updates WHERE project_id = ? AND decision_outcome IS NULL",
        (project_id,),
    ).fetchone()
    return row["c"] if row else 0


def _get_operator_inactive_days(conn: sqlite3.Connection, now: datetime) -> int | None:
    """Return business days since last Telegram interaction, or None if active."""
    from src.portfolio.inactivity_alert import (
        INACTIVITY_THRESHOLD_DAYS,
        _business_days_between,
    )
    from src.repositories.operator_state_repo import get_operator_state

    state = get_operator_state(conn)
    last_action_at = state.get("last_telegram_interaction_at") if state else None
    if not last_action_at:
        return None
    try:
        last_dt = datetime.fromisoformat(last_action_at)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        days = _business_days_between(last_dt, now)
        return days if days >= INACTIVITY_THRESHOLD_DAYS else None
    except (ValueError, TypeError):
        return None


def _days_until(date_str: str, now: datetime) -> int | None:
    try:
        target = datetime.fromisoformat(date_str)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return (target - now).days
    except (ValueError, TypeError):
        return None


def _count_overdue_internal_actions(conn: sqlite3.Connection, project_id: str, now: datetime) -> int:
    from src.repositories.internal_action_repo import count_overdue_internal_actions
    return count_overdue_internal_actions(conn, project_id, now.strftime("%Y-%m-%d"))


def _count_open_followups(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM internal_followups WHERE project_id = ? AND status = 'open'",
        (project_id,),
    ).fetchone()
    return row[0] if row else 0


def _count_overdue_followups(conn: sqlite3.Connection, project_id: str, now: datetime) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM internal_followups WHERE project_id = ? AND status = 'open' AND due_date < ?",
        (project_id, now.isoformat()),
    ).fetchone()
    return row[0] if row else 0


def _count_pending_drafts(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM drafts WHERE project_id = ? AND status IN ('created', 'in_review')",
        (project_id,),
    ).fetchone()
    return row[0] if row else 0


def _get_operator_flag_value(conn: sqlite3.Connection, project_id: str) -> str | None:
    """Return the raw operator flag string ('urgent'/'watch'/None)."""
    row = conn.execute(
        "SELECT memory_value FROM working_memory WHERE project_id = ? AND memory_key = 'operator_flag'",
        (project_id,),
    ).fetchone()
    if not row:
        return None
    return (row[0] or "").strip().lower() or None


def _get_operator_flag_score(conn: sqlite3.Connection, project_id: str) -> int:
    """Return score points from operator flag. Delegates to shared scoring module."""
    return score_operator_flag(_get_operator_flag_value(conn, project_id))


def format_brief_with_llm(brief: MorningBrief) -> str:
    """Format morning brief using LLM in the operator's preferred language.

    Falls back to the deterministic English template (format_brief_text) on any
    failure so the operator always receives a message.
    """
    from src.llm.client import llm_call
    from src.utils.operator_language import (
        operator_language_instruction,
        prose_label,
        read_response_language,
    )

    lang = read_response_language()
    context = _build_brief_context(brief)
    try:
        prompt_text = _load_brief_prompt()
        lang_rule = (
            f"\n\n## {operator_language_instruction(lang)}\n"
            f"OUTPUT LANGUAGE: Write the entire brief in {prose_label(lang)}. "
            "This overrides any hardcoded language in the template above."
        )
        messages = [
            {"role": "system", "content": prompt_text + lang_rule},
            {"role": "user", "content": f"Brief data:\n{context}"},
        ]
        result = llm_call("morning_brief", messages)
        if result and result.strip():
            from src.llm.client import strip_reasoning
            result = _clean_brief_output(strip_reasoning(result))
            if result:
                return result
    except Exception:
        import logging
        logging.getLogger(__name__).warning("LLM morning brief formatting failed; using fallback")
    return format_brief_text(brief)


def _load_brief_prompt() -> str:
    """Load the LLM system prompt from prompts/morning_brief.md (lazy, cached)."""
    import os
    prompt_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "prompts", "morning_brief.md"
    )
    try:
        with open(os.path.normpath(prompt_path), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return _FALLBACK_PROMPT


def _clean_brief_output(text: str) -> str:
    """Apply morning-brief-specific cleanup after think-tag stripping."""
    # Accept either 📌 (action items header) or 📋 (portfolio header) as brief start
    pin_start = text.find("\U0001f4cc")  # 📌
    clip_start = text.find("\U0001f4cb")  # 📋
    starts = [s for s in (pin_start, clip_start) if s >= 0]
    if starts:
        brief_start = min(starts)
        if brief_start > 0:
            text = text[brief_start:]
    elif pin_start < 0 and clip_start < 0:
        return ""

    text = re.sub(
        r"^(?:Thinking\s+Process|Analysis|Step\s+\d|Let me|Wait|Ready to)[^\n]*\n",
        "",
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    return text.strip()


_FALLBACK_PROMPT = """\
You are AgentFIXBPRO, a FIX protocol onboarding PM copilot.
Format the morning brief data below in the operator's preferred response language
(see OPERATOR LANGUAGE POLICY if injected; otherwise 繁體中文).
Keep it concise: one line per project. Highlight red items.
Max 20 lines.
"""


def _build_brief_context(brief: MorningBrief) -> str:
    """Build a structured text summary of the brief for the LLM."""
    lines = [f"生成時間: {brief.generated_at}", f"活躍項目總數: {brief.total_active}", ""]

    # ── Section 1: Action Items ──
    lines.append("── 今日待處理 ──")
    if brief.action_items:
        by_type: dict[str, list[str]] = {}
        for ai in brief.action_items:
            label = _ACTION_TYPE_LABELS.get(ai.action_type, ai.action_type)
            detail = f"{ai.project_id}({ai.detail})"
            if ai.evidence:
                detail += " [" + " | ".join(ai.evidence) + "]"
            by_type.setdefault(label, []).append(detail)
        for label, entries in by_type.items():
            lines.append(f"{label}: {', '.join(entries)}")
    else:
        lines.append("無")
    lines.append("")

    lines.append("── Desk issues ──")
    lines.append(
        f"Needs us {brief.desk.needs_count} · Waiting {brief.desk.waiting_count} · Stale {brief.desk.stale_count}"
    )
    if brief.desk.needs:
        for item in brief.desk.needs:
            who = item.party_name or "Unidentified"
            lines.append(
                f"  {item.ticket_id} [{who}] {item.case_type} → {item.next_label}: {item.display_title[:70]}"
            )
    elif brief.desk.all_active == 0:
        lines.append("無未完結工單")
    else:
        lines.append("今日無需我哋即時處理的工單")
    lines.append("")

    # ── Section 2: Portfolio by tier ──
    for tier_label, items in [
        (f"紅色（緊急）{len(brief.red_items)}個", brief.red_items),
        (f"黃色（監察）{len(brief.yellow_items)}個", brief.yellow_items),
        (f"綠色（正常）{len(brief.green_items)}個", brief.green_items),
    ]:
        if items:
            lines.append(f"{tier_label}:")
            for item in items:
                lines.append(f"  {_format_item_line(item)}")
    return "\n".join(lines)


_ACTION_TYPE_LABELS = {
    "email_action_likely": "較可能需要處理的外部郵件",
    "email_review": "近日未評分客信",
    "pending_draft": "待審批草稿",
    "overdue_followup": "逾期跟進",
    "overdue_internal_action": "逾期內部任務",
}


def _format_item_line(item: BriefItem) -> str:
    """Format a single project line for the LLM context."""
    name = item.client_name or item.project_id
    milestone = item.current_milestone or "—"
    ball = f"Ball:{item.ball_with}" if item.ball_with else "Ball:—"
    stale = f"活動停滯:{item.days_inactive}d" if item.days_inactive is not None else "活動停滯:—"
    reasons = "; ".join(
        re.sub(r"\s*\(\+\d+\)$", "", r) for r in item.urgency_reasons
        if r != "On track"
    ) or "正常"
    parts = [f"{item.project_id} [{name}]", milestone, ball, stale, f"原因:{reasons}"]
    if item.next_action:
        parts.append(f"next_action:{item.next_action[:60]}")
    return " | ".join(parts)
