"""Deterministic operator-facing text for Email Project Truth serving."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.utils.operator_language import normalize_response_language

if TYPE_CHECKING:
    from src.email.project_truth.serve import EmailProjectTruthServeResult


_MESSAGES: dict[str, dict[str, tuple[str, str]]] = {
    "en": {
        "identity": (
            "The email could not be matched safely to the selected project, so no project data was changed.",
            "Confirm the project identity before applying any update.",
        ),
        "contradiction": (
            "The email evidence is contradictory, so no project data was changed.",
            "Resolve the conflict with a current authoritative source.",
        ),
        "later_stage": (
            "The email describes later-stage work than the recorded milestone, so no project data was changed.",
            "Review the recorded milestone against the later-stage evidence before updating any field.",
        ),
        "terminal": (
            "The project is already in a terminal state, so the email did not change onboarding fields.",
            "Handle the issue separately or request explicit operator review.",
        ),
        "citations": (
            "The proposed change was not fully supported by exact source citations, so no project data was changed.",
            "Review the source email before making a manual update.",
        ),
        "insufficient": (
            "The email adds new information, but it does not yet prove that project state should change.",
            "Keep the current project state and obtain the missing confirmation.",
        ),
        "no_change": (
            "The email was assessed and no project-field change is needed.",
            "Keep the current project state.",
        ),
    },
    "zh-CN": {
        "identity": (
            "未能安全确认邮件所属项目，因此没有更新项目资料。",
            "请先确认项目身份，再应用任何更新。",
        ),
        "contradiction": (
            "邮件证据互相矛盾，因此没有更新项目资料。",
            "请用最新的权威资料解决冲突。",
        ),
        "later_stage": (
            "邮件描述的进度晚于记录中的里程碑，因此没有更新项目资料。",
            "请先对照后期邮件证据核对记录中的里程碑，再更新任何字段。",
        ),
        "terminal": (
            "项目已经处于终止状态，因此邮件没有修改 onboarding 字段。",
            "请把问题分开处理，或要求操作员明确审核。",
        ),
        "citations": (
            "建议变更未获完整的原文引用支持，因此没有更新项目资料。",
            "请先核对原邮件，再考虑手动更新。",
        ),
        "insufficient": (
            "邮件提供了新资料，但仍不足以证明项目状态需要改变。",
            "请保留现有状态，并取得欠缺的确认。",
        ),
        "no_change": (
            "邮件已经完成评估，目前无需修改项目字段。",
            "请保留现有项目状态。",
        ),
    },
    "zh-TW": {
        "identity": (
            "未能安全確認電郵屬於邊個專案，所以未有更新專案資料。",
            "請先確認專案身份，再套用任何更新。",
        ),
        "contradiction": (
            "電郵證據互相矛盾，所以未有更新專案資料。",
            "請用最新嘅權威資料釐清衝突。",
        ),
        "later_stage": (
            "電郵描述嘅進度遲過記錄入面嘅里程碑，所以未有更新專案資料。",
            "請先對照後期電郵證據核對記錄入面嘅里程碑，再更新任何欄位。",
        ),
        "terminal": (
            "專案已經係終止狀態，所以電郵未有改動 onboarding 欄位。",
            "請將問題分開處理，或者要求操作員明確審核。",
        ),
        "citations": (
            "建議變更未有完整原文引用支持，所以未有更新專案資料。",
            "請先核對原電郵，再考慮手動更新。",
        ),
        "insufficient": (
            "電郵提供咗新資料，但未足以證明專案狀態需要改變。",
            "請保留現有狀態，並取得欠缺嘅確認。",
        ),
        "no_change": (
            "電郵已完成評估，目前毋須修改專案欄位。",
            "請保留現有專案狀態。",
        ),
    },
}

_PROPOSAL_MESSAGES = {
    "en": (
        "The email evidence supports the following project updates.",
        "Review the proposed values and cited evidence before applying them.",
    ),
    "zh-CN": (
        "邮件证据支持以下项目资料更新。",
        "请核对建议值及引用证据，再决定是否应用。",
    ),
    "zh-TW": (
        "電郵證據支持以下專案資料更新。",
        "請核對建議值同引用證據，再決定係咪套用。",
    ),
}


def render_email_project_truth_decision_text(
    result: "EmailProjectTruthServeResult",
    *,
    language: str | None = None,
) -> tuple[str, str]:
    """Render concise operator text without another model call."""
    lang = normalize_response_language(language)
    if result.proposed_fields:
        return _PROPOSAL_MESSAGES[lang]

    reasons = set(result.safety_reasons)
    recommendation = result.recommendation
    if reasons & {
        "project_identity_unresolved",
        "project_identity_mismatch",
        "assessment_project_mismatch",
    }:
        key = "identity"
    elif "later_stage_milestone_lag" in reasons:
        key = "later_stage"
    elif "contradictory_evidence" in reasons:
        key = "contradiction"
    elif "terminal_project_protection" in reasons:
        key = "terminal"
    elif "incomplete_state_change_citations" in reasons:
        key = "citations"
    elif recommendation is not None and "INVALID_STATE_TRANSITION" in {
        getattr(code, "value", code)
        for code in recommendation.assessment.assessment_codes
    }:
        key = "later_stage"
    elif recommendation is not None and recommendation.assessment.contradictions:
        key = "contradiction"
    elif recommendation is not None and recommendation.missing_evidence:
        key = "insufficient"
    else:
        key = "no_change"
    reason, action = _MESSAGES[lang][key]
    if key in {"insufficient", "no_change"} and recommendation is not None:
        if recommendation.missing_evidence:
            label = "Missing confirmation" if lang == "en" else "尚欠確認"
            reason += f"\n{label}: {recommendation.missing_evidence[0][:350]}"
        # The deterministic evaluator knows existing actions and missing criteria.
        # Do not replace those with the old generic 'keep current state' sentence.
        if recommendation.suggested_action:
            action = recommendation.suggested_action
    return reason, action


def render_email_project_truth_operator_text(
    result: "EmailProjectTruthServeResult", *, language: str | None = None,
) -> tuple[str, str]:
    """Preserve substantive email content in persisted analysis as well as cards."""
    reason, action = render_email_project_truth_decision_text(result, language=language)
    if result.case is None:
        return reason, action
    from src.email.analysis_notice import email_highlights
    highlights = email_highlights(result.case.newest_authored_body)
    prefix = "Source excerpts" if normalize_response_language(language) == "en" else "郵件重點（原文摘錄）"
    summary = f"{prefix}: " + " / ".join(highlights) + "\n" + reason if highlights else reason
    return summary, action


__all__ = ["render_email_project_truth_operator_text"]
