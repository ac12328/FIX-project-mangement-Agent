"""Deterministic surface routing for email-to-project reconciliation."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from config import defaults
from src.agent.project_resolve import resolve_project_reference
from src.project_reconciliation.contracts import (
    ReconciliationMode,
    ReconciliationResult,
)
from src.project_reconciliation.repository import find_latest_owned_operation
from src.project_reconciliation.replay import reviewable_recon_fields
from src.project_reconciliation.service import reconcile_project_from_emails

_EMAIL_SIGNAL_RE = re.compile(
    r"(?:\be-?mail(?:s|\s+evidence|\s+history|\s+data)?\b|"
    r"email[-_ ]to[-_ ]truth|電郵|郵件|邮件)",
    re.IGNORECASE,
)
_CONTEXT_SIGNAL_RE = re.compile(
    r"(?:按(?:照)?(?:上面|以上|之前)|根據(?:上面|以上|之前|上述)?(?:資料|分析|結果|內容)?|"
    r"依(?:上面|以上|之前|上述)?(?:資料|分析|結果|內容)?|上面(?:資料|分析|結果|內容)?|"
    r"以上(?:資料|分析|結果|內容)?|上述(?:資料|分析|結果|內容)?|"
    r"\b(?:based on|according to|from)\s+(?:the\s+)?(?:above|earlier|previous)\b|"
    r"\b(?:above|earlier|previous)\s+(?:analysis|result|details?|information)\b|"
    r"\bas discussed\b)",
    re.IGNORECASE,
)
_RETRY_SIGNAL_RE = re.compile(
    r"(?:再試|重試|再一次|再做一次|重新(?:嘗試|更新)|\bretry\b|\btry again\b|"
    r"\bagain\b)",
    re.IGNORECASE,
)
_HARD_COMMIT_RE = re.compile(
    r"(?:\b(?:apply|commit|write|sync|reconcile)\b|"
    r"\bconfirm(?:\s+the)?\s+(?:email\s+)?(?:update|reconciliation)\b|"
    r"套用|寫入|写入|同步|確認更新|确认更新|落實更新|落实更新)",
    re.IGNORECASE,
)
_UPDATE_RE = re.compile(
    r"(?:\b(?:update|change|refresh|catch\s*up)\b|更新|改(?:返|成|為|为)?|追返|追上)",
    re.IGNORECASE,
)
_UPDATE_IMPERATIVE_RE = re.compile(
    r"(?:"
    r"(?:^|(?<=[\s,;:])|(?:please|pls)\s+)(?:update|change|refresh|catch\s*up)\s+"
    r"(?:it|this|the\s+project|project(?:\s+\S+)?|[A-Za-z]{2,}\d[\w-]*)"
    r"|"
    r"(?:根據|根据|按).{0,24}(?:郵件|邮件|電郵|电邮|e-?mail).{0,12}更新"
    r"|"
    r"更新\s*[A-Za-z]{2,}\d[\w-]*"
    r"|"
    r"(?:再試|重試|再一次|再做一次).{0,12}更新"
    r")",
    re.IGNORECASE,
)
_PREVIEW_RE = re.compile(
    r"(?:\b(?:check|inspect|estimate|assess|review|verify|infer)\b|"
    r"檢查|检查|核對|核对|推估|估(?:下|計|计)?|判斷|判断|睇下|看看|分析)",
    re.IGNORECASE,
)
_PROPOSAL_RE = re.compile(
    r"(?:\b(?:propose|proposal|preview|suggest|recommend)\b|"
    r"建議|建议|提議|提议|預覽|预览)",
    re.IGNORECASE,
)
_CREATE_RE = re.compile(
    r"(?:\b(?:create|open|start|kick\s*start|kickstart)\b|"
    r"建立|開立|开立|開(?:一個|一个)?(?:project|項目|项目|ticket|ticker)|"
    r"啟動|启动)",
    re.IGNORECASE,
)
_PROJECT_NOUN_RE = re.compile(
    r"(?:\b(?:project|ticket|ticker)\b|專案|项目|項目)",
    re.IGNORECASE,
)
_EMAIL_COMPOSE_RE = re.compile(
    r"(?:\b(?:draft|compose|reply|send|forward)\b|"
    r"\u8349\u64ec|\u8349\u62df|\u8d77\u8349|"
    r"\u5beb\u5c01\u96fb\u90f5|\u5199\u5c01\u90ae\u4ef6|"
    r"\u56de\u8986|\u56de\u590d|\u8f49\u5bc4|\u8f6c\u53d1)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReconciliationSurfaceRequest:
    """A high-confidence, deterministic reconciliation request."""

    mode: ReconciliationMode
    project_id: str | None
    create_if_missing: bool = False
    resume_latest: bool = False
    signal: str = "email"

    @property
    def explicit_commit(self) -> bool:
        return self.mode == ReconciliationMode.COMMIT


@dataclass(frozen=True)
class ReconciliationSurfaceExecution:
    """Surface-ready execution output with a durable service receipt."""

    request: ReconciliationSurfaceRequest
    result: ReconciliationResult
    text: str


def classify_reconciliation_request(
    message: str,
    *,
    fallback_project_ids: Iterable[str] = (),
) -> ReconciliationSurfaceRequest | None:
    """Recognize email reconciliation without intercepting ordinary field updates."""
    raw = (message or "").strip()
    if not raw:
        return None

    has_email = bool(_EMAIL_SIGNAL_RE.search(raw))
    has_context = bool(_CONTEXT_SIGNAL_RE.search(raw))
    has_retry = bool(_RETRY_SIGNAL_RE.search(raw))
    has_hard_commit = bool(_HARD_COMMIT_RE.search(raw))
    has_update = bool(_UPDATE_RE.search(raw))
    has_imperative_update = bool(_UPDATE_IMPERATIVE_RE.search(raw))
    has_preview = bool(_PREVIEW_RE.search(raw))
    has_proposal = bool(_PROPOSAL_RE.search(raw))
    # Retrying a previous write is an execution request even when "update" is a noun.
    if has_retry and has_update:
        has_imperative_update = True

    if not (has_email or has_context or has_retry):
        return None
    # Email drafting/replying is a different workflow. Do not swallow a
    # multi-action or compose request just because it also contains "update".
    if _EMAIL_COMPOSE_RE.search(raw):
        return None
    if not (has_hard_commit or has_update or has_preview or has_proposal):
        return None

    # Inspect/proposal wording is fail-closed unless the operator used an
    # unambiguous apply/commit/write/sync verb. Noun "update" is preview.
    if (has_preview or has_proposal) and not has_hard_commit:
        mode = ReconciliationMode.PREVIEW
    elif has_hard_commit or has_imperative_update:
        mode = ReconciliationMode.COMMIT
    else:
        mode = ReconciliationMode.PREVIEW

    resolved = resolve_project_reference(raw)
    project_id = None if resolved.ambiguous else resolved.project_id
    if project_id is None:
        fallback = [
            str(item).strip().upper()
            for item in fallback_project_ids
            if str(item).strip()
        ]
        fallback = list(dict.fromkeys(fallback))
        if len(fallback) == 1:
            project_id = fallback[0]

    create_if_missing = bool(
        mode == ReconciliationMode.COMMIT
        and _CREATE_RE.search(raw)
        and _PROJECT_NOUN_RE.search(raw)
        and has_email
    )
    signal = "retry" if has_retry else "context" if has_context else "email"
    return ReconciliationSurfaceRequest(
        mode=mode,
        project_id=project_id,
        create_if_missing=create_if_missing,
        resume_latest=has_retry or has_context,
        signal=signal,
    )


def _workspace_write_gate(
    request: ReconciliationSurfaceRequest,
    trusted_surface: str,
) -> ReconciliationSurfaceRequest:
    """Workspace commits follow the operator-writes flag; Telegram is unchanged."""
    if (
        trusted_surface != "ai_workspace"
        or request.mode != ReconciliationMode.COMMIT
    ):
        return request
    if bool(getattr(defaults, "WORKSPACE_OPERATOR_WRITES_ENABLED", False)):
        return request
    return replace(request, mode=ReconciliationMode.PREVIEW, create_if_missing=False)


def execute_reconciliation_request(
    conn: sqlite3.Connection,
    *,
    request: ReconciliationSurfaceRequest,
    operator_id: str | None,
    chat_id: str,
    trusted_surface: str,
    request_key: str | None = None,
    trigger_kind: str = "operator",
    post_commit: list[Callable[[], None]] | None = None,
    allowed_project_ids: Iterable[str] | None = None,
) -> ReconciliationSurfaceExecution:
    """Execute one classified request, resuming the latest owned checkpoint when safe."""
    actor = (operator_id or "").strip()
    request = _workspace_write_gate(request, trusted_surface)
    if not actor or actor == "None":
        result = ReconciliationResult(
            ok=False,
            status="needs_operator",
            outcome="rejected",
            project_id=request.project_id or "",
            operation_id="not_started",
            error_code="operator_required",
            error="A bound operator identity is required to reconcile project truth.",
            text="Project reconciliation did not start.",
        )
        return ReconciliationSurfaceExecution(
            request=request,
            result=result,
            text=format_reconciliation_result(result),
        )

    project_id = request.project_id
    latest = None
    if request.mode == ReconciliationMode.COMMIT or request.resume_latest:
        latest = find_latest_owned_operation(
            conn,
            operator_id=actor,
            chat_id=str(chat_id),
            trusted_surface=trusted_surface,
            project_id=project_id,
        )
        if project_id is None and latest is not None:
            project_id = str(latest.get("resource_key") or "").strip().upper() or None

    if project_id is None:
        result = ReconciliationResult(
            ok=False,
            status="needs_project",
            outcome="rejected",
            project_id="",
            operation_id="not_started",
            error_code="project_id_required",
            error="A single project could not be resolved from the instruction or active scope.",
            text="Project reconciliation did not start.",
        )
        return ReconciliationSurfaceExecution(
            request=request,
            result=result,
            text=format_reconciliation_result(result),
        )

    if allowed_project_ids is not None:
        allowed = {str(item).strip().upper() for item in allowed_project_ids}
        if project_id not in allowed:
            result = ReconciliationResult(
                ok=False,
                status="denied",
                outcome="rejected",
                project_id=project_id,
                operation_id="not_started",
                error_code="project_outside_workspace_scope",
                error="The resolved reconciliation project is outside the current scope.",
                text="Project reconciliation did not start.",
            )
            return ReconciliationSurfaceExecution(
                request=replace(request, project_id=project_id),
                result=result,
                text=format_reconciliation_result(result),
            )

    result = reconcile_project_from_emails(
        conn,
        project_id=project_id,
        mode=request.mode,
        operator_id=actor,
        chat_id=str(chat_id),
        trusted_surface=trusted_surface,
        trusted_explicit_operator=request.explicit_commit,
        request_key=request_key,
        resume_operation_id=(
            str(latest["operation_id"])
            if latest is not None
            else None
        ),
        create_if_missing=request.create_if_missing,
        trigger_kind=trigger_kind,
        post_commit=post_commit,
    )
    return ReconciliationSurfaceExecution(
        request=ReconciliationSurfaceRequest(
            mode=request.mode,
            project_id=project_id,
            create_if_missing=request.create_if_missing,
            resume_latest=request.resume_latest,
            signal=request.signal,
        ),
        result=result,
        text=format_reconciliation_result(result),
    )


def format_reconciliation_result(result: ReconciliationResult) -> str:
    """Format a transparent Traditional Chinese DB receipt for operator surfaces."""
    project_id = result.project_id or "指定專案"
    operation = result.operation_id or "not_started"

    if result.status == "pending_commit":
        return (
            f"已為 **{project_id}** 暫存郵件核對變更；尚待外層交易提交，"
            "目前未能確認 DB 已持久化。若外層交易回滾，變更不會生效。\n"
            f"• Operation：`{operation}`"
        )

    if result.committed:
        created = "（已先建立專案）" if result.project_created else ""
        state_changed = bool(reviewable_recon_fields(result.applied_fields))
        lines = [
            f"已根據郵件更新 **{project_id}**{created}，並完成 DB 回讀確認。"
            if state_changed or result.project_created else
            f"已為 **{project_id}** 儲存郵件核對 Notes；**專案狀態及待辦未變更**。",
        ]
        lines.extend(_format_fields(result.applied_fields))
        lines.append(f"• Operation：`{operation}`")
        return "\n".join(lines)

    if result.status == "no_change" or result.outcome in {"no_change", "no_evidence"}:
        detail = (
            "找不到可用的歷史郵件證據"
            if result.outcome == "no_evidence"
            else "現有證據未能建立可套用的專案狀態變更"
        )
        return (
            f"已核對 **{project_id}**：{detail}，沒有寫入專案欄位。\n"
            f"• Operation：`{operation}`"
        )

    if result.outcome == "superseded" or result.status == "superseded":
        return (
            f"已毋須處理 **{project_id}** 的較舊核對：較新的 reconciliation 已完成，"
            "這個 operation 已自動結束，毋須到 Dashboard 批准。\n"
            f"• Operation：`{operation}`"
        )

    if result.outcome == "proposed" or result.status == "needs_review" and not result.error_code:
        lines = [
            f"已為 **{project_id}** 建立郵件核對預覽，**尚未寫入 DB**。",
            "建議變更：",
        ]
        lines.extend(_format_fields(result.proposed_fields))
        lines.append(f"• Operation：`{operation}`")
        if result.proposed_update_id:
            lines.append(f"• Review：`{result.proposed_update_id}`")
            lines.append("Dashboard 位置：**Pending reviews**（不是 Approvals）。")
        lines.append("請覆核引用證據及建議值；里程碑變更仍須通過審核政策。")
        return "\n".join(lines)

    if result.status == "needs_review" and result.proposed_update_id:
        lines = [
            f"**{project_id} 尚未更新。** 安全寫入政策要求人手覆核。",
            "待覆核變更：",
        ]
        lines.extend(_format_fields(result.proposed_fields))
        lines.append(f"• Review：`{result.proposed_update_id}`")
        lines.append(f"• Operation：`{operation}`")
        lines.append("請到 Dashboard 的 **Pending reviews** 處理（不是 Approvals）。")
        return "\n".join(lines)

    reason = _failure_reason(result)
    return (
        f"**{project_id} 未有更新。** {reason}\n"
        "專案 truth 維持不變。\n"
        f"• Operation：`{operation}`"
    )


def _format_fields(fields: dict) -> list[str]:
    if not fields:
        return ["• 沒有需要變更的欄位"]
    labels = {
        "current_milestone": "Milestone",
        "project_status": "Status",
        "ball_with": "Ball",
        "next_action": "Next Action",
        "target_go_live": "Target Go-Live",
        "market": "Market",
        "notes": "Notes",
    }
    ordered = [
        "current_milestone",
        "project_status",
        "ball_with",
        "next_action",
        "target_go_live",
        "market",
        "notes",
    ]
    keys = [key for key in ordered if key in fields]
    keys.extend(key for key in fields if key not in keys)
    lines: list[str] = []
    for key in keys:
        value = fields[key]
        if key == "notes":
            value = "已加入可重放的郵件 evidence marker"
        lines.append(f"• {labels.get(key, key)}：{value}")
    return lines


def _failure_reason(result: ReconciliationResult) -> str:
    messages = {
        "operator_required": "未能識別 operator 身分，沒有寫入專案欄位。",
        "project_id_required": "未能從指示或目前 scope 唯一識別專案。",
        "project_missing": "系統尚未建立此專案；除非你明確要求從郵件建立，否則不會自動開 project。",
        "project_version_conflict": "核對期間專案資料持續被其他操作修改；系統沒有建立 Dashboard 批准項目，請稍後重試。",
        "project_created_after_preview": "preview 後專案已由其他流程建立，需要重新核對。",
        "operator_authorization_required": "缺少可信的明確更新指示。",
        "four_eyes_required": "此變更跨越多個 milestone，需要第二人在 Pending reviews 覆核。",
        "project_outside_workspace_scope": "續接的專案不在目前 Workspace scope；沒有執行核對或更新。",
        "commit_requires_post_commit_scope": "借用外層交易時缺少提交後回呼；沒有寫入專案，請由受控交易入口重試。",
        "operation_scope_mismatch": "該 reconciliation operation 不屬於目前 operator/chat scope。",
    }
    if result.error_code in messages:
        return messages[result.error_code]
    if result.error:
        return f"原因：{result.error}"
    return "reconciliation 未能完成。"


def reconciliation_tool_trace(
    execution: ReconciliationSurfaceExecution,
) -> dict:
    """Return the standard tool trace shape used by the Workspace response parts."""
    return {
        "tool": "project_reconcile",
        "arguments": {
            "project_id": execution.request.project_id,
            "mode": execution.request.mode.value,
            "create_if_missing": execution.request.create_if_missing,
        },
        "result": execution.result.model_dump(mode="json"),
    }
