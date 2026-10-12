"""Shared, source-grounded content for analysis-only email notifications.

Presentation only: never authorizes project writes or creates follow-up tasks.
"""
from __future__ import annotations

import re
from pydantic import BaseModel, Field
from src.email.body_cleaner import clean_email_body


class EmailAnalysisNotice(BaseModel):
    highlights: list[str] = Field(default_factory=list)
    reason: str = ""
    current_state: str = ""
    next_step: str = ""
    limitations: list[str] = Field(default_factory=list)
    source_excerpt: bool = False
    fields_unchanged: bool = True
    advisory_steps: list[str] = Field(default_factory=list)
    advisory_escalation: str = ""
    advisory_draft: str = ""
    advisory_questions: list[str] = Field(default_factory=list)
    advisory_conflicts: list[str] = Field(default_factory=list)


def email_highlights(body: str, *, limit: int = 700) -> list[str]:
    """Quote the newest authored text, not generated claims or quoted history."""
    clean = clean_email_body(body)
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", clean)]
    result: list[str] = []
    remaining = limit
    for paragraph in paragraphs:
        if not paragraph or remaining < 30:
            continue
        excerpt = paragraph[:remaining]
        if len(paragraph) > remaining:
            excerpt += "…"
        result.append(excerpt)
        remaining -= len(excerpt)
        if len(result) == 3:
            break
    return result


def build_email_analysis_notice(
    *, summary: str, suggested_action: str = "", body: str = "",
    reason: str = "", current_state: str = "", limitations: list[str] | None = None,
    fields_unchanged: bool = True,
) -> EmailAnalysisNotice:
    highlights = email_highlights(body)
    return EmailAnalysisNotice(
        highlights=highlights or ([summary[:700]] if summary.strip() else []),
        source_excerpt=bool(highlights), reason=reason, current_state=current_state,
        next_step=suggested_action[:700], limitations=list(limitations or []),
        fields_unchanged=fields_unchanged,
    )


def render_email_analysis_notice(notice: EmailAnalysisNotice, *, language: str = "zh-TW") -> str:
    """Bound each section separately so the next step and limitations survive."""
    english = language == "en"
    labels = (
        ("Email highlights (source excerpts)", "Summary", "Why no fields changed", "Current record",
         "Suggested next step", "Analysis limitations", "These suggestions do not mean a reminder has been created.",
         "No source summary is available; inspect the original email.",
         "No project fields were applied. A specific decision reason was not supplied; inspect the analysis before updating.",
         "Review the email requirements and confirm the responsible party and timing before creating a follow-up.")
        if english else
        ("郵件重點（原文摘錄）", "郵件重點", "為何沒有欄位變更", "目前記錄",
         "建議下一步", "分析限制", "上述為建議，不代表已建立跟進提醒。",
         "未取得可用摘要，請查看原電郵。",
         "本次沒有套用專案欄位變更；分析未提供具體原因，更新前請先核對原電郵及分析記錄。",
         "請核對郵件要求，確認負責對象及時間後，再決定是否建立跟進提醒。")
    )
    highlights, summary, why, state, step, limits, reminder, empty, fallback_reason, fallback_step = labels
    lines = [highlights if notice.source_excerpt else summary]
    lines.extend(f"• {item}" for item in notice.highlights)
    if not notice.highlights:
        lines.append(empty)
    decision_heading = why if notice.fields_unchanged else ("Processing outcome" if english else "處理結果")
    lines.extend(["", decision_heading, notice.reason[:800] or fallback_reason])
    if notice.current_state:
        lines.append(f"{state}：{notice.current_state[:350]}")
    lines.extend(["", step, notice.next_step or fallback_step, reminder])
    if notice.advisory_steps:
        lines.extend(["", "Supporting next steps" if english else "其他必要跟進"])
        lines.extend(f"• {item[:500]}" for item in notice.advisory_steps[:4])
    if notice.advisory_escalation:
        lines.append(("Suggested escalation: " if english else "建議升級處理：") + notice.advisory_escalation[:500])
    if notice.advisory_draft:
        lines.append(("Draft option (not sent): " if english else "可準備草稿（尚未發送）：") + notice.advisory_draft[:350])
    if notice.advisory_questions:
        lines.extend(["", "Questions requiring confirmation" if english else "需要確認的問題"])
        lines.extend(f"• {item[:350]}" for item in notice.advisory_questions[:4])
    if notice.advisory_conflicts:
        lines.extend(["", "Conflicting records require review" if english else "記錄衝突，需要核對"])
        lines.extend(f"• {item[:350]}" for item in notice.advisory_conflicts[:4])
    if notice.limitations:
        lines.extend(["", limits])
        lines.extend(f"• {item[:350]}" for item in notice.limitations[:5])
        if len(notice.limitations) > 5:
            lines.append(f"+{len(notice.limitations) - 5}")
    return "\n".join(lines)
