"""Handler for Batch Update card callbacks (BATCH:ACTION:session_id[:idx])."""

from __future__ import annotations

import json
import sqlite3

from src.telegram.cards import InteractionCard
from src.telegram.callback_protocol import format_callback_data
from src.telegram.i18n import t


def build_batch_card(session_id: str, preview) -> "InteractionCard":
    """Build an interactive batch card with per-item [✓] [✗] buttons."""
    from src.telegram.cards import build_card

    pending = [i for i in preview.items if i.status == "pending"]
    done_count = len(preview.items) - len(pending)

    lines = [f"Batch: {preview.instruction}"]
    lines.append(t("batch.card_status_pending").format(count=len(pending)) + (t("batch.card_status_completed").format(count=done_count) if done_count else ""))
    lines.append("")
    for i, item in enumerate(preview.items[:10]):
        if item.status == "applied":
            icon = "✓"
        elif item.status in ("skipped", "error"):
            icon = "✗"
        else:
            icon = "○"
        lines.append(f"{icon} [{i}] {item.project_id} ({item.client_name or '-'})")
    if len(preview.items) > 10:
        lines.append(t("batch.card_more").format(count=len(preview.items) - 10))
    body = "\n".join(lines)

    extra_buttons: list = []
    # Bulk actions
    extra_buttons.append([
        ("✓ CONFIRM ALL", format_callback_data("BATCH", "CONFIRM_ALL", session_id)),
        ("✗ REJECT ALL", format_callback_data("BATCH", "REJECT_ALL", session_id)),
    ])
    # Per-item rows (cap at 8 to stay within Telegram limits)
    for i, item in enumerate(preview.items[:8]):
        if item.status == "pending":
            entity = f"{session_id}:{i}"
            extra_buttons.append([
                (f"✓ {item.project_id}", format_callback_data("BATCH", "APPROVE", entity)),
                (f"✗ {item.project_id}", format_callback_data("BATCH", "REJECT", entity)),
                (f"✎ {item.project_id}", format_callback_data("BATCH", "EDIT", entity)),
            ])

    return build_card("BATCH", "Batch Update", body, extra_buttons=extra_buttons)


def handle_batch(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
) -> dict:
    """Handle BATCH:ACTION:session_id[:idx] callbacks."""
    from src.portfolio.batch_update import (
        BatchPreview,
        execute_batch,
        load_batch_session,
        save_batch_session,
    )
    from src.telegram.cards import format_card_text
    from src.telegram.markup import card_to_markup
    from src.telegram.session import complete_session, get_session

    # Decode session_id (and optional item index) from project_id param
    session_id = project_id
    idx: int | None = None
    if project_id and ":" in project_id:
        session_id, _, rest = project_id.partition(":")
        try:
            idx = int(rest)
        except ValueError:
            idx = None

    if not session_id:
        return {"text": t("batch.missing_session")}

    session = get_session(conn, session_id)
    if session and session["operator_id"] != operator_id:
        return {"_error": True, "text": "Session unavailable. No action was taken."}
    if not session or session["status"] != "active":
        return {"text": t("batch.session_not_found")}

    preview = load_batch_session(conn, session_id)
    if preview is None:
        return {"text": t("batch.session_not_found")}

    if action == "CONFIRM_ALL":
        results = execute_batch(conn, preview)
        applied = sum(1 for r in results if r.status == "applied")
        failed = sum(1 for r in results if r.status in ("skipped", "error"))
        save_batch_session(conn, session_id, preview)
        complete_session(conn, session_id)
        return {"text": t("batch.confirm_all_result").format(applied=applied, failed=failed)}

    if action == "REJECT_ALL":
        for item in preview.items:
            if item.status == "pending":
                item.status = "skipped"
        save_batch_session(conn, session_id, preview)
        complete_session(conn, session_id)
        return {"text": t("batch.rejected_all")}

    if action == "APPROVE" and idx is not None:
        if 0 <= idx < len(preview.items) and preview.items[idx].status == "pending":
            item = preview.items[idx]
            single = BatchPreview(
                instruction=preview.instruction,
                items=[item],
                total_affected=1,
            )
            execute_batch(conn, single)
        save_batch_session(conn, session_id, preview)
        card = build_batch_card(session_id, preview)
        return {
            "text": format_card_text(card),
            "reply_markup": card_to_markup(card),
            "project_id": preview.items[idx].project_id if idx < len(preview.items) else None,
        }

    if action == "REJECT" and idx is not None:
        if 0 <= idx < len(preview.items) and preview.items[idx].status == "pending":
            preview.items[idx].status = "skipped"
        save_batch_session(conn, session_id, preview)
        card = build_batch_card(session_id, preview)
        return {
            "text": format_card_text(card),
            "reply_markup": card_to_markup(card),
            "project_id": preview.items[idx].project_id if idx < len(preview.items) else None,
        }

    if action == "EDIT" and idx is not None:
        if 0 <= idx < len(preview.items) and preview.items[idx].status == "pending":
            # Transition the batch session to edit-input mode
            row = conn.execute(
                "SELECT intermediate_data FROM telegram_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            raw = json.loads(row["intermediate_data"]) if row and row["intermediate_data"] else {}
            raw["editing_idx"] = idx
            conn.execute(
                """UPDATE telegram_sessions
                   SET flow_type = 'batch_item_edit', intermediate_data = ?
                   WHERE session_id = ?""",
                (json.dumps(raw, ensure_ascii=False), session_id),
            )
            conn.commit()
            item = preview.items[idx]
            field_name = next(iter(item.changes), "value")
            return {
                "text": t("batch.editing_item").format(project_id=item.project_id, field_name=field_name),
                "session_id": session_id,
                "project_id": item.project_id,
                "object_links": [{"object_type": "telegram_session", "object_ref": session_id}],
            }
        return {"text": t("batch.item_not_editable")}

    return {"text": t("batch.unknown_action").format(action=action)}
