"""Deterministic go-live staging — bypasses multi-round orchestrator thrash.

When the operator asks to mark a project go-live after PCT, stage:
  current_milestone → M8_GoLive
  ball_with → Internal
  next_action → ask operator to confirm close

Does NOT set project_status=Completed (requires explicit sign-off).
"""
from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from typing import Any

from src.agent.guards import (
    _sign_off_project_id,
    is_draft_request,
    is_inbound_email_context,
    is_production_sign_off,
    is_staged_update_confirm_intent,
)
from src.models.enums import Milestone
from src.tools.executor import execute_tool

logger = logging.getLogger(__name__)

_M8 = Milestone.M8_GO_LIVE.value
_M7 = Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY.value

_GO_LIVE_INTENT_RE = re.compile(
    r"(?:"
    r"go[\s-]?live|m8_?golive|to\s+go[\s-]?live|mark\s+.*go[\s-]?live"
    r"|update\s+.*go[\s-]?live|set\s+.*m8"
    r"|pct\s+(?:is\s+)?complete|pct\s+completed|tests?\s+are\s+passed"
    r"|生產上線|改?\s*go\s*live|上線"
    r")",
    re.IGNORECASE,
)

_NEXT_ACTION = (
    "PCT completed (M7 done). Project at M8_GoLive. "
    "Ask operator to confirm close project (Completed) if no residual open work."
)


@contextmanager
def _write_conn(conn: sqlite3.Connection | None):
    if conn is not None:
        yield conn
        return
    from src.db.connection import scoped_write

    with scoped_write() as wconn:
        yield wconn


def is_go_live_update_intent(user_text: str) -> bool:
    """True for explicit go-live / PCT-complete → M8 write requests."""
    if not user_text or not user_text.strip():
        return False
    if re.search(
        r"(?:can|could|ready|status|whether|should)\b.{0,24}\bgo[\s-]?live"
        r"|go[\s-]?live.{0,24}\b(?:ready|status)\b"
        r"|(?:可以|可否|能否|是否|準備好).{0,16}(?:go[\s-]?live|上線)"
        r"|(?:go[\s-]?live|上線).{0,16}(?:嗎|未|狀態|準備好)",
        user_text,
        re.IGNORECASE,
    ):
        return False
    if is_production_sign_off(user_text) and not _GO_LIVE_INTENT_RE.search(user_text):
        # Pure close language without go-live → sign-off path
        return False
    return bool(_GO_LIVE_INTENT_RE.search(user_text))


def try_deterministic_go_live(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
) -> tuple[str, str] | None:
    """Stage M8 and its committed card intent. Returns (reply, project_id) or None."""
    if not _should_run_go_live(conn, user_text, chat_id):
        return None

    project_id = _sign_off_project_id(conn, user_text, chat_id=chat_id)
    if not project_id:
        return None

    try:
        from src.repositories.project_repo import get_project_field

        def _read_ms(c: sqlite3.Connection) -> str | None:
            return get_project_field(c, project_id, "current_milestone")

        if conn is not None:
            current_ms = _read_ms(conn)
        else:
            from src.db.connection import scoped_read
            with scoped_read() as rconn:
                current_ms = _read_ms(rconn)

        if current_ms == _M8:
            return (
                f"**{project_id}** 已在 **M8_GoLive**。\n\n"
                "需要我幫你 **關閉 project（Completed）** 嗎？\n"
                "（可回覆：關閉 / confirm close / sign off）",
                project_id,
            )

        # Only allow jump from M7 (PCT complete). Earlier milestones need evidence path.
        if current_ms and current_ms != _M7:
            return (
                f"**{project_id}** 目前 milestone 是 `{current_ms}`，"
                f"deterministic go-live 只接受 **M7 → M8**（PCT complete）。\n"
                f"若已有 PCT 完成證據，請先確認 milestone 為 M7，或用 Dashboard 帶 evidence 更新。",
                project_id,
            )

        batch_args = {
            "project_id": project_id,
            "reason": (
                "PM Telegram: PCT complete → M8_GoLive; "
                "ask operator to confirm project close"
            ),
            "changes": [
                {"field": "current_milestone", "new_value": _M8},
                {"field": "ball_with", "new_value": "Internal"},
                {"field": "next_action", "new_value": _NEXT_ACTION},
            ],
            "evidence_ref": "manual_note:operator_pct_complete_go_live",
        }

        with _write_conn(conn) as wconn:
            stage_result = execute_tool(
                wconn, "deterministic_go_live", "update_project_batch", batch_args,
                execution_context={
                    "deterministic_authorization": True,
                    "explicit_operator_instruction": True,
                    "operator_id": "default_operator",
                    "chat_id": chat_id,
                },
            )
            stage_text = _result_text(stage_result)
            if stage_text.startswith("Update rejected") or stage_text.startswith("Error:"):
                return (
                    f"無法為 **{project_id}** 準備 go-live 更新：{stage_text}",
                    project_id,
                )
            if "No changes needed" in stage_text:
                return (
                    f"**{project_id}** 已是 go-live 目標狀態。\n"
                    "需要我幫你 **關閉 project（Completed）** 嗎？",
                    project_id,
                )

            # The tool staged the operation and card intent in one transaction.
            card_queued = bool(stage_result.get("card_queued")) if isinstance(stage_result, dict) else False

            reply = (
                f"已為 **{project_id}** 準備 **Go-Live** 更新（PCT complete 政策）：\n"
                f"- current_milestone → **{_M8}**（M7 complete）\n"
                f"- ball_with → **Internal**\n"
                f"- next_action → 請 operator 確認是否關閉 project\n\n"
                "確認卡片會在暫存提交後送出；也可回覆 confirm_update / cancel_update。\n"
                "確認後我會再問你是否 **關閉 project（Completed）**。"
            )
            if not card_queued:
                reply += (
                    "\n\n（未有確認卡片投遞意圖；可回覆 confirm_update 確認或 cancel_update 取消。）"
                )
            logger.info(
                "deterministic_go_live_staged project=%s chat_id=%s card_queued=%s",
                project_id,
                chat_id,
                card_queued,
            )
            return reply, project_id
    except Exception:
        logger.exception("deterministic_go_live failed project=%s", project_id)
        return (
            f"Go-live **{project_id}** 時發生錯誤，請稍後重試或使用 Dashboard。",
            project_id,
        )


def _should_run_go_live(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int,
) -> bool:
    if (
        is_inbound_email_context(user_text)
        or is_staged_update_confirm_intent(user_text)
        or is_draft_request(user_text)
    ):
        return False
    # Pure close without go-live language → sign-off path only
    if is_production_sign_off(user_text) and not re.search(
        r"go[\s-]?live|m8|pct|上線", user_text or "", re.I
    ):
        return False
    if not is_go_live_update_intent(user_text):
        return False
    return _sign_off_project_id(conn, user_text, chat_id=chat_id) is not None


def _result_text(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("text") or result.get("error") or result)
    return str(result)
