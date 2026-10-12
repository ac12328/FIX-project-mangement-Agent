"""Handlers for Draft Card (C/D) and Inbound Reply Card (L)."""

from __future__ import annotations

from src.telegram.operator_messages import draft_receipt, en

import sqlite3
from collections.abc import Callable

from config.defaults import EMAIL_BODY_SNIPPET_CHARS
from config.settings import CONFIG
from src.email.sending import email_config_dict, imap_config_dict
from src.events.bus import Event, event_bus
from src.telegram.i18n import t
from src.telegram.draft_card_binding import (
    _draft_was_edited,
    _resolve_card_draft,
    _resolve_style_pref_draft,
)
from src.telegram.session import create_session, set_session_data


def _format_preview_response(preview_data: dict) -> dict:
    """Convert raw preview data into Telegram-ready response with CP card markup."""
    from src.telegram.cards import build_card
    from src.telegram.markup import card_to_markup

    subject = preview_data["preview_subject"]
    to_email = preview_data["preview_to"]
    cc = preview_data.get("preview_cc", "")
    body = preview_data["preview_body"]
    project_id = preview_data["project_id"]
    draft_id = preview_data["draft_id"]

    # Build preview text (P2-8: 繁中 labels for the card the PM reviews pre-send)
    lines = [
        "Draft preview" if en() else "草稿預覽",
        f"{'To' if en() else '收件人'}：{to_email}",
    ]
    lines.insert(1, "Not sent. Check recipients, subject and contents before confirming delivery." if en() else "尚未寄出；請核對收件人、主旨及內容後再確認寄送。")
    if cc:
        lines.append(f"{'CC' if en() else '副本'}：{cc}")
    lines.append(f"{'Subject' if en() else '主旨'}：{subject}")
    lines.append("")
    # This is the explicit full preview. Delivery chunks long text and puts
    # confirmation controls on the final chunk; never hide text being approved.
    lines.append(body)

    # Build CP card with project_id so callbacks route correctly
    card = build_card("CP", "", "", project_id=project_id)

    return {
        "text": "\n".join(lines),
        "reply_markup": card_to_markup(card),
        "project_id": project_id,
        "draft_id": draft_id,
    }


def _execute_send(
    conn: sqlite3.Connection,
    draft_id: str,
    project_id: str | None,
    draft: dict,
    operator_id: str = "default_operator",
    scope_start_changes: int | None = None,
) -> dict:
    """Commit approval with its delivery intent before attempting provider I/O."""
    from src.email.draft_session import get_draft
    from src.governance.four_eyes import prepare_client_send
    from src.repositories.activity_log_repo import insert_activity
    from src.services.draft_approval_command import (
        commit_immediate_draft_approval,
        dispatch_committed_draft_approval,
    )

    blocked = prepare_client_send(conn, draft, operator_id)
    if blocked:
        return {"text": blocked, "project_id": project_id, "object_ref": draft_id}

    # A caller may already own a transaction. A total_changes snapshot taken
    # here would silently certify its pending writes and commit them with the
    # draft. Production supplies the marker at the *opening* of its write
    # scope; bare connections are allowed only when no transaction is active.
    if scope_start_changes is None:
        if conn.in_transaction:
            return {
                "_error": True,
                "text": f"Draft not queued: write scope ownership unavailable. Reference: {draft_id}",
                "project_id": project_id,
                "object_ref": draft_id,
            }
        scope_start_changes = conn.total_changes

    email_config = email_config_dict(CONFIG)
    receipt = commit_immediate_draft_approval(
        conn, draft_id, actor_user_id=operator_id,
        email_config=email_config, scope_start_changes=scope_start_changes,
    )
    receipt = dispatch_committed_draft_approval(
        conn, receipt, email_config=email_config,
        imap_config=imap_config_dict(CONFIG),
    )
    if receipt.state == "not_queued":
        return {
            "_error": True,
            "text": f"Draft not queued: {receipt.error}. Reference: {draft_id}",
            "project_id": project_id,
            "object_ref": draft_id,
        }

    if receipt.state == "sent":
        try:
            insert_activity(
                conn,
                actor=operator_id,
                action_type="draft_approved",
                project_id=project_id or draft.get("project_id"),
                surface="telegram",
                object_ref=draft_id,
                summary=f"Draft {draft_id} approved and sent",
            )
            conn.commit()
        except Exception:
            # Optional activity bookkeeping cannot overturn the durable send.
            conn.rollback()
        sent_draft = get_draft(conn, draft_id) or draft
        original, _sent = _draft_was_edited(sent_draft)
        result: dict = {
            "text": draft_receipt(sent_draft, sent=True),
            "project_id": project_id,
            "object_ref": draft_id,
        }
        if original is not None:
            from src.telegram.cards import build_card
            from src.telegram.markup import card_to_markup
            pref_card = build_card(
                "PREF", "Style Preference", "",
                extra_buttons=[[
                    ("YES", f"PREF:YES:{draft_id}:{project_id}"),
                    ("NO", f"PREF:NO:{draft_id}:{project_id}"),
                ]],
            )
            result["text"] += t("draft.save_style_prompt")
            result["reply_markup"] = card_to_markup(pref_card)
    elif receipt.state in {"queued", "in_progress"}:
        result = {
            "text": f"Draft approved; delivery {receipt.state}. Do not confirm again. Reference: {draft_id}",
            "project_id": project_id,
            "object_ref": draft_id,
        }
    else:
        result = {
            "_error": True,
            "text": draft_receipt(
                get_draft(conn, draft_id) or draft, sent=False,
                error=receipt.error, uncertain=receipt.state == "uncertain",
            ),
            "project_id": project_id,
            "object_ref": draft_id,
        }
    try:
        event_bus.publish(Event(
            event_type="draft_approved",
            project_id=project_id,
            detail={"draft_id": draft_id, "sent": receipt.state == "sent"},
            surface="telegram",
        ))
    except Exception:
        pass  # The durable approval/delivery receipt remains authoritative.
    return result


