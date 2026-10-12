"""
CommandsMixin: basic slash-command handlers for TelegramBot.
Mixed into TelegramBot in bot.py.
"""

from __future__ import annotations

from telegram import Update

from src.telegram.cards import build_card, format_card_text
from src.telegram.i18n import t
from src.telegram.markup import card_to_markup


def _parse_notification_mode(arg: str) -> str | None:
    """Return validated notification mode or None."""
    return arg if arg in ("silent", "active", "emergency") else None


def _parse_thinking_mode(arg: str) -> bool | None:
    """Return validated thinking window mode or None."""
    if arg == "on":
        return True
    if arg == "off":
        return False
    return None


def _thinking_mode_label(enabled: bool) -> str:
    return "開啟" if enabled else "關閉"


class CommandsMixin:
    async def _cmd_start(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        self._persist_command(update, command_name="start")
        await self._safe_reply(
            update,
            t("cmd.start"),
            message_kind="command_reply",
        )

    async def _cmd_scan(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        self._persist_command(update, command_name="scan")
        from src.portfolio.portfolio_scan import format_portfolio_text, scan_portfolio

        with self.scoped_db_read() as conn:
            portfolio_text = format_portfolio_text(scan_portfolio(conn))
        await self._safe_reply(update, portfolio_text, message_kind="command_reply")

    async def _cmd_status(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        args = context.args or []
        project_id = args[0].upper() if args else None
        self._persist_command(update, command_name="status", project_id=project_id)
        from src.status.answer import answer_status, answer_status_all, format_status_telegram

        with self.scoped_db_read() as conn:
            if not args:
                items = answer_status_all(conn)
                if not items:
                    await self._safe_reply(update, t("cmd.status_no_projects"), message_kind="command_reply")
                    return
                lines = [t("cmd.status_header")]
                for item in items:
                    lines.append(
                        f"  {item['project_id']} ({item.get('client_name', '-')}) "
                        f"- {item.get('current_milestone', '-')} [{item.get('ball_with', '-')}]"
                    )
                await self._safe_reply(update, "\n".join(lines), message_kind="command_reply")
                return

            question = " ".join(args[1:]) if len(args) > 1 else None
            result = answer_status(conn, project_id, question=question)
        if result is None:
            await self._safe_reply(
                update,
                t("cmd.status_not_found").format(project_id=project_id),
                message_kind="command_reply",
            )
            return

        await self._safe_reply(
            update, format_status_telegram(result), project_id=project_id, message_kind="command_reply",
        )

    async def _cmd_pending(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        self._persist_command(update, command_name="pending")
        from src.email.draft_session import get_pending_drafts

        with self.scoped_db_read() as conn:
            drafts = get_pending_drafts(conn)
        if not drafts:
            await self._safe_reply(update, t("cmd.pending_none"), message_kind="command_reply")
            return
        for draft in drafts:
            preview = (draft.get('current_draft_text') or '')[:120]
            if preview:
                preview = preview.replace('\n', ' ').strip()
                if len(preview) >= 118:
                    preview = preview + "..."
            from src.telegram.card_push import _generate_draft_summary
            summary = _generate_draft_summary(
                draft.get('subject', ''), draft.get('current_draft_text', '')
            )
            body_lines = [f"📧 To: {draft.get('to_email', '-')}"]
            if summary:
                body_lines.append(f"💬 {summary}")
            elif preview:
                body_lines.append(f"📋 {preview}")
            card = build_card(
                "D",
                f"Draft: {draft['subject']}",
                "\n".join(body_lines),
                project_id=draft.get("project_id"),
            )
            await self._safe_send(
                update.effective_chat.id,
                format_card_text(card),
                reply_to_message_id=update.message.message_id,
                project_id=draft.get("project_id"),
                message_kind="card",
                reply_markup=card_to_markup(card),
                object_links=[{"object_type": "draft", "object_ref": draft["draft_id"], "project_id": draft.get("project_id")}],
            )

    async def _cmd_approve(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        args = context.args or []
        self._persist_command(update, command_name="approve")
        if not args:
            await self._safe_reply(update, t("cmd.approve_usage"), message_kind="command_reply")
            return
        draft_id = args[0]
        from config.settings import CONFIG
        from src.email.draft_session import get_draft
        from src.email.sending import email_config_dict, imap_config_dict
        from src.repositories.activity_log_repo import insert_activity
        from src.services.draft_approval_command import (
            commit_immediate_draft_approval,
            dispatch_committed_draft_approval,
        )
        from src.telegram.operator_binding import resolve_telegram_actor, telegram_user_id_from_update

        project_id = None
        with self.scoped_db_write() as conn:
            scope_start_changes = conn.total_changes
            actor_id = resolve_telegram_actor(conn, telegram_user_id_from_update(update))
            if actor_id is None:
                text = "Command not executed. Link your Telegram account to an active user."
            else:
                draft = get_draft(conn, draft_id)
                if draft is None:
                    text = t("cmd.draft_not_found").format(draft_id=draft_id)
                else:
                    project_id = draft.get("project_id")
                    email_config = email_config_dict(CONFIG)
                    receipt = commit_immediate_draft_approval(
                        conn, draft_id, actor_user_id=actor_id,
                        email_config=email_config, scope_start_changes=scope_start_changes,
                    )
                    receipt = dispatch_committed_draft_approval(
                        conn, receipt, email_config=email_config,
                        imap_config=imap_config_dict(CONFIG),
                    )
                    if receipt.state == "sent":
                        try:
                            insert_activity(
                                conn,
                                actor=actor_id,
                                action_type="draft_approved",
                                project_id=project_id,
                                surface="telegram",
                                object_ref=draft_id,
                                summary=f"Draft {draft_id} approved and sent",
                            )
                            conn.commit()
                        except Exception:
                            # Delivery's durable receipt, not optional activity
                            # bookkeeping, determines the user-facing result.
                            conn.rollback()
                        text = t("cmd.approve_sent").format(draft_id=draft_id)
                    elif receipt.state == "not_queued":
                        text = t("cmd.approve_not_queued").format(
                            draft_id=draft_id, error=receipt.error,
                        )
                    elif receipt.state == "queued":
                        text = t("cmd.approve_queued").format(draft_id=draft_id)
                    elif receipt.state == "in_progress":
                        text = t("cmd.approve_in_progress").format(draft_id=draft_id)
                    elif receipt.state == "uncertain":
                        text = t("cmd.approve_send_uncertain").format(
                            draft_id=draft_id, error=receipt.error,
                        )
                    else:
                        text = t("cmd.approve_send_failed").format(
                            draft_id=draft_id, error=receipt.error,
                        )
        await self._safe_reply(
            update, text, project_id=project_id, message_kind="command_reply",
        )

    async def _cmd_reject(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        args = context.args or []
        self._persist_command(update, command_name="reject")
        if not args:
            await self._safe_reply(update, t("cmd.reject_usage"), message_kind="command_reply")
            return
        draft_id = args[0]
        from src.email.draft_session import get_draft
        from src.services.draft_service import draft_discard

        with self.scoped_db_write() as conn:
            draft = get_draft(conn, draft_id)
            if not draft:
                await self._safe_reply(update, t("cmd.draft_not_found").format(draft_id=draft_id), message_kind="command_reply")
                return
            if draft["status"] not in ("created", "in_review", "approved", "send_failed"):
                await self._safe_reply(
                    update,
                    t("cmd.reject_cannot").format(draft_id=draft_id, status=draft["status"]),
                    project_id=draft.get("project_id"),
                    message_kind="command_reply",
                )
                return
            draft_discard(conn, draft_id, actor="operator", surface="telegram")
        text = t("cmd.reject_discarded").format(draft_id=draft_id)
        await self._safe_reply(update, text, project_id=draft.get("project_id"), message_kind="command_reply")

    async def _cmd_brief(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        self._persist_command(update, command_name="brief")
        from src.portfolio.morning_brief import format_brief_text, generate_morning_brief

        with self.scoped_db_read() as conn:
            brief = generate_morning_brief(conn)
        card = build_card("I", "Morning Brief", format_brief_text(brief))
        await self._safe_send(
            update.effective_chat.id,
            format_card_text(card),
            reply_to_message_id=update.message.message_id,
            message_kind="card",
            reply_markup=card_to_markup(card),
        )

    async def _cmd_note(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        args = context.args or []
        project_id = args[0].upper() if args else None
        self._persist_command(update, command_name="note", project_id=project_id)
        if len(args) < 2:
            await self._safe_reply(update, t("cmd.note_usage"), project_id=project_id, message_kind="command_reply")
            return
        from src.notes.note_service import store_note
        from src.repositories.project_repo import project_exists

        note_text = " ".join(args[1:])
        with self.scoped_db_write() as conn:
            if not project_exists(conn, project_id):
                await self._safe_reply(update, t("cmd.note_project_not_found").format(project_id=project_id), message_kind="command_reply")
                return
            result = store_note(conn, project_id, note_text, source="telegram")
        await self._safe_reply(update, t("cmd.note_stored").format(project_id=result.project_id, evidence_id=result.evidence_id, summary=result.summary), project_id=project_id, message_kind="command_reply")

    async def _cmd_track(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        args = context.args or []
        project_id = args[0].upper() if args else None
        self._persist_command(update, command_name="track", project_id=project_id)
        if len(args) < 3:
            await self._safe_reply(update, t("cmd.track_usage"), project_id=project_id, message_kind="command_reply")
            return
        from src.portfolio.followup_tracker import create_followup
        from src.repositories.project_repo import project_exists

        due_date = args[1]
        description = " ".join(args[2:])
        with self.scoped_db_write() as conn:
            if not project_exists(conn, project_id):
                await self._safe_reply(update, t("cmd.track_project_not_found").format(project_id=project_id), message_kind="command_reply")
                return
            followup_id = create_followup(conn, project_id, description, due_date, created_by="operator")
        await self._safe_reply(update, t("cmd.track_created").format(followup_id=followup_id, project_id=project_id, due_date=due_date, description=description), project_id=project_id, message_kind="command_reply")

    async def _cmd_close(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        args = context.args or []
        project_id = args[0].upper() if args else None
        self._persist_command(update, command_name="close", project_id=project_id)
        if not project_id:
            await self._safe_reply(update, t("cmd.close_usage"), message_kind="command_reply")
            return

        from src.telegram.closing_advisory import build_closing_advisory

        with self.scoped_db_read() as conn:
            message = build_closing_advisory(conn, project_id)
        await self._safe_reply(
            update,
            message,
            project_id=project_id,
            message_kind="command_reply",
        )

    async def _cmd_reopen(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        args = context.args or []
        project_id = args[0].upper() if args else None
        self._persist_command(update, command_name="reopen", project_id=project_id)
        if not project_id:
            await self._safe_reply(update, t("cmd.reopen_usage"), message_kind="command_reply")
            return

        from src.telegram.closing_advisory import build_closing_advisory

        with self.scoped_db_read() as conn:
            message = build_closing_advisory(conn, project_id, reopen=True)
        await self._safe_reply(
            update,
            message,
            project_id=project_id,
            message_kind="command_reply",
        )

    async def _cmd_flag(self, update: Update, context) -> None:
        """Set/clear operator urgency flag for a project (for morning brief scoring)."""
        if not self._auth(update):
            return
        args = context.args or []
        self._persist_command(update, command_name="flag")
        if len(args) < 2:
            await self._safe_reply(
                update,
                t("cmd.flag_usage"),
                message_kind="command_reply",
            )
            return
        project_id = args[0].upper()
        flag_value = args[1].lower()
        if flag_value not in ("urgent", "watch", "clear"):
            await self._safe_reply(
                update,
                t("cmd.flag_invalid"),
                message_kind="command_reply",
            )
            return
        from src.repositories.working_memory_repo import upsert_memory

        with self.scoped_db_write() as conn:
            if flag_value == "clear":
                conn.execute(
                    "DELETE FROM working_memory WHERE project_id = ? AND memory_key = 'operator_flag'",
                    (project_id,),
                )
        if flag_value == "clear":
            await self._safe_reply(
                update, t("cmd.flag_cleared").format(project_id=project_id), project_id=project_id, message_kind="command_reply"
            )
        else:
            with self.scoped_db_write() as conn:
                upsert_memory(conn, project_id, "operator_flag", flag_value)
            await self._safe_reply(
                update,
                t("cmd.flag_set").format(project_id=project_id, flag_value=flag_value),
                project_id=project_id,
                message_kind="command_reply",
            )

    async def _cmd_activity(self, update: Update, context) -> None:
        """Show recent activity timeline for a project."""
        if not self._auth(update):
            return
        args = context.args or []
        project_id = args[0].upper() if args else None
        self._persist_command(update, command_name="activity", project_id=project_id)
        if not project_id:
            await self._safe_reply(update, t("cmd.activity_usage"), message_kind="command_reply")
            return
        from src.repositories.activity_log_repo import get_recent_activity

        with self.scoped_db_read() as conn:
            events = get_recent_activity(conn, project_id, limit=10)
        if not events:
            await self._safe_reply(update, t("cmd.activity_none").format(project_id=project_id), project_id=project_id, message_kind="command_reply")
            return
        lines = [t("cmd.activity_header").format(project_id=project_id)]
        for e in events:
            ts = (e.get("created_at") or "")[:16].replace("T", " ")
            actor = e.get("actor", "-")
            action = e.get("action_type", "-")
            summary = e.get("summary") or ""
            lines.append(f"[{ts}] {actor} · {action}" + (f"\n  {summary}" if summary else ""))
        await self._safe_reply(update, "\n".join(lines), project_id=project_id, message_kind="command_reply")

    async def _cmd_set_preference(self, update: Update, context) -> None:
        """Set a preference field: /set_preference <ProjectID|ContactID> <field=value>"""
        if not self._auth(update):
            return
        args = context.args or []
        self._persist_command(update, command_name="set_preference")
        if len(args) < 2:
            await self._safe_reply(
                update,
                t("cmd.set_preference_usage"),
                message_kind="command_reply",
            )
            return

        scope_id = args[0].upper()
        field_value_str = " ".join(args[1:])
        if "=" not in field_value_str:
            await self._safe_reply(update, t("cmd.set_preference_format"), message_kind="command_reply")
            return

        field, _, value = field_value_str.partition("=")
        field = field.strip().lower()
        value = value.strip()

        valid_fields = {"tone", "language", "push_style", "custom_notes"}
        if field not in valid_fields:
            await self._safe_reply(
                update,
                t("cmd.set_preference_invalid_field").format(field=field, valid_fields=', '.join(sorted(valid_fields))),
                message_kind="command_reply",
            )
            return

        from src.repositories.preference_repo import OperatorPreference, get_effective_preference, upsert_preference
        from src.repositories.project_repo import project_exists

        with self.scoped_db_write() as conn:
            if project_exists(conn, scope_id):
                pref = get_effective_preference(conn, scope_id)
                pref.scope = "project"
                pref.project_id = scope_id
                pref.counterparty_id = None
            else:
                pref = get_effective_preference(conn, scope_id, counterparty_id=scope_id)
                pref.scope = "counterparty"
                pref.project_id = None
                pref.counterparty_id = scope_id

            setattr(pref, field, value)
            upsert_preference(conn, pref)
        await self._safe_reply(
            update,
            t("cmd.set_preference_updated").format(scope_id=scope_id, field=field, value=value),
            message_kind="command_reply",
        )

    async def _cmd_delete_preference(self, update: Update, context) -> None:
        """Delete a preference record: /delete_preference <project|counterparty> <ID>"""
        if not self._auth(update):
            return
        args = context.args or []
        self._persist_command(update, command_name="delete_preference")
        if len(args) < 2:
            await self._safe_reply(
                update,
                t("cmd.delete_preference_usage"),
                message_kind="command_reply",
            )
            return

        scope = args[0].lower()
        scope_id = args[1].upper()
        if scope not in ("project", "counterparty"):
            await self._safe_reply(update, t("cmd.delete_preference_invalid_scope"), message_kind="command_reply")
            return

        from src.repositories.preference_repo import delete_preference

        with self.scoped_db_write() as conn:
            deleted = delete_preference(
                conn,
                scope,
                project_id=scope_id if scope == "project" else None,
                counterparty_id=scope_id if scope == "counterparty" else None,
            )
        if deleted:
            await self._safe_reply(
                update,
                t("cmd.delete_preference_deleted").format(scope=scope, scope_id=scope_id),
                message_kind="command_reply",
            )
        else:
            await self._safe_reply(
                update,
                t("cmd.delete_preference_not_found").format(scope=scope, scope_id=scope_id),
                message_kind="command_reply",
            )

    async def _cmd_thinking(self, update: Update, context) -> None:
        """Toggle or set thinking debug window: /thinking [on|off]"""
        if not self._auth(update):
            return
        self._persist_command(update, command_name="thinking")
        args = context.args or []
        from src.repositories.operator_state_repo import (
            set_thinking_window_enabled,
            toggle_thinking_window,
        )

        with self.scoped_db_write() as conn:
            if not args:
                enabled = toggle_thinking_window(conn)
                await self._safe_reply(
                    update,
                    t("cmd.thinking_toggled").format(mode=_thinking_mode_label(enabled)),
                    message_kind="command_reply",
                )
                return

            mode = _parse_thinking_mode(args[0].lower())
            if mode is None:
                await self._safe_reply(
                    update, t("cmd.thinking_invalid"), message_kind="command_reply"
                )
                return

            set_thinking_window_enabled(conn, mode)
            key = "cmd.thinking_set_on" if mode else "cmd.thinking_set_off"
            await self._safe_reply(update, t(key), message_kind="command_reply")

    async def _cmd_notification(self, update: Update, context) -> None:
        """Get or set notification mode: /notification [silent|active|emergency]"""
        if not self._auth(update):
            return
        self._persist_command(update, command_name="notification")
        args = context.args or []
        from src.repositories.operator_state_repo import get_notification_mode, set_notification_mode

        with self.scoped_db_write() as conn:
            if not args:
                mode = get_notification_mode(conn)
                await self._safe_reply(
                    update,
                    t("cmd.notification_current").format(mode=mode),
                    message_kind="command_reply",
                )
                return
            new_mode = _parse_notification_mode(args[0].lower())
            if new_mode is None:
                await self._safe_reply(
                    update, t("cmd.notification_invalid"), message_kind="command_reply"
                )
                return
            set_notification_mode(conn, new_mode)
            await self._safe_reply(
                update, t("cmd.notification_set").format(new_mode=new_mode), message_kind="command_reply"
            )


    async def _cmd_session(self, update: Update, context) -> None:
        """Show verified FIX session configuration; never reveal secrets inline."""
        if not self._auth(update):
            return
        args = context.args or []
        project_id = args[0].upper() if args else None
        environment = args[1].upper() if len(args) > 1 else None
        self._persist_command(update, command_name="session", project_id=project_id)
        if not project_id:
            await self._safe_reply(update, "Usage: /session PROJECT [UAT|PRODUCTION]", message_kind="command_reply")
            return
        if environment and environment not in {"UAT", "PRODUCTION", "PROD"}:
            await self._safe_reply(update, "Environment must be UAT or PRODUCTION.", project_id=project_id, message_kind="command_reply")
            return
        from src.fix_sessions.repository import list_candidates, list_sessions
        with self.scoped_db_read() as conn:
            sessions = list_sessions(conn, project_id, "PRODUCTION" if environment == "PROD" else environment)
            pending = list_candidates(conn, project_id)
        if not sessions:
            text = f"No verified FIX session configuration found for {project_id}."
            if pending:
                text += f"\n{len(pending)} candidate(s) are awaiting confirmation."
            await self._safe_reply(update, text, project_id=project_id, message_kind="command_reply")
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from config import defaults
        private_chat = getattr(update.effective_chat, "type", None) == "private"
        rows = []
        buttons = []
        for item in sessions:
            endpoints = ", ".join(f"{ep['route_role']} {ep['host']}:{ep['port']}" for ep in item.get("endpoints", []))
            rows.extend([
                f"{item['environment']} / {item['session_key']}",
                f"SenderCompID: {item['sender_comp_id']}",
                f"TargetCompID: {item['target_comp_id']}",
                f"Endpoint: {endpoints or '-'}",
                f"Username: {'??????' if item.get('username_secret_reference') else '-'}",
                f"Password: {'??????' if item.get('password_secret_reference') else '-'}",
                "",
            ])
            if private_chat and defaults.FIX_SESSION_SECRET_REVEAL_ENABLED:
                for secret_name in ("username", "password"):
                    if item.get(f"{secret_name}_secret_reference"):
                        buttons.append([InlineKeyboardButton(
                            f"Reveal {item['environment']} {secret_name} once",
                            callback_data=f"fxr:{project_id}:{item['environment']}:{secret_name}",
                        )])
        if pending:
            rows.append(f"Pending: {len(pending)} candidate(s) awaiting confirmation.")
        await self._safe_reply(update, "\n".join(rows).strip(), project_id=project_id, message_kind="command_reply", reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

    async def _cmd_help(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        self._persist_command(update, command_name="help")
        await self._safe_reply(
            update,
            t("cmd.help"),
            message_kind="command_reply",
        )
