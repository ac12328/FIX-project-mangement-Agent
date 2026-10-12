"""Presentation of inbound project email analysis outside the policy router.

Caller-owned write scopes are used for digest records and project lookup.
The info-card sender runs after the project lookup scope has closed; its
outbound persistence and budget accounting own separate short write scopes.
"""
from __future__ import annotations

import logging

from config.settings import CONFIG
from src.telegram.i18n import t

logger = logging.getLogger(__name__)


def _is_raw_tool_dump(text: str) -> bool:
    """Detect if text is a raw tool result dump rather than human-readable output."""
    if not text:
        return False
    stripped = text.strip()
    if stripped.startswith(("emails:", "{'", "[{", '{"', "[{")):
        return True
    if "'reply_id':" in stripped and "'email_date':" in stripped:
        return True
    if stripped.count("None") > 3 and ("'summary':" in stripped or "'confidence':" in stripped):
        return True
    return False


def _format_raw_email_dump(raw: str, project_id: str) -> str | None:
    """Try to extract a brief summary from raw email tool output."""
    import ast
    try:
        payload = raw.strip()
        if payload.startswith("emails:"):
            payload = payload[len("emails:"):].strip()
        items = ast.literal_eval(payload)
        if not isinstance(items, list) or not items:
            return None
        senders = {e.get("sender_email") for e in items if e.get("sender_email")}
        subjects = {e.get("subject", "")[:60] for e in items if e.get("subject")}
        inbound = sum(1 for e in items if e.get("direction") == "inbound")
        outbound = sum(1 for e in items if e.get("direction") == "outbound")
        parts = [f"📨 {len(items)} emails processed"]
        if inbound:
            parts.append(f"↓ {inbound} inbound")
        if outbound:
            parts.append(f"↑ {outbound} outbound")
        if senders:
            parts.append(f"From: {', '.join(sorted(senders)[:3])}")
        if subjects:
            parts.append(f"Re: {next(iter(subjects))}")
        return " | ".join(parts)
    except Exception:
        return None


def prepare_email_info_card(
    conn, project_id: str, *, summary: str, suggested_action: str = "",
    sender_email: str | None = None, subject: str | None = None,
    body: str = "", serve_result=None, write_action: str | None = "skipped",
    fetched=None,
) -> dict | None:
    """Freeze the source-grounded notice before commit; never send here."""
    from src.repositories.project_repo import get_project

    from src.email.analysis_notice import build_email_analysis_notice
    from src.email.ticket_notice import is_passive_acknowledgment
    # Suppress only the no-write notification, not analysis/evidence/review.
    attachments_known = fetched is not None or (serve_result is not None and serve_result.case is not None)
    has_attachments = bool(getattr(fetched, "attachments", ())) or bool(
        serve_result is not None and serve_result.case is not None and serve_result.case.forwarded_attachment)
    if write_action in (None, "skipped") and attachments_known and is_passive_acknowledgment(body, has_attachments=has_attachments):
        from src.repositories.digest_queue_repo import enqueue_digest_item
        enqueue_digest_item(conn, category="email_information", title=subject or project_id,
            body="Acknowledgment only", project_id=project_id,
            metadata={"action_required": False, "email_id": getattr(fetched, "email_id", None)},
            commit=False)
        return
    from src.telegram.i18n import get_ui_language
    english = get_ui_language() == "en"
    decision_reason = ""
    limitations: list[str] = []
    if serve_result is not None:
        from src.email.project_truth.serve_renderer import render_email_project_truth_decision_text
        decision_reason, _ = render_email_project_truth_decision_text(
            serve_result, language=get_ui_language(),
        )
        case = serve_result.case
        if case is not None:
            body = case.newest_authored_body
            for name in case.partial_attachment_names:
                limitations.append(f"Attachment only partially read: {name}." if english
                                   else f"附件只讀取部分內容：{name}；超出範圍的內容未評估。")
            for name in case.unread_attachment_names:
                limitations.append(
                    f"Attachment not read: {name}. Its contents were not assessed."
                    if english else f"附件未讀取：{name}；本次未評估其內容。"
                )
    elif getattr(fetched, "attachments", None):
        limitations.append(
            "Attachment coverage was not verified for this analysis; do not assume all attachments were read."
            if english else "本次分析未取得附件讀取範圍的確認，請勿假設所有附件均已讀取。"
        )
    proj = get_project(conn, project_id)
    client_name = proj.client_name if proj else None
    current_state = ""
    if proj:
        current_state = " / ".join(
            str(getattr(value, "value", value))
            for value in (proj.current_milestone, proj.project_status, proj.ball_with)
            if value is not None
        )
    fields_unchanged = write_action in (None, "skipped", "review_persistence_failed")
    if write_action == "review_persistence_failed":
        decision_reason = (
            "Review proposal was not queued; no project fields were changed. "
            "Inspect the source email and retry the review after resolving the persistence error."
            if english else
            "?????????????????????????????????????????"
        )
    if not fields_unchanged:
        # This sender is also used for committed action/PCT outcomes.
        # Never relabel an actual write as 'no fields changed'.
        decision_reason = summary
    notice = build_email_analysis_notice(
        summary=summary, suggested_action=suggested_action, body=body,
        reason=decision_reason, current_state=current_state, limitations=limitations,
        fields_unchanged=fields_unchanged,
    )
    return {
        "project_id": project_id, "summary": summary,
        "suggested_action": suggested_action, "sender_email": sender_email,
        "subject": subject, "client_name": client_name, "notice": notice.model_dump(),
    }