def handle_draft(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
    scope_start_changes: int | None = None,
) -> dict:
    """Handle Draft Card (C/D) actions."""
    from src.email.draft_session import update_draft_status

    if action == "DETAILS":
        from src.email.draft_session import get_draft
        draft, pid = _resolve_card_draft(conn, project_id, message_context)
        if not draft:
            return {"_error": True, "text": t("draft.no_pending"), "project_id": project_id}
        draft_id = draft["draft_id"]
        full = get_draft(conn, draft_id) or draft
        preview_data = {
            "action": "preview",
            "draft_id": draft_id,
            "project_id": pid or project_id,
            "preview_subject": full["subject"],
            "preview_body": full["current_draft_text"],
            "preview_to": full["to_email"],
            "preview_cc": full.get("cc") or "",
            "card_profile": "CP",
        }
        return _format_preview_response(preview_data)

    if action == "SEND":
        draft, pid = _resolve_card_draft(conn, project_id, message_context)
        if not draft:
            return {"_error": True, "text": t("draft.no_pending"), "project_id": project_id}

        draft_id = draft["draft_id"]
        if CONFIG.preview_before_send:
            if draft["status"] == "created":
                update_draft_status(conn, draft_id, "in_review")
            # A3: Stop at in_review and return preview for PM confirmation
            from src.email.draft_session import get_draft
            full = get_draft(conn, draft_id) or draft
            preview_data = {
                "action": "preview",
                "draft_id": draft_id,
                "project_id": pid or project_id,
                "preview_subject": full["subject"],
                "preview_body": full["current_draft_text"],
                "preview_to": full["to_email"],
                "preview_cc": full.get("cc") or "",
                "card_profile": "CP",
            }
            return _format_preview_response(preview_data)

        # Legacy path: send immediately when preview disabled
        return _execute_send(
            conn, draft_id, pid or project_id, draft, operator_id=operator_id,
            scope_start_changes=scope_start_changes,
        )

    if action == "CONFIRM_SEND":
        # A4: PM confirmed send from the CP preview card
        draft, pid = _resolve_card_draft(conn, project_id, message_context)
        if not draft:
            return {"_error": True, "text": t("draft.no_pending"), "project_id": project_id}

        draft_id = draft["draft_id"]
        return _execute_send(
            conn, draft_id, pid or project_id, draft, operator_id=operator_id,
            scope_start_changes=scope_start_changes,
        )

    if action == "EDIT":
        from src.governance.work_claims import ClaimConflictError, claim_draft

        draft, pid = _resolve_card_draft(conn, project_id, message_context)
        if not draft:
            return {"_error": True, "text": t("draft.no_pending"), "project_id": project_id}
        claim_ref = draft["draft_id"]
        try:
            claim_draft(conn, claim_ref, operator_id)
        except ClaimConflictError as exc:
            return {
                "text": f"Draft is being edited by {exc.claimed_by}.",
                "project_id": pid,
                "object_ref": claim_ref,
            }
        session_id = create_session(
            conn, "draft_edit", originating_ref=claim_ref, operator_id=operator_id,
        )
        set_session_data(conn, session_id, {"project_id": pid})
        return {
            "text": t("draft.edit_mode"),
            "project_id": pid,
            "session_id": session_id,
            "object_links": [
                {"object_type": "telegram_session", "object_ref": session_id, "project_id": pid},
            ],
        }

    if action == "DISCARD":
        draft, pid = _resolve_card_draft(conn, project_id, message_context)
        if not draft:
            return {"_error": True, "text": t("draft.no_pending_discard"), "project_id": project_id}
        draft_id = draft["draft_id"]
        # Single path: domain activity + status transition (not transport-only log)
        from src.services.draft_service import draft_discard

        draft_discard(conn, draft_id, actor="operator", surface="telegram")
        return {
            "text": t("draft.discarded").format(draft_id=draft_id),
            "project_id": pid or project_id,
            "object_ref": draft_id,
        }

    return {"text": t("draft.generic_action").format(action=action), "project_id": project_id}


