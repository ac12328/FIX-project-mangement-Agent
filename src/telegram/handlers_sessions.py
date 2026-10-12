"""
SessionsMixin: handles callback queries and active session text flows.
Mixed into TelegramBot in bot.py.
"""

from __future__ import annotations

import json
import sqlite3

from telegram import Update

from src.portfolio.followup_tracker import reassign_followup
from src.repositories.activity_log_repo import insert_activity
from src.repositories.contact_repo import find_internal_contacts_by_expertise
from src.telegram.bot_helpers import (
    _build_query_clarification_card,
    _llm_parse_project_query,
    _query_session_payload,
)
from src.db.connection import scoped_write
from src.repositories.operator_state_repo import record_telegram_interaction
from src.telegram.callbacks import handle_callback
from src.telegram.cards import format_card_text
from src.telegram.i18n import t
from src.telegram.markup import card_to_markup


def _lq_callback_denial(conn, parsed, context, operator_id: str) -> str | None:
    """Bind session-backed callbacks to their persisted card and actor."""
    from src.telegram._cb_helpers import extract_link, extract_message_metadata
    from src.telegram.operator_binding import fallback_operator_id
    from src.telegram.session import get_session

    unavailable = "Card session unavailable. No action was taken."
    entity_id = parsed.entity_id
    if parsed.profile == "L":
        if parsed.action not in {"CONFIRM_UPDATE", "EDIT"}:
            return None
        link = extract_link(context, "telegram_session")
        if not entity_id or not link or link.get("object_ref") != entity_id:
            return unavailable
        session = get_session(conn, entity_id)
        if not session or session["status"] != "active" or (
            session["flow_type"] != "email_proposal"
            or session["operator_id"] != operator_id
        ):
            return unavailable
        try:
            data = json.loads(session.get("intermediate_data") or "{}")
        except (TypeError, ValueError):
            return unavailable
        project_id = data.get("project_id") if isinstance(data, dict) else None
        if not project_id or context.get("project_id") != project_id or (
            link.get("project_id") != project_id
        ):
            return unavailable
    elif parsed.profile == "Q":
        metadata = extract_message_metadata(context)
        if not entity_id or metadata.get("card_profile") != "Q" or (
            context.get("project_id") != entity_id
            or metadata.get("project_id") != entity_id
        ):
            return unavailable
        session_id = metadata.get("session_id")
        session = get_session(conn, session_id) if isinstance(session_id, str) else None
        if not session or session["status"] != "active" or (
            session["flow_type"] != "commitment_confirm"
            or session["originating_ref"] != entity_id
            or session["operator_id"] not in {operator_id, fallback_operator_id()}
        ):
            return unavailable
        try:
            data = json.loads(session.get("intermediate_data") or "{}")
        except (TypeError, ValueError):
            return unavailable
        if not isinstance(data, dict) or data.get("project_id") != entity_id:
            return unavailable
    return None


