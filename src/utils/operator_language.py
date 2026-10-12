"""Operator-facing response language helpers.

Reads ``operator_state.response_language`` (via /lang or preferences API)
and builds prompt fragments so agent prose, email analysis summaries, and
suggested_action text follow en / zh-TW / zh-CN instead of hardcoding 繁體中文.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

VALID_LANGUAGES = ("zh-TW", "en", "zh-CN")

# Short labels for embedding in prompts ("Reply in English", etc.)
_PROSE_LABEL: dict[str, str] = {
    "zh-TW": "繁體中文",
    "en": "English",
    "zh-CN": "简体中文",
}

_LANG_POLICY: dict[str, str] = {
    "zh-TW": (
        "OPERATOR LANGUAGE POLICY (overrides any earlier language rules in this "
        "conversation): Always reply in 繁體中文 (Traditional Chinese). Never reply "
        "in English for prose. Domain jargon (FIX, milestone codes, enum values) "
        "may stay English inside Chinese sentences."
    ),
    "en": (
        "OPERATOR LANGUAGE POLICY (overrides any earlier language rules in this "
        "conversation): Always reply in English. Do not use Chinese for operator-facing "
        "prose. Domain jargon (FIX, milestone codes, enum values) stays English."
    ),
    "zh-CN": (
        "OPERATOR LANGUAGE POLICY (overrides any earlier language rules in this "
        "conversation): Always reply in 简体中文 (Simplified Chinese). Never reply "
        "in English for prose. Domain jargon (FIX, milestone codes, enum values) "
        "may stay English inside Chinese sentences."
    ),
}

# Operator-facing summary / suggested_action instructions (email analysis).
_SUMMARY_ACTION_RULE: dict[str, str] = {
    "zh-TW": (
        "All operator-facing `summary` and `suggested_action` text MUST be in 繁體中文. "
        "Domain jargon (FIX, conformance, milestone codes) may stay English inside Chinese sentences."
    ),
    "en": (
        "All operator-facing `summary` and `suggested_action` text MUST be in English. "
        "Do not write Chinese prose. Domain jargon (FIX, conformance, milestone codes) stays English."
    ),
    "zh-CN": (
        "All operator-facing `summary` and `suggested_action` text MUST be in 简体中文. "
        "Domain jargon (FIX, conformance, milestone codes) may stay English inside Chinese sentences."
    ),
}


def normalize_response_language(lang: str | None) -> str:
    """Return a valid language code; default zh-TW."""
    if lang in VALID_LANGUAGES:
        return lang  # type: ignore[return-value]
    return "zh-TW"


def read_response_language(conn: sqlite3.Connection | None = None) -> str:
    """Load operator response_language; default zh-TW when DB unavailable."""
    from src.repositories.operator_state_repo import get_response_language

    if conn is not None:
        try:
            return normalize_response_language(get_response_language(conn))
        except Exception:
            logger.debug("read_response_language(conn) failed", exc_info=True)
            return "zh-TW"
    try:
        from src.db.connection import scoped_read

        with scoped_read() as rconn:
            return normalize_response_language(get_response_language(rconn))
    except Exception:
        logger.debug("read_response_language() failed; defaulting to zh-TW", exc_info=True)
        return "zh-TW"


def prose_label(lang: str | None = None) -> str:
    """Short language name for prompt snippets (English / 繁體中文 / …)."""
    code = normalize_response_language(lang if lang is not None else read_response_language())
    return _PROSE_LABEL.get(code, _PROSE_LABEL["zh-TW"])


def operator_language_instruction(lang: str | None = None) -> str:
    """Full OPERATOR LANGUAGE POLICY block for system messages."""
    code = normalize_response_language(lang if lang is not None else read_response_language())
    return _LANG_POLICY.get(code, _LANG_POLICY["zh-TW"])


def summary_action_language_rule(lang: str | None = None) -> str:
    """Rule for email analysis summary + suggested_action language."""
    code = normalize_response_language(lang if lang is not None else read_response_language())
    return _SUMMARY_ACTION_RULE.get(code, _SUMMARY_ACTION_RULE["zh-TW"])


def final_reply_directive(lang: str | None = None, *, extra: str = "") -> str:
    """Build 'Final reply: <lang>, …' fragment for guards."""
    label = prose_label(lang)
    base = f"Final reply: {label}"
    if extra:
        return f"{base}, {extra}"
    return base


def reply_in_directive(lang: str | None = None) -> str:
    """Build 'Reply in <lang>' / 'Reply <lang>' fragment."""
    return f"Reply in {prose_label(lang)}"