def handle_style_pref(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
) -> dict:
    """Handle style preference save callbacks (PREF:YES/NO:{draft_id}:{project_id})."""
    if action not in ("YES", "NO"):
        actual_project_id = (project_id or "").partition(":")[2] or project_id
        return {"text": t("draft.style_pref_unknown_action"), "project_id": actual_project_id}

    draft, actual_project_id = _resolve_style_pref_draft(
        conn, project_id, message_context,
    )
    if draft is None:
        return {
            "_error": True, "text": "Style preference card unavailable. No action was taken.",
            "project_id": actual_project_id,
        }
    if action == "NO":
        return {"text": t("draft.style_pref_not_saved"), "project_id": actual_project_id}

    try:
        from src.repositories.contact_repo import find_contact_by_email
        from src.repositories.response_pattern_repo import add_positive_signal

        proj = conn.execute(
            "SELECT client_email FROM projects WHERE project_id = ?",
            (actual_project_id,),
        ).fetchone()
        contact_id = None
        if proj and proj["client_email"]:
            contact = find_contact_by_email(conn, proj["client_email"])
            contact_id = contact["contact_id"] if contact else None

        if not contact_id:
            return {
                "text": t("draft.style_pref_no_contact"),
                "project_id": actual_project_id,
            }

        sent_body = (draft.get("current_draft_text") or "")[:250]
        signal = f"operator_style_approved:{sent_body}"
        add_positive_signal(conn, contact_id, signal, project_id=actual_project_id)
        return {
            "text": t("draft.style_pref_saved"),
            "project_id": actual_project_id,
        }
    except Exception as exc:  # pragma: no cover
        return {"_error": True, "text": t("draft.style_pref_error").format(exc=exc),
                "project_id": actual_project_id}


