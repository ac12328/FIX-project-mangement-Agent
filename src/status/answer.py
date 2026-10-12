"""
Status Answer service (Blueprint Section 22).
Handles /status command — read-only, never writes project truth.
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

from src.context.layer1_truth import load_project_truth
from src.context.layer2_working_memory import load_working_memory
from src.context.layer4_evidence import load_evidence
from src.models.status_answer import StatusAnswer
from src.skills.fix_onboarding import FIXOnboardingSkill
from src.telegram.i18n import t
from src.utils.prompt_loader import load_prompt


def answer_status(
    conn: sqlite3.Connection,
    project_id: str,
    question: str | None = None,
    *,
    llm_fn=None,
) -> StatusAnswer | None:
    """
    Generate a status answer for a project.
    READ ONLY — never modifies project truth.

    Returns None if the project is not found.
    """
    p, truth_missing = load_project_truth(conn, project_id)
    if p is None:
        return None

    wm, wm_missing = load_working_memory(conn, project_id)
    evidence_list, ev_missing = load_evidence(conn, project_id)

    # Confidence scoring (matches former builder.py logic)
    all_missing = truth_missing + wm_missing + ev_missing
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

    days_since = _days_since_last_activity(conn, project_id)
    skill = FIXOnboardingSkill()
    brief = skill.assess(
        current_milestone=p.current_milestone,
        ball_with=p.ball_with,
        days_since_last_activity=days_since,
    )

    active_blockers = [obs for obs in (brief.observations or []) if "block" in obs.lower()]
    escalate = bool(active_blockers or wm.stale_keys)
    escalation_reason: str | None = None
    if active_blockers:
        escalation_reason = active_blockers[0]
    elif wm.stale_keys:
        escalation_reason = f"Stale memory keys: {', '.join(wm.stale_keys)}"

    recommended_action = brief.recommendations[0] if brief.recommendations else None

    llm_answer: str | None = None
    if llm_fn is not None and question:
        try:
            blockers_text = "; ".join(active_blockers) if active_blockers else "None"
            prompt = [
                {"role": "system", "content": load_prompt("formatting/status_answer.md")},
                {"role": "user", "content": (
                    f"Project: {p.project_id} ({p.client_name})\n"
                    f"Status: {p.project_status}\n"
                    f"Stage: {p.current_milestone or 'Not set'}\n"
                    f"Ball with: {p.ball_with}\n"
                    f"Days since last change: {days_since if days_since is not None else 'Unknown'}\n"
                    f"Next action: {p.next_action or 'Not set'}\n"
                    f"Blockers: {blockers_text}\n"
                    f"Recommended: {recommended_action or 'None'}\n"
                    f"Escalate: {'Yes — ' + escalation_reason if escalation_reason else 'No'}\n"
                    f"SLA limit (days): {p.sla_limit or 'Not set'}\n\n"
                    f"Question: {question}"
                )},
            ]
            llm_answer = llm_fn("status", prompt)
        except Exception:
            logger.warning("LLM status answer failed for %s", project_id, exc_info=True)
            llm_answer = None

    return StatusAnswer(
        project_id=p.project_id,
        client_name=p.client_name,
        project_status=str(p.project_status.value) if p.project_status else None,
        current_milestone=str(p.current_milestone.value) if p.current_milestone else None,
        ball_with=str(p.ball_with.value) if p.ball_with else None,
        next_action=p.next_action,
        market=p.market,
        days_since_last_material_change=days_since,
        active_blockers=active_blockers,
        recommended_action=recommended_action,
        escalate_to_operator=escalate,
        escalation_reason=escalation_reason,
        confidence=confidence,
        context_confidence=confidence,
        pending_questions=wm.pending_questions or [],
        operator_instruction=wm.latest_operator_instruction,
        stale_memory_keys=wm.stale_keys or [],
        skill_observations=brief.observations or [],
        skill_recommendations=brief.recommendations or [],
        recent_evidence_count=len(evidence_list),
        llm_answer=llm_answer,
    )


def answer_status_all(conn: sqlite3.Connection) -> list[dict]:
    """Get brief status for all active projects."""
    from src.repositories.project_repo import get_active_projects
    projects = get_active_projects(conn)

    summaries = []
    for p in projects:
        summaries.append({
            "project_id": p.project_id,
            "client_name": p.client_name,
            "current_milestone": str(p.current_milestone.value) if p.current_milestone else None,
            "ball_with": str(p.ball_with.value) if p.ball_with else None,
            "next_action": p.next_action,
        })
    return summaries


def _ball_plain(ball_with: str | None) -> str:
    """Operator-facing ball label (ZH-friendly short gloss)."""
    raw = (ball_with or "").strip()
    mapping = {
        "Internal": "Internal（我方待處理）",
        "External": "External（對方待回）",
        "Shared": "Shared（雙方共同推進）",
        "Unknown": "Unknown（未判定）",
    }
    return mapping.get(raw, raw or "-")


def _status_proactive_offer(answer: StatusAnswer) -> str | None:
    """One concrete offer after status — improves TG chat actionability (P2)."""
    next_a = (answer.next_action or "").lower()
    ball = (answer.ball_with or "").strip()
    ms = (answer.current_milestone or "").strip()
    status = (answer.project_status or "").strip()
    # M8 go-live: ask to close (never auto-close without operator confirm)
    if ms == "M8_GoLive" and status not in {"Completed", "Archived", "Cancelled"}:
        return (
            f"**{answer.project_id}** 已在 **M8_GoLive**。"
            "需要我幫你 **關閉 project（Completed）** 嗎？請回覆確認關閉。"
        )
    if any(k in next_a for k in ("close project", "關閉", "confirm close")):
        return "需要我現在 **stage 關閉 project（Completed）** 給你確認嗎？"
    if any(k in next_a for k in ("draft", "email", "send", "mail", "回覆", "寄")):
        return "需要我幫你 **draft 這封 email** 嗎？"
    if ball == "External":
        return "需要我幫你準備一封 **follow-up** 催對方嗎？"
    if ball == "Internal" and (answer.next_action or "").strip():
        return "需要我把 next action 開成 **internal follow-up**，或更新 project 欄位嗎？"
    if not (answer.next_action or "").strip():
        return "next_action 目前空白 — 需要我根據最近進度建議一步嗎？"
    return "需要我深入查看 email / 開 follow-up 嗎？"


def format_status_telegram(answer: StatusAnswer) -> str:
    """Format a StatusAnswer for Telegram (table + next action + one offer)."""
    days = answer.days_since_last_material_change
    if days is None:
        sla_icon = ""
    elif days >= 5:
        sla_icon = "🔴"
    elif days >= 3:
        sla_icon = "🟡"
    else:
        sla_icon = "🟢"

    pid = answer.project_id
    client = answer.client_name or "-"
    lines = [
        f"以下是 **{pid} — {client}** 的最新狀態：",
        "",
        "| 欄位 | 內容 |",
        "|------|------|",
        f"| **Client** | {client} |",
        f"| **Status** | {answer.project_status or '-'} |",
        f"| **Ball** | {_ball_plain(answer.ball_with)} |",
        f"| **Milestone** | {answer.current_milestone or '-'} |",
    ]
    if answer.market:
        lines.append(f"| **Market** | {answer.market} |")
    if days is not None:
        lines.append(f"| **Days since change** | {days} {sla_icon} |")
    lines.append("")
    na = (answer.next_action or "").strip() or "（未設定）"
    lines.append(f"**Next Action (DB)：** {na}")
    if answer.active_blockers:
        lines.append(
            t("status.blockers").format(blockers="; ".join(answer.active_blockers[:2]))
        )
    rec = (answer.recommended_action or "").strip()
    if rec and rec != na:
        lines.append(t("status.recommended").format(action=rec))
    if answer.escalate_to_operator and answer.escalation_reason:
        lines.append(t("status.escalation").format(reason=answer.escalation_reason))
    if answer.pending_questions:
        lines.append(t("status.pending_q").format(question=answer.pending_questions[0]))
    offer = _status_proactive_offer(answer)
    if offer:
        lines.append("")
        lines.append(offer)
    # Soft cap ~24 lines — table + session/progress hints
    return "\n".join(lines[:24])


def _days_since_last_activity(conn: sqlite3.Connection, project_id: str) -> int | None:
    """Calculate days since last activity for a project."""
    from datetime import datetime, timezone

    row = conn.execute(
        "SELECT last_activity_at FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()

    if row is None:
        return None

    last = dict(row).get("last_activity_at")
    if not last:
        return None

    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - last_dt
        return delta.days
    except (ValueError, TypeError):
        return None