class SessionsMixin:
    # ---------------------------------------------------------------------------
    # Interface stubs — implemented by TelegramBot / sibling mixins
    # ---------------------------------------------------------------------------
    conn: object = None  # sqlite3.Connection, provided by TelegramBot at runtime

    def _auth(self, update: Update) -> bool:  # pragma: no cover
        raise NotImplementedError

    def _build_progress_streamer(self, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def _safe_send(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    # ---------------------------------------------------------------------------

    async def _on_callback(self, update: Update, context) -> None:
        import logging as _logging
        _log = _logging.getLogger(__name__)
        if not self._auth(update):
            return

        from src.telegram.card_types import executing_state, resolved_state, errored_state

        from src.telegram.callback_protocol import parse_callback_data

        from src.auth.rbac import permissions_for_role
        from src.auth.repository import get_user
        from src.telegram._cb_helpers import extract_link
        from src.telegram.callback_authorization import permission_for_callback
        from src.telegram.session import get_session

        cb_data = update.callback_query.data or ""
        # Unsupported secret-reveal callbacks fail closed before card editing.
        secret_reveal = cb_data.startswith("fxr:")
        parsed = parse_callback_data(cb_data)
        required_permission = None if secret_reveal else permission_for_callback(parsed)
        legacy_cu_card = (
            parsed.profile == "CU"
            and parsed.action in {"CONFIRM", "CANCEL"}
            and not (parsed.entity_id or "").startswith("@")
        )
        operator_id = "default_operator"
        actor_bound = False
        denial_reason = (
            "Secret reveal is unavailable. No operation was executed."
            if secret_reveal else (
                "This legacy update card is not bound to an operation. No change was made. "
                "Request a fresh operation-bound card or use an authorized text command."
                if legacy_cu_card else None
            )
        )
        try:
            from src.telegram.operator_binding import (
                resolve_telegram_operator,
                telegram_user_id_from_update,
            )

            telegram_user_id = telegram_user_id_from_update(update)
            with scoped_write() as wconn:
                record_telegram_interaction(wconn)
                actor = resolve_telegram_operator(wconn, telegram_user_id)
                operator_id = actor.operator_id
                actor_bound = actor.bound
                if actor_bound and not secret_reveal and not legacy_cu_card:
                    user = get_user(wconn, operator_id)
                    if required_permission is None:
                        denial_reason = "Unknown callback action. No operation was executed."
                    elif not user or user["status"] != "active" or (
                        required_permission not in permissions_for_role(user["role"])
                    ):
                        denial_reason = (
                            f"Operation not executed. Permission required: {required_permission}."
                        )
                    elif parsed.profile == "CU" and (
                        parsed.entity_id or ""
                    ).startswith("@"):
                        from src.telegram.handlers_staged import bound_staged_operation_project

                        message = update.callback_query.message
                        chat = update.effective_chat
                        project = bound_staged_operation_project(
                            wconn, parsed.entity_id[1:],
                            chat_id=chat.id if chat is not None else None,
                            telegram_message_id=(
                                message.message_id if message is not None else None
                            ),
                        )
                        if project is None:
                            denial_reason = (
                                "This staged update card is not bound to this message. "
                                "No operation was executed."
                            )
                        else:
                            # Deny before editing the shared card; preserve second-person approval.
                            from src.governance.four_eyes import (
                                gate_high_risk_write,
                                is_high_risk_project_fields,
                                team_four_eyes_enabled,
                            )
                            from src.repositories.pending_operation_repo import get_operation

                            operation = get_operation(wconn, parsed.entity_id[1:])
                            if operation is None or str(operation.chat_id) != str(chat.id):
                                denial_reason = "Staged update unavailable. No operation was executed."
                            elif parsed.action == "CONFIRM" and team_four_eyes_enabled() and (
                                is_high_risk_project_fields(
                                    wconn, project, (operation.payload or {}).get("fields") or {},
                                )[0]
                            ):
                                blocked = gate_high_risk_write(
                                    wconn, project, (operation.payload or {}).get("fields") or {},
                                    operation.operator_id, operator_id,
                                )
                                if blocked:
                                    denial_reason = f"Operation not executed. {blocked}"
                            elif operation.operator_id != operator_id:
                                denial_reason = (
                                    "This pending update belongs to another conversation. "
                                    "No operation was executed."
                                )
                    elif parsed.profile in {"K", "BATCH"}:
                        # Match handler session IDs before editing a shared card.
                        if parsed.profile == "K":
                            from src.telegram.persistence import find_message_context

                            message = update.callback_query.message
                            chat = update.effective_chat
                            context_data = (
                                find_message_context(
                                    wconn,
                                    chat_id=chat.id,
                                    telegram_message_id=message.message_id,
                                ) if chat is not None and message is not None else None
                            )
                            link = extract_link(context_data, "telegram_session")
                            session_id = link.get("object_ref") if link else None
                        else:
                            session_id = (parsed.entity_id or "").partition(":")[0]
                        session = get_session(wconn, session_id) if session_id else None
                        if not session or session["status"] != "active" or (
                            session["operator_id"] != operator_id
                        ):
                            denial_reason = "Session unavailable. No action was taken."
                    elif parsed.profile == "E":
                        from src.telegram.handlers_project import identity_callback_denial
                        from src.telegram.persistence import find_message_context

                        message = update.callback_query.message
                        chat = update.effective_chat
                        context_data = (
                            find_message_context(
                                wconn, chat_id=chat.id,
                                telegram_message_id=message.message_id,
                            ) if chat is not None and message is not None else None
                        )
                        denial_reason = identity_callback_denial(
                            wconn, parsed.entity_id, context_data,
                        )
                    elif parsed.profile == "Q" or (
                        parsed.profile == "L"
                        and parsed.action in {"CONFIRM_UPDATE", "EDIT"}
                    ):
                        from src.telegram.persistence import find_message_context

                        message = update.callback_query.message
                        chat = update.effective_chat
                        context_data = (
                            find_message_context(
                                wconn, chat_id=chat.id,
                                telegram_message_id=message.message_id,
                            ) if chat is not None and message is not None else None
                        )
                        denial_reason = _lq_callback_denial(
                            wconn, parsed, context_data, operator_id,
                        )
                    elif parsed.profile == "P" and parsed.action in {
                        "ESCALATE", "RECLASS_SELECT",
                    }:
                        from src.telegram.persistence import find_message_context
                        from src.telegram.support_callback_command import support_callback_denial

                        message = update.callback_query.message
                        chat = update.effective_chat
                        context_data = (
                            find_message_context(
                                wconn, chat_id=chat.id,
                                telegram_message_id=message.message_id,
                            ) if chat is not None and message is not None else None
                        )
                        denial_reason = support_callback_denial(
                            wconn, parsed.action, parsed.entity_id, context_data, operator_id,
                        )
                    elif parsed.profile == "PREF" and parsed.action in {"YES", "NO"}:
                        from src.telegram.handlers_draft import _resolve_style_pref_draft
                        from src.telegram.persistence import find_message_context

                        message = update.callback_query.message
                        chat = update.effective_chat
                        persisted_context = (
                            find_message_context(
                                wconn, chat_id=chat.id,
                                telegram_message_id=message.message_id,
                            ) if chat is not None and message is not None else None
                        )
                        draft, _ = _resolve_style_pref_draft(
                            wconn, parsed.entity_id, persisted_context,
                        )
                        if draft is None:
                            denial_reason = "Style preference card unavailable. No action was taken."
                    elif parsed.profile in {"C", "D", "CP"}:
                        # A callback must be tied to this persisted card before
                        # changing its visible state, even for a preview/read action.
                        from src.telegram.handlers_draft import _resolve_card_draft
                        from src.telegram.persistence import find_message_context

                        message = update.callback_query.message
                        chat = update.effective_chat
                        context_data = (
                            find_message_context(
                                wconn, chat_id=chat.id,
                                telegram_message_id=message.message_id,
                            ) if chat is not None and message is not None else None
                        )
                        draft, _ = _resolve_card_draft(
                            wconn, parsed.entity_id, context_data,
                        )
                        if draft is None:
                            denial_reason = "Draft card unavailable. No action was taken."
        except Exception:
            _log.exception("Callback pre-flight persistence failed")
            denial_reason = "Operation not executed. Callback pre-flight failed."

        # Never let a fallback read namespace authorize a card mutation. Keep
        # the owner's card intact when an unbound user clicks it.
        if not actor_bound or denial_reason:
            try:
                await update.callback_query.answer(
                    denial_reason or "未有執行任何操作。請先綁定有效 Telegram 帳戶。",
                    show_alert=True,
                )
            except Exception:
                _log.debug("Callback denial alert failed", exc_info=True)
            return

        try:
            await update.callback_query.answer()
        except Exception:
            _log.debug("callback_query.answer() failed", exc_info=True)

        message = update.callback_query.message
        original_text = (message.text or "") if message else ""

        cb_data = update.callback_query.data or ""
        # D4: Check if this action needs two-click confirmation
        from src.telegram.card_dispatcher import _CONFIRM_REQUIRED
        cb_parts = cb_data.split(":", 2)
        cb_profile = cb_parts[0] if cb_parts else ""
        cb_action = cb_parts[1] if len(cb_parts) > 1 else ""
        cb_entity = cb_parts[2] if len(cb_parts) > 2 else ""
        if (cb_profile, cb_action) in _CONFIRM_REQUIRED and message:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            confirm_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(t("card.confirm_button").format(action=cb_action), callback_data=f"{cb_profile}:CONFIRMED_{cb_action}:{cb_entity}"),
                InlineKeyboardButton(t("card.cancel_button"), callback_data=f"{cb_profile}:CANCEL:{cb_entity}"),
            ]])
            try:
                await message.edit_text(
                    original_text + "\n\n" + t("card.confirm_prompt").format(action=cb_action),
                    reply_markup=confirm_markup,
                )
            except Exception:
                _log.debug("D4 confirmation edit failed", exc_info=True)
            return

        # Phase 1: instant visual feedback — edit card to executing state
        if message:
            try:
                await message.edit_text(
                    executing_state(original_text),
                    parse_mode="Markdown",
                    reply_markup=None,
                )
            except Exception:
                _log.debug("Phase 1 edit_text failed", exc_info=True)

        # Phase 2: do the actual work
        response: dict | None = None
        handler_error: bool = False
        scope_committed = False
        post_commit = []
        try:
            # Draft generation may call an LLM and the draft tool owns its
            # short persistence scope. Never hold the shared writer during it.
            draft_generation = (
                (cb_profile == "L" and cb_action == "DRAFT_REPLY")
                or (cb_profile == "G" and cb_action in {"DRAFT_EMAIL", "DRAFT EMAIL"})
                or (cb_profile == "M" and cb_action in {"DRAFT_STATUS_EMAIL", "DRAFT STATUS EMAIL"})
            )
            db_scope = self.scoped_db_read() if draft_generation else self.scoped_db_write()
            with db_scope as dbconn:
                # Capture at entry, before this callback can make any writes.
                # An injected/test connection may already have caller-owned
                # writes; only the production serializer guarantees a fresh
                # transaction at this point. Bare clean connections use the
                # handler's no-active-transaction fallback instead.
                draft_scope_start_changes = (
                    dbconn.total_changes
                    if cb_profile in {"C", "D", "CP"}
                    and cb_action in {"SEND", "CONFIRM_SEND", "CONFIRMED_SEND"}
                    and isinstance(dbconn, sqlite3.Connection)
                    and getattr(self, "_conn_override", None) is None
                    else None
                )
                response = handle_callback(
                    dbconn,
                    update.callback_query.data,
                    chat_id=update.effective_chat.id if update.effective_chat else None,
                    telegram_message_id=message.message_id if message else None,
                    operator_id=operator_id,
                    post_commit=post_commit,
                    owned_draft_scope=draft_generation,
                    draft_scope_start_changes=draft_scope_start_changes,
                    # Audit through a separate short write after draft creation.
                    audit_write_scope=self.scoped_db_write if draft_generation else None,
                )
                handler_error = bool((response or {}).get("_error"))
            scope_committed = True
        except Exception:
            _log.exception("Callback handler error: %s", update.callback_query.data)
            handler_error = True
            if cb_profile == "CU":
                # The writer may have committed before acknowledgement failed.
                # Do not describe the business command as rolled back or invite
                # another click when the actual outcome cannot be proved here.
                response = {
                    "_error": True,
                    "_unconfirmed": True,
                    "text": t("card.outcome_unconfirmed").format(
                        operation_id=cb_entity.lstrip("@"),
                    ),
                }

        # A committed supersession can return an error card while still needing
        # its deferred event. A failed write scope must publish nothing.
        if scope_committed:
            for effect in post_commit:
                try:
                    effect()
                except Exception:
                    _log.exception("Post-commit callback effect failed: %s", update.callback_query.data)

        # Phase 3: update card to final state
        if message:
            try:
                if handler_error:
                    if cb_profile == "CU" and response and response.get("_unconfirmed"):
                        error_card = original_text.rstrip() + str(response["text"])
                    else:
                        error_card = errored_state(original_text)
                        if cb_profile == "CU" and response and response.get("text"):
                            error_card += "\n\n" + str(response["text"]).replace("`", "'")
                    await message.edit_text(
                        error_card,
                        parse_mode=(
                            None if cb_profile == "CU" and response
                            and response.get("_unconfirmed") else "Markdown"
                        ),
                        reply_markup=None,
                    )
                else:
                    result_text = (response or {}).get("text", t("session.done"))
                    follow_up_markup = (response or {}).get("reply_markup")
                    safe_result = result_text.replace("`", "'")
                    await message.edit_text(
                        resolved_state(original_text, safe_result),
                        parse_mode="Markdown",
                        reply_markup=follow_up_markup,
                    )
            except Exception:
                _log.debug("Phase 3 edit_text failed — falling back to _safe_send", exc_info=True)
                # Fallback: send new message if edit fails (message too old, content unchanged, etc.)
                if response and not handler_error:
                    try:
                        await self._safe_send(
                            update.effective_chat.id,
                            response.get("text", t("session.done")),
                            reply_to_message_id=message.message_id if message else None,
                            project_id=response.get("project_id"),
                            session_id=response.get("session_id"),
                            message_kind=response.get("message_kind", "callback_reply"),
                            object_links=response.get("object_links"),
                            reply_markup=response.get("reply_markup"),
                        )
                    except Exception:
                        _log.exception("Callback fallback _safe_send also failed")

    async def _handle_active_session(self, update: Update, user_text: str) -> bool:
        from src.repositories.project_repo import project_exists
        from src.telegram.session import complete_session, get_active_session, set_session_data, touch_session

        # Production bots intentionally keep no long-lived SQLite connection.
        # Load and touch the session in one short write scope, then reopen brief
        # scopes for mutations below so no DB lock is held across Telegram/LLM I/O.
        from src.telegram.operator_binding import (
            resolve_telegram_operator,
            telegram_user_id_from_update,
        )

        with self.scoped_db_write() as session_conn:
            actor = resolve_telegram_operator(
                session_conn, telegram_user_id_from_update(update)
            )
            session = get_active_session(session_conn, actor.operator_id)
            if not session:
                return False
            if not touch_session(session_conn, session["session_id"]):
                return False
            session = dict(session)
        try:
            data = json.loads(session["intermediate_data"]) if session.get("intermediate_data") else {}
        except json.JSONDecodeError:
            data = {}

        flow_type = session["flow_type"]
        project_id = data.get("project_id")

        if flow_type == "query_modify":
            from src.portfolio.aggregate_query import (
                evaluate_aggregate_query,
                format_query_result_text,
            )
            from src.telegram.session import cancel_session

            if user_text.strip().lower() in {"cancel", "/cancel", "stop"}:
                with self.scoped_db_write() as session_conn:
                    cancel_session(session_conn, session["session_id"])
                await self._safe_reply(
                    update,
                    t("session.query_cancelled"),
                    session_id=session["session_id"],
                )
                return True

            with self.scoped_db_read() as session_conn:
                evaluation = evaluate_aggregate_query(
                    session_conn,
                    user_text,
                    llm_parse_fn=_llm_parse_project_query,
                )
            with self.scoped_db_write() as session_conn:
                set_session_data(session_conn, session["session_id"], _query_session_payload(evaluation))
                if evaluation.status == "ready":
                    complete_session(session_conn, session["session_id"])
            if evaluation.status == "ready":
                await self._safe_reply(
                    update,
                    format_query_result_text(evaluation),
                    session_id=session["session_id"],
                    message_kind="command_reply",
                )
                return True

            card = _build_query_clarification_card(evaluation)
            await self._safe_send(
                update.effective_chat.id,
                format_card_text(card),
                reply_to_message_id=update.message.message_id if update.message else None,
                session_id=session["session_id"],
                message_kind="card",
                object_links=[{"object_type": "telegram_session", "object_ref": session["session_id"]}],
                reply_markup=card_to_markup(card),
            )
            return True

        if flow_type in {"review_edit", "inbound_update_project"}:
            target_project = project_id or session.get("originating_ref")
            if not target_project:
                with self.scoped_db_write() as session_conn:
                    complete_session(session_conn, session["session_id"])
                await self._safe_reply(update, t("session.no_project_context"), session_id=session["session_id"])
                return True

            streamer = self._build_progress_streamer(
                chat_id=update.effective_chat.id if update.effective_chat else None,
                reply_to_message_id=update.message.message_id if update.message else None,
                project_id=target_project,
                session_id=session["session_id"],
            )
            progress_cb = self._build_progress_callback(streamer)
            try:
                if streamer:
                    await streamer.start_step(t("progress.receive_session_input"), f"Received session input for {target_project}")
                    await streamer.complete_step(t("progress.receive_session_input"))
                    await streamer.start_step(t("progress.load_context"), "Loading project context and pending review state")
                    await streamer.complete_step(t("progress.load_context"))
                    await streamer.start_step(t("progress.run_write_layer"), "Evaluating the requested change")
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).debug("Session streamer pre-reasoning failed", exc_info=True)
                streamer = None
            instruction = f"Update project {target_project} based on this evidence: {user_text}"
            result = await self._run_agent_loop(
                instruction,
                chat_id=update.effective_chat.id if update.effective_chat else 0,
                progress_cb=progress_cb,
            )
            with self.scoped_db_write() as session_conn:
                complete_session(session_conn, session["session_id"])
            if flow_type == "review_edit":
                from src.project_state.observability import project_state_metrics

                project_state_metrics.record_review("edited")
            try:
                if streamer:
                    await streamer.complete_step(t("progress.run_write_layer"))
                    await streamer.start_step(t("progress.send_final_reply"), "Preparing final session reply")
            except Exception:
                import logging as _logging
                _logging.getLogger(__name__).debug("Session streamer post-reasoning failed", exc_info=True)
            if isinstance(result, str):
                await self._safe_reply(update, t("session.processed_result").format(project_id=target_project, result=result[:300]), project_id=target_project, session_id=session["session_id"])
            else:
                await self._safe_reply(update, t("session.processed_staged").format(project_id=target_project), project_id=target_project, session_id=session["session_id"])
            return True

        if flow_type == "draft_edit":
            draft_id = session.get("originating_ref")
            if draft_id:
                from src.email.draft_session import edit_draft
                with self.scoped_db_write() as session_conn:
                    ok = edit_draft(session_conn, draft_id, new_body=user_text)
                    complete_session(session_conn, session["session_id"])
                if ok:
                    await self._safe_reply(update, t("session.draft_updated").format(draft_id=draft_id), project_id=project_id, session_id=session["session_id"])
                else:
                    await self._safe_reply(update, t("session.draft_update_failed").format(draft_id=draft_id), project_id=project_id, session_id=session["session_id"])
            else:
                with self.scoped_db_write() as session_conn:
                    complete_session(session_conn, session["session_id"])
                await self._safe_reply(update, t("session.no_draft_found"), session_id=session["session_id"])
            return True

        if flow_type == "triage_assign":
            target_project = user_text.strip().upper()
            with self.scoped_db_read() as session_conn:
                target_exists = project_exists(session_conn, target_project)
            if not target_exists:
                await self._safe_reply(update, t("session.project_not_found").format(project_id=target_project), session_id=session["session_id"])
                return True
            from src.email.triage_conversation import TriageAssignmentScope, apply_frozen_assignment
            from src.telegram.operator_messages import en
            assigned_ids = []
            try:
                with self.scoped_db_write() as session_conn:
                    scope_data = data.get("triage_assignment_scope")
                    if scope_data:
                        scope = TriageAssignmentScope.model_validate(scope_data)
                        count = apply_frozen_assignment(session_conn, scope, target_project, actor.operator_id)
                        assigned_ids = scope.email_ids
                    elif session.get("originating_ref"):
                        # Legacy sessions retain their exact one-source scope,
                        # but cannot overwrite a source assigned elsewhere.
                        email_id = session["originating_ref"]
                        changed = session_conn.execute("""UPDATE processed_emails
                            SET project_id=?,result='project_update'
                            WHERE email_id=? AND project_id IS NULL AND ticket_id IS NULL""",
                            (target_project, email_id)).rowcount
                        if changed != 1:
                            raise ValueError("source assignment changed")
                        insert_activity(session_conn, actor=actor.operator_id, action_type="triage_assigned",
                            project_id=target_project, surface="telegram", object_ref=email_id,
                            summary=f"Email assigned to project {target_project}")
                        count, assigned_ids = 1, [email_id]
                    else:
                        raise ValueError("source assignment unavailable")
                    complete_session(session_conn, session["session_id"])
            except ValueError:
                await self._safe_reply(update,
                    "No assignment made. Sources changed; review the latest classification card."
                    if en() else "未有完成指派。郵件範圍已改變，請重新查看最新分類通知。",
                    session_id=session["session_id"])
                return True
            from src.events.bus import Event, event_bus
            event_bus.publish(Event(event_type="email_processed", project_id=target_project,
                detail={"email_ids": assigned_ids, "operation": "triage_assigned"}, surface="telegram"))
            receipt = (f"Assigned {count} saved email(s) to {target_project}. Later arrivals were not included."
                       if en() else f"已將 {count} 封已列出的郵件指派至 {target_project}；不包括之後收到的郵件。")
            await self._safe_reply(update, receipt, project_id=target_project, session_id=session["session_id"])
            return True

        if flow_type == "identity_merge":
            with self.scoped_db_write() as session_conn:
                insert_activity(
                    session_conn,
                    actor="operator",
                    action_type="identity_merge_requested",
                    surface="telegram",
                    object_ref=session.get("originating_ref"),
                    summary=f"Merge {session.get('originating_ref') or 'contact'} -> {user_text.strip()}",
                )
                complete_session(session_conn, session["session_id"])
            await self._safe_reply(update, t("session.merge_recorded").format(text=user_text.strip()), session_id=session["session_id"])
            return True

        if flow_type == "followup_reassign":
            query = user_text.strip()
            followup_id = session.get("originating_ref")
            with self.scoped_db_read() as session_conn:
                candidates = find_internal_contacts_by_expertise(session_conn, query, limit=5)

            if len(candidates) == 1:
                c = candidates[0]
                with self.scoped_db_write() as session_conn:
                    reassigned = reassign_followup(session_conn, followup_id, c["contact_id"], commit=False)
                    if reassigned:
                        insert_activity(
                            session_conn,
                            actor="operator",
                            action_type="followup_reassigned",
                            project_id=project_id,
                            surface="telegram",
                            object_ref=followup_id,
                            summary=f"Follow-up reassigned to {c['display_name']}",
                        )
                        complete_session(session_conn, session["session_id"])
                if not reassigned:
                    await self._safe_reply(update, t("followup.invalid_owner"), session_id=session["session_id"])
                    return True
                await self._safe_reply(
                    update,
                    t("session.followup_reassigned").format(display_name=c['display_name']),
                    project_id=project_id,
                    session_id=session["session_id"],
                )
            elif len(candidates) == 0:
                await self._safe_reply(
                    update,
                    t("session.no_contact_found").format(query=query),
                    project_id=project_id,
                )
                # session stays open
            else:
                names = ", ".join(c["display_name"] for c in candidates)
                await self._safe_reply(
                    update,
                    t("session.multiple_matches").format(names=names),
                    project_id=project_id,
                )
                # session stays open
            return True

        if flow_type == "resolution_learning":
            from src.advisory.resolution_learner import handle_resolution_followup_reply
            from src.telegram.session import complete_session

            with self.scoped_db_write() as session_conn:
                result = handle_resolution_followup_reply(session_conn, data, user_text)
                complete_session(session_conn, session["session_id"])
            await self._safe_reply(
                update,
                result.get("text", t("session.resolution_noted")),
                project_id=project_id,
                session_id=session["session_id"],
            )
            return True

        if flow_type == "batch_item_edit":
            editing_idx = data.get("editing_idx")
            items = data.get("items", [])

            # Allow cancel
            if user_text.strip().lower() in {"cancel", "/cancel", "stop"}:
                data.pop("editing_idx", None)
                with self.scoped_db_write() as session_conn:
                    session_conn.execute(
                        """UPDATE telegram_sessions
                           SET flow_type = 'batch_review', intermediate_data = ?
                           WHERE session_id = ?""",
                        (json.dumps(data, ensure_ascii=False), session["session_id"]),
                    )
                await self._safe_reply(update, t("session.edit_cancelled"), session_id=session["session_id"])
                return True

            if editing_idx is None or editing_idx >= len(items):
                with self.scoped_db_write() as session_conn:
                    session_conn.execute(
                        "UPDATE telegram_sessions SET flow_type = 'batch_review' WHERE session_id = ?",
                        (session["session_id"],),
                    )
                await self._safe_reply(update, t("session.batch_edit_invalid"), session_id=session["session_id"])
                return True

            item = items[editing_idx]
            field_name = next(iter(item.get("changes", {})), None)
            if field_name:
                items[editing_idx]["changes"][field_name] = user_text.strip()
            data["items"] = items
            data.pop("editing_idx", None)

            with self.scoped_db_write() as session_conn:
                session_conn.execute(
                    """UPDATE telegram_sessions
                       SET flow_type = 'batch_review', intermediate_data = ?
                       WHERE session_id = ?""",
                    (json.dumps(data, ensure_ascii=False), session["session_id"]),
                )

            # Rebuild the batch card with updated values
            from src.portfolio.batch_update import BatchItem, BatchPreview
            from src.telegram.handlers_batch import build_batch_card

            preview_items = [
                BatchItem(
                    project_id=it["project_id"],
                    client_name=it.get("client_name"),
                    changes=it.get("changes", {}),
                    status=it.get("status", "pending"),
                )
                for it in items
            ]
            preview = BatchPreview(
                instruction=data.get("instruction", ""),
                items=preview_items,
                total_affected=sum(1 for i in preview_items if i.status == "pending"),
            )
            card = build_batch_card(session["session_id"], preview)
            await self._safe_send(
                update.effective_chat.id,
                format_card_text(card),
                reply_to_message_id=update.message.message_id if update.message else None,
                session_id=session["session_id"],
                message_kind="card",
                reply_markup=card_to_markup(card),
            )
            return True

        return False