def handle_inbound_reply(
    conn: sqlite3.Connection,
    action: str,
    project_id: str | None,
    *,
    message_context: dict | None = None,
    operator_id: str = "default_operator",
    post_commit: list[Callable[[], None]] | None = None,
) -> dict:
    """Handle Inbound Reply Card (L)."""
    if action == "CONFIRM_UPDATE" and project_id:
        # project_id here is actually the session_id from the callback data
        return _handle_proposal_confirm(
            conn, project_id, operator_id, post_commit=post_commit,
        )

    if action == "EDIT" and project_id:
        # project_id here is actually the session_id from the callback data
        return _handle_proposal_edit(
            conn, project_id, operator_id, commit=post_commit is None,
        )

    if action == "VIEW_FULL_REPLY" and project_id:
        row = conn.execute(
            """SELECT summary, subject FROM project_events
               WHERE project_id = ? ORDER BY event_at DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        if row:
            d = dict(row)
            text = d.get("summary") or d.get("subject") or t("inbound.no_details")
            return {"text": t("inbound.latest_reply").format(project_id=project_id, text=text), "project_id": project_id}
        return {"text": t("inbound.no_reply_events").format(project_id=project_id), "project_id": project_id}

    if action == "UPDATE_PROJECT" and project_id:
        from src.db.savepoint import savepoint

        with savepoint(conn, ensure_transaction=True):
            session_id = create_session(
                conn,
                "inbound_update_project",
                originating_ref=project_id,
                operator_id=operator_id,
                commit=False,
            )
            if not set_session_data(
                conn, session_id, {"project_id": project_id}, commit=False,
            ):
                raise RuntimeError("Inbound update session could not be initialized")
        if post_commit is None:
            conn.commit()
        return {
            "text": t("inbound.describe_update").format(project_id=project_id),
            "project_id": project_id,
            "session_id": session_id,
            "object_ref": project_id,
            "object_links": [
                {"object_type": "telegram_session", "object_ref": session_id, "project_id": project_id},
            ],
        }

    if action == "DRAFT_REPLY" and project_id:
        from src.tools.executor import _handle_draft_reply
        try:
            result = _handle_draft_reply(conn, {
                "project_id": project_id,
                "context": "Inbound reply received",
            })
            if result.get("subject") and result.get("body") and not result.get("error"):
                try:
                    event_bus.publish(Event(
                        event_type="draft_reply_generated",
                        project_id=project_id,
                        surface="telegram",
                    ))
                except Exception:
                    import logging

                    logging.getLogger(__name__).exception(
                        "Committed inbound draft event publication failed"
                    )
                return {
                    "text": t("inbound.draft_reply_generated").format(project_id=project_id),
                    "project_id": project_id,
                }
            return {
                "text": t("inbound.draft_reply_failed").format(project_id=project_id, error=result.get('error') or 'empty draft returned'),
                "project_id": project_id,
            }
        except Exception as exc:
            return {
                "text": t("inbound.draft_reply_failed").format(project_id=project_id, error=str(exc)),
                "project_id": project_id,
            }

    if action in ("UNDO", "REJECT") and project_id:
        # For current cards the entity is the proposed update ID. Legacy project-ID
        # cards no longer have enough identity to safely choose an undo snapshot.
        from src.email.auto_apply_undo import revert_auto_apply
        result = revert_auto_apply(
            conn,
            project_id,
            reason="Operator undid auto-applied email update",
            post_commit=post_commit,
        )
        result_project_id = result.get("project_id") or project_id
        if not result.get("ok"):
            return {"text": t("inbound.undo_not_available"), "project_id": result_project_id}
        revert_text = result.get("revert_text") or ""
        return {
            "text": t("inbound.undo_applied").format(revert_text=revert_text),
            "project_id": result_project_id,
        }

    if action == "DISMISS":
        return {"text": t("inbound.dismissed"), "project_id": project_id}

    if action in ("📎_ATTACH_TO_TICKET", "📎 ATTACH TO TICKET", "ATTACH_TO_TICKET"):
        from src.telegram.handlers_ticket import handle_attach
        return handle_attach(conn, "PICK", project_id,
                             message_context=message_context,
                             operator_id=operator_id)

    return {"text": t("inbound.generic_action").format(action=action), "project_id": project_id}


def _handle_proposal_confirm(
    conn: sqlite3.Connection,
    session_id: str,
    operator_id: str,
    *,
    post_commit: list[Callable[[], None]] | None = None,
) -> dict:
    """Apply the proposed field changes from an email auto-proposal."""
    import json as _json
    import logging as _logging
    from src.telegram.session import complete_session, get_session
    from src.write_layer.write_helpers import safe_write_project

    _log = _logging.getLogger(__name__)
    session = get_session(conn, session_id)
    if not session or session["status"] != "active":
        return {"text": t("inbound.proposal_expired")}

    try:
        data = _json.loads(session.get("intermediate_data") or "{}")
    except _json.JSONDecodeError:
        data = {}

    project_id = data.get("project_id")
    proposed_fields = data.get("proposed_fields", {})
    reply_id = data.get("reply_id")

    if not project_id or not proposed_fields:
        from src.db.savepoint import savepoint

        with savepoint(conn, ensure_transaction=True):
            complete_session(conn, session_id, commit=False)
        if post_commit is None:
            conn.commit()
        return {"text": t("inbound.no_proposal_data"), "project_id": project_id}

    from src.governance.four_eyes import (
        gate_high_risk_write,
        is_bound_actor,
        is_high_risk_project_fields,
        team_four_eyes_enabled,
    )
    from src.telegram.session import set_session_data

    if team_four_eyes_enabled():
        high_risk, _reason = is_high_risk_project_fields(
            conn, project_id, proposed_fields
        )
        if high_risk:
            proposed_by = data.get("proposed_by") or session.get("operator_id")
            if not is_bound_actor(str(proposed_by or ""), conn=conn):
                data["proposed_by"] = operator_id
                from src.db.savepoint import savepoint

                with savepoint(conn, ensure_transaction=True):
                    if not set_session_data(conn, session_id, data, commit=False):
                        raise RuntimeError("Email proposal session changed during review")
                if post_commit is None:
                    conn.commit()
                return {
                    "text": (
                        "High-risk email proposal recorded. Another Editor "
                        "must confirm this change."
                    ),
                    "project_id": project_id,
                    "session_id": session_id,
                }
            blocked = gate_high_risk_write(
                conn, project_id, proposed_fields, str(proposed_by), operator_id
            )
            if blocked:
                return {
                    "text": blocked,
                    "project_id": project_id,
                    "session_id": session_id,
                }

    from src.db.savepoint import savepoint

    class _DeclinedWrite(Exception):
        """Discard partial writer effects but still resolve the rejected session."""

    with savepoint(conn, ensure_transaction=True):
        try:
            with savepoint(conn):
                result = safe_write_project(
                    conn,
                    project_id,
                    proposed_fields,
                    actor=operator_id,
                    evidence_ref=reply_id,
                    operator_confirmed=True,
                    reason="Operator confirmed email auto-proposal",
                    commit=False,
                    publish_event=False,
                    schedule_notifications=False,
                    rollback_on_error=False,
                )
                if result.outcome not in {"accepted", "no_change"}:
                    raise _DeclinedWrite()
        except _DeclinedWrite:
            pass
        if not complete_session(conn, session_id, commit=False):
            raise RuntimeError("Email proposal session changed during confirmation")

    if post_commit is None:
        conn.commit()
    if result.outcome == "accepted":
        def after_commit() -> None:
            import logging

            log = logging.getLogger(__name__)
            try:
                event_bus.publish(Event(
                    event_type="project_updated",
                    project_id=project_id,
                    detail={"fields": list(result.applied_fields)},
                    surface="telegram",
                ))
            except Exception:
                log.exception("Committed email proposal event publication failed")
            if result.applied_fields.get("current_milestone") == "M8_GoLive":
                try:
                    from src.services.go_live_notice import schedule_go_live_telegram

                    schedule_go_live_telegram(project_id)
                except Exception:
                    log.exception("Committed email proposal go-live wakeup failed")

        if post_commit is None:
            after_commit()
        else:
            post_commit.append(after_commit)
        parts = [f"{k}={v}" for k, v in (result.applied_fields or {}).items()]
        return {
            "text": t("inbound.proposal_updated").format(project_id=project_id, parts=', '.join(parts)),
            "project_id": project_id,
        }
    if result.outcome == "no_change":
        return {"text": t("inbound.proposal_no_change").format(project_id=project_id), "project_id": project_id}
    return {
        "text": t("inbound.proposal_rejected").format(project_id=project_id, reason=result.rejection_reason or 'unknown'),
        "project_id": project_id,
    }


def _handle_proposal_edit(
    conn: sqlite3.Connection,
    session_id: str,
    operator_id: str,
    *,
    commit: bool = True,
) -> dict:
    """Open an NLP session for the operator to describe edits to the proposal."""
    import json as _json
    from src.telegram.session import get_session

    session = get_session(conn, session_id)
    if not session or session["status"] != "active":
        return {"text": t("inbound.proposal_expired")}

    try:
        data = _json.loads(session.get("intermediate_data") or "{}")
    except _json.JSONDecodeError:
        data = {}

    project_id = data.get("project_id")

    # Transition the session to inbound_update_project so the operator
    # can type free-text instructions for the agent loop
    from src.db.savepoint import savepoint

    with savepoint(conn, ensure_transaction=True):
        conn.execute(
            "UPDATE telegram_sessions SET flow_type = 'inbound_update_project' "
            "WHERE session_id = ? AND status = 'active'",
            (session_id,),
        )
    if commit:
        conn.commit()

    return {
        "text": t("inbound.describe_update_agent").format(project_id=project_id),
        "project_id": project_id,
        "session_id": session_id,
        "object_links": [
            {"object_type": "telegram_session", "object_ref": session_id, "project_id": project_id},
        ],
    }
