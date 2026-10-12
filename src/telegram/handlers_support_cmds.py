"""
SupportCommandsMixin: /ticket, /support, /resolve, /lang command handlers for TelegramBot.
Mixed into TelegramBot in bot.py.
"""

from __future__ import annotations

from telegram import Update

from src.telegram.i18n import t

# Canonical display labels for operator response language (menu surface is EN/ZH).
_LANG_LABELS: dict[str, str] = {
    "zh-TW": "繁體中文 (zh-TW)",
    "en": "English (en)",
    "zh-CN": "简体中文 (zh-CN)",
}

# Confirmation keys keyed by canonical language code.
_LANG_SET_KEYS: dict[str, str] = {
    "en": "cmd.lang_set_en",
    "zh-TW": "cmd.lang_set_zh",
    "zh-CN": "cmd.lang_set_zh_cn",
}


def _parse_response_language(raw: str) -> str | None:
    """Map user input to a canonical response_language code, or None if invalid."""
    key = raw.strip().lower().replace("_", "-")
    aliases = {
        "en": "en",
        "english": "en",
        "zh": "zh-TW",
        "zh-tw": "zh-TW",
        "chinese": "zh-TW",
        "zh-cn": "zh-CN",
        "cn": "zh-CN",
    }
    return aliases.get(key)


def _response_language_label(code: str) -> str:
    """Human-readable label for a stored response_language code."""
    return _LANG_LABELS.get(code, code)


class SupportCommandsMixin:
    """Mixin adding support-ticket and language slash commands to TelegramBot."""

    async def _cmd_ticket(self, update: Update, context) -> None:
        """Create a support ticket.  /ticket <title> [category=fix_session] [severity=medium]"""
        if not self._auth(update):
            return
        args = context.args or []
        self._persist_command(update, command_name="ticket")
        if not args:
            await self._safe_reply(
                update,
                t("cmd.ticket_usage"),
                message_kind="command_reply",
            )
            return

        # Parse keyword args from tail
        category = "other"
        severity = "medium"
        title_parts: list[str] = []
        for arg in args:
            if arg.startswith("category="):
                category = arg.split("=", 1)[1]
            elif arg.startswith("severity="):
                severity = arg.split("=", 1)[1]
            else:
                title_parts.append(arg)

        title = " ".join(title_parts).strip()
        if not title:
            await self._safe_reply(update, t("cmd.ticket_title_required"), message_kind="command_reply")
            return

        from src.tools.support_chat_handlers import chat_create_ticket

        with self.scoped_db_write() as conn:
            result = chat_create_ticket(conn, {
                "title": title,
                "category": category,
                "severity": severity,
            })
        await self._safe_reply(update, result["text"], message_kind="command_reply")

    async def _cmd_support(self, update: Update, context) -> None:
        """Show open support tickets.  /support [status] [limit]"""
        if not self._auth(update):
            return
        self._persist_command(update, command_name="support")
        args = context.args or []

        status = ""
        limit = 10
        for arg in args:
            if arg.isdigit():
                limit = int(arg)
            else:
                status = arg

        from src.tools.support_chat_handlers import chat_search_tickets

        with self.scoped_db_read() as conn:
            result = chat_search_tickets(conn, {
                "status": status or "open",
                "limit": str(limit),
            })
        await self._safe_reply(update, result["text"], message_kind="command_reply")

    async def _cmd_resolve(self, update: Update, context) -> None:
        """Record a resolution.  /resolve <ticket_id> <resolution text>"""
        if not self._auth(update):
            return
        args = context.args or []
        self._persist_command(update, command_name="resolve")
        if len(args) < 2:
            await self._safe_reply(
                update,
                t("cmd.resolve_usage"),
                message_kind="command_reply",
            )
            return

        ticket_id = args[0].upper()
        resolution_text = " ".join(args[1:])

        from src.tools.support_chat_handlers import chat_record_resolution

        with self.scoped_db_write() as conn:
            result = chat_record_resolution(conn, {
                "ticket_id": ticket_id,
                "resolution_text": resolution_text,
            })
        await self._safe_reply(update, result["text"], message_kind="command_reply")

    async def _cmd_lang(self, update: Update, context) -> None:
        """Get or set agent response language: /lang [en|zh]."""
        if not self._auth(update):
            return
        self._persist_command(update, command_name="lang")
        args = context.args or []
        from src.repositories.operator_state_repo import (
            get_response_language,
            set_response_language,
        )
        from src.telegram.i18n import clear_ui_language_cache

        def _ui_locale(code: str) -> str:
            return "en" if code == "en" else "zh-TW"

        if not args:
            with self.scoped_db_read() as conn:
                current = get_response_language(conn)
            await self._safe_reply(
                update,
                t("cmd.lang_current", lang=_ui_locale(current)).format(
                    label=_response_language_label(current)
                ),
                message_kind="command_reply",
            )
            return

        canonical = _parse_response_language(args[0])
        if canonical is None:
            # Invalid path: use current language for the error text.
            with self.scoped_db_read() as conn:
                current = get_response_language(conn)
            await self._safe_reply(
                update,
                t("cmd.lang_invalid", lang=_ui_locale(current)),
                message_kind="command_reply",
            )
            return

        with self.scoped_db_write() as conn:
            set_response_language(conn, canonical)
        clear_ui_language_cache()
        key = _LANG_SET_KEYS.get(canonical, "cmd.lang_set_zh")
        # Confirm in the *target* language so /lang en is fully English, etc.
        await self._safe_reply(
            update,
            t(key, lang=_ui_locale(canonical)),
            message_kind="command_reply",
        )
