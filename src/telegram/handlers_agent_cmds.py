"""
AgentCommandsMixin: agent-driven and workflow slash-command handlers for TelegramBot.
Mixed into TelegramBot in bot.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from telegram import Update

from src.telegram.bot_helpers import (
    _build_query_clarification_card,
    _llm_parse_project_query,
    _query_session_payload,
)
from src.telegram.cards import build_card, format_card_text
from src.telegram.i18n import t
from src.telegram.markup import card_to_markup
from src.telegram.operator_binding import resolve_telegram_operator, telegram_user_id_from_update


def _extract_positive_phrases(body: str) -> list[str]:
    """Use LLM to extract 3-5 positive key phrases from a sent draft body.

    Falls back to a date-stamped generic signal if LLM is unavailable.
    """
    if not body or len(body.strip()) < 20:
        return [f"positive_signal:{datetime.now(timezone.utc).date().isoformat()}"]
    try:
        from src.llm.client import llm_call_structured
        from src.utils.prompt_loader import load_prompt
        schema = {
            "type": "object",
            "properties": {
                "phrases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                }
            },
            "required": ["phrases"],
        }
        prompt = load_prompt("learning/phrase_extraction.md") + f"\n\nDraft:\n{body[:800]}"
        result = llm_call_structured("phrase_extraction", prompt, schema, temperature=0.0)
        phrases = result.get("phrases") or []
        if phrases:
            return [f"style_phrase:{p.strip()}" for p in phrases if p.strip()]
    except Exception:
        pass
    return [f"positive_signal:{datetime.now(timezone.utc).date().isoformat()}"]


class AgentCommandsMixin:
    async def _cmd_signal_positive(self, update: Update, context) -> None:
        """Mark the most recent sent draft for a project as a positive signal."""
        if not self._auth(update):
            return
        args = context.args or []
        self._persist_command(update, command_name="signal_positive")
        if not args:
            await self._safe_reply(update, t("cmd.signal_positive_usage"), message_kind="command_reply")
            return
        project_id = args[0].upper()

        with self.scoped_db_read() as conn:
            sent_rows = conn.execute(
                """SELECT * FROM drafts
                   WHERE project_id = ? AND status = 'sent'
                   ORDER BY updated_at DESC LIMIT 1""",
                (project_id,),
            ).fetchall()
            if not sent_rows:
                await self._safe_reply(update, t("cmd.signal_positive_no_drafts").format(project_id=project_id), project_id=project_id, message_kind="command_reply")
                return

            from src.repositories.contact_repo import find_contact_by_email
            proj = conn.execute(
                "SELECT client_email FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            contact_id = None
            if proj and proj["client_email"]:
                contact = find_contact_by_email(conn, proj["client_email"])
                contact_id = contact["contact_id"] if contact else None
            sent_draft = dict(sent_rows[0])

        if contact_id:
            from src.repositories.response_pattern_repo import add_positive_signal
            phrases = _extract_positive_phrases(sent_draft.get("current_draft_text") or "")
            with self.scoped_db_write() as conn:
                for phrase in phrases:
                    add_positive_signal(conn, contact_id, phrase, project_id=project_id)
            label = f"{len(phrases)} phrase(s)" if phrases else "positive signal"
            await self._safe_reply(
                update,
                t("cmd.signal_positive_recorded").format(project_id=project_id, label=label),
                project_id=project_id, message_kind="command_reply",
            )
        else:
            await self._safe_reply(
                update,
                t("cmd.signal_positive_no_contact").format(project_id=project_id),
                project_id=project_id, message_kind="command_reply",
            )

    async def _cmd_skip(self, update: Update, context) -> None:
        """Suppress a Morning Brief item for today only."""
        if not self._auth(update):
            return
        args = context.args or []
        self._persist_command(update, command_name="skip")
        if not args:
            await self._safe_reply(update, t("cmd.skip_usage"), message_kind="command_reply")
            return
        project_id = args[0].upper()
        from src.repositories.working_memory_repo import upsert_memory

        skip_until = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.scoped_db_write() as conn:
            upsert_memory(conn, project_id, "brief_item_suppressed_until", skip_until)
        await self._safe_reply(
            update,
            t("cmd.skip_suppressed").format(project_id=project_id),
            project_id=project_id, message_kind="command_reply",
        )

    async def _cmd_reply(self, update: Update, context) -> None:
        """Immediately trigger draft_reply from Morning Brief card for a project."""
        if not self._auth(update):
            return
        args = context.args or []
        self._persist_command(update, command_name="reply")
        if not args:
            await self._safe_reply(update, t("cmd.reply_usage"), message_kind="command_reply")
            return
        project_id = args[0].upper()
        from src.tools.executor import _handle_draft_reply

        try:
            with self.scoped_db_write() as conn:
                result = _handle_draft_reply(conn, {
                    "project_id": project_id,
                    "context": "Operator triggered via /reply command",
                })
            if result.get("subject") and result.get("body") and not result.get("error"):
                await self._safe_reply(
                    update,
                    t("cmd.reply_generated").format(project_id=project_id),
                    project_id=project_id, message_kind="command_reply",
                )
            else:
                await self._safe_reply(
                    update,
                    t("cmd.reply_failed").format(project_id=project_id, error=result.get('error') or 'empty draft returned'),
                    project_id=project_id, message_kind="command_reply",
                )
        except Exception as exc:
            await self._safe_reply(
                update,
                t("cmd.reply_failed").format(project_id=project_id, error=exc),
                project_id=project_id, message_kind="command_reply",
            )

    async def _cmd_query(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        self._persist_command(update, command_name="query")
        args = context.args or []
        if not args:
            await self._safe_reply(update, t("cmd.query_usage"), message_kind="command_reply")
            return
        from src.portfolio.aggregate_query import (
            evaluate_aggregate_query,
            format_query_result_text,
        )

        free_text = " ".join(args)
        with self.scoped_db_read() as conn:
            evaluation = evaluate_aggregate_query(
                conn,
                free_text,
                llm_parse_fn=_llm_parse_project_query,
            )
        if evaluation.status != "ready":
            from src.telegram.session import create_session, set_session_data

            with self.scoped_db_write() as conn:
                operator_id = resolve_telegram_operator(
                    conn, telegram_user_id_from_update(update),
                ).operator_id
                session_id = create_session(
                    conn,
                    "query_modify",
                    originating_ref=free_text,
                    operator_id=operator_id,
                )
                set_session_data(conn, session_id, _query_session_payload(evaluation))
            card = _build_query_clarification_card(evaluation)
            await self._safe_send(
                update.effective_chat.id,
                format_card_text(card),
                reply_to_message_id=update.message.message_id if update.message else None,
                session_id=session_id,
                message_kind="card",
                object_links=[{"object_type": "telegram_session", "object_ref": session_id}],
                reply_markup=card_to_markup(card),
            )
            return
        await self._safe_reply(
            update,
            format_query_result_text(evaluation),
            message_kind="command_reply",
        )

    async def _cmd_newproject(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        args = context.args or []
        project_id = args[0].upper() if args else None
        self._persist_command(update, command_name="newproject", project_id=project_id)
        if len(args) < 2:
            await self._safe_reply(update, t("cmd.newproject_usage"), message_kind="command_reply")
            return
        from src.portfolio.project_intake import create_intake_from_structured
        from src.tools.project_intake_handlers import store_intake_proposal

        client_name = args[1]
        client_email = args[2] if len(args) > 2 else None
        proposal = create_intake_from_structured(
            project_id, client_name, client_email=client_email, source="telegram",
        )
        with self.scoped_db_write() as conn:
            store_intake_proposal(conn, proposal)
        card = build_card(
            "F",  # Profile F: New Project Intake Card
            "New Project",
            "\n".join([
                t("cmd.newproject_card_id").format(project_id=proposal.project_id),
                t("cmd.newproject_card_client").format(client_name=proposal.client_name),
                t("cmd.newproject_card_email").format(email=proposal.client_email or '—'),
                t("cmd.newproject_card_type").format(connection_type=proposal.connection_type),
            ]),
            project_id=project_id,
        )
        await self._safe_send(
            update.effective_chat.id,
            format_card_text(card),
            reply_to_message_id=update.message.message_id,
            project_id=project_id,
            message_kind="card",
            reply_markup=card_to_markup(card),
            object_links=[{"object_type": "project_intake", "object_ref": project_id, "project_id": project_id}],
        )

    async def _cmd_report(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        self._persist_command(update, command_name="report")
        from src.portfolio.periodic_reports import format_report_markdown, generate_monthly_report, generate_weekly_report

        report_type = (context.args or ["weekly"])[0].lower()
        now = datetime.now(timezone.utc)
        with self.scoped_db_read() as conn:
            if report_type == "monthly":
                period_start = now.replace(day=1).strftime("%Y-%m-%d")
                report = generate_monthly_report(conn, period_start, now.strftime("%Y-%m-%d"))
            else:
                period_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
                report = generate_weekly_report(conn, period_start, now.strftime("%Y-%m-%d"))
        await self._safe_reply(update, format_report_markdown(report), message_kind="command_reply")

    async def _cmd_batch(self, update: Update, context) -> None:
        if not self._auth(update):
            return
        self._persist_command(update, command_name="batch")
        args = context.args or []
        if len(args) < 2:
            await self._safe_reply(update, t("cmd.batch_usage"), message_kind="command_reply")
            return
        from src.portfolio.batch_update import execute_batch, format_batch_preview, prepare_batch

        confirm = "confirm" in [a.lower() for a in args]
        filtered_args = [a for a in args if a.lower() != "confirm"]
        field_name = filtered_args[0]
        new_value = filtered_args[1]
        project_filter = {}
        for arg in filtered_args[2:]:
            if "=" in arg:
                key, value = arg.split("=", 1)
                project_filter[key] = value
        with self.scoped_db_read() as conn:
            preview = prepare_batch(conn, field_name, new_value, project_filter=project_filter or None)
        text = format_batch_preview(preview)
        if preview.warnings:
            await self._safe_reply(update, text, message_kind="command_reply")
            return
        if preview.total_affected == 0:
            await self._safe_reply(update, t("cmd.batch_no_projects"), message_kind="command_reply")
            return
        if not confirm:
            from src.portfolio.batch_update import save_batch_session
            from src.telegram.handlers_batch import build_batch_card
            from src.telegram.cards import format_card_text
            from src.telegram.markup import card_to_markup
            from src.telegram.session import create_session

            with self.scoped_db_write() as conn:
                operator_id = resolve_telegram_operator(
                    conn, telegram_user_id_from_update(update),
                ).operator_id
                session_id = create_session(conn, "batch_review", operator_id=operator_id)
                save_batch_session(conn, session_id, preview)
            card = build_batch_card(session_id, preview)
            await self._safe_send(
                update.effective_chat.id,
                format_card_text(card),
                reply_to_message_id=update.message.message_id if update.message else None,
                session_id=session_id,
                message_kind="card",
                object_links=[{"object_type": "telegram_session", "object_ref": session_id}],
                reply_markup=card_to_markup(card),
            )
            return
        with self.scoped_db_write() as conn:
            results = execute_batch(conn, preview)
        applied = sum(1 for r in results if r.status == "applied")
        failed = sum(1 for r in results if r.status != "applied")
        await self._safe_reply(update, t("cmd.batch_complete").format(applied=applied, failed=failed), message_kind="command_reply")