def push_email_info_card(
    project_id: str,
    *,
    summary: str,
    suggested_action: str = "",
    sender_email: str | None = None,
    subject: str | None = None,
    body: str = "",
    serve_result=None,
    write_action: str | None = "skipped",
    fetched=None,
    write_scope=None,
) -> None:
    """Compatibility sender; the inbound route uses the durable outbox instead."""
    try:
        from src.db.connection import scoped_write
        from src.email.analysis_notice import EmailAnalysisNotice
        from src.telegram.review_push import push_email_info_card_sync

        write_scope = write_scope or scoped_write
        with write_scope() as wc:
            payload = prepare_email_info_card(
                wc, project_id, summary=summary, suggested_action=suggested_action,
                sender_email=sender_email, subject=subject, body=body,
                serve_result=serve_result, write_action=write_action, fetched=fetched,
            )
        if payload is None:
            return
        push_email_info_card_sync(
            None, project_id=project_id, summary=summary,
            suggested_action=suggested_action, sender_email=sender_email,
            subject=subject, client_name=payload["client_name"],
            notice=EmailAnalysisNotice.model_validate(payload["notice"]),
        )
    except Exception:
        logger.exception("Failed to push email info card for %s", project_id)



def prepare_plain_notification(result: str, project_id: str) -> dict | None:
    """Suppress raw tool dumps before recording an immutable plain-message intent."""
    if _is_raw_tool_dump(result):
        result = _format_raw_email_dump(result, project_id)
        if not result:
            logger.debug("Suppressing raw tool dump notification for %s", project_id)
            return None
    text = t("policy.email_update_result").format(
        project_id=project_id, result=result,
    )[:4096]
    return {"project_id": project_id, "result": result, "text": text}


def send_email_update_notification(
    result: str, project_id: str, *, raw_detector=None, raw_formatter=None,
) -> None:
    """Fallback plain Telegram message (legacy path).

    Prefer structured cards from review/auto-apply/info helpers.
    Best-effort — never raises so Telegram failure cannot block inbound processing.
    """
    try:
        import requests
        bot_token = CONFIG.telegram.bot_token
        chat_id = CONFIG.telegram.chat_id
        raw_detector = raw_detector or _is_raw_tool_dump
        raw_formatter = raw_formatter or _format_raw_email_dump
        if raw_detector(result):
            formatted = raw_formatter(result, project_id)
            if formatted:
                result = formatted
            else:
                logger.debug("Suppressing raw tool dump notification for %s", project_id)
                return
        text = t("policy.email_update_result").format(project_id=project_id, result=result)
        text = text[:4096]
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if not resp.ok:
            logger.warning(
                "Telegram sendMessage returned %d for project %s",
                resp.status_code,
                project_id,
            )
    except Exception:
        logger.exception("Failed to send email update notification for %s", project_id)



def project_update_failure_notification(event) -> dict:
    """Build the existing operator-review payload after both update paths fail."""
    from config.defaults import EMAIL_BODY_STORE_CHARS

    return {
        "profile": "B",
        "title": t("policy.update_needs_attention").format(subject=event.subject[:50]),
        "body": t("policy.update_body").format(
            sender=event.sender_email,
            classification=event.classification_type,
            confidence=f"{event.confidence:.2f}",
            reason=(event.reason or "")[:EMAIL_BODY_STORE_CHARS],
        ),
        "project_id": event.project_id,
    }
