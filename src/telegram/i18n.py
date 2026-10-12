"""
Centralised i18n module for Telegram-facing strings.

Usage:
    from src.telegram.i18n import t
    t("review.approved_applied").format(project_id="HK-2025", applied="ball_with")

Language follows operator ``response_language`` (set via /lang):
  - zh-TW / zh-CN → Traditional Chinese strings (zh-CN reuses zh-TW UI pack)
  - en → English strings

Fallback chain: requested locale → zh-TW → key itself (makes missing keys visible).
"""

from __future__ import annotations

import logging
import time
from typing import Mapping

logger = logging.getLogger(__name__)

# key → {locale → text}
_TRANSLATIONS: dict[str, dict[str, str]] = {}

# Short-lived cache so every t() call does not hit SQLite.
_lang_cache: str | None = None
_lang_cache_mono: float = 0.0
_LANG_CACHE_TTL_SEC = 2.0
# Process-local override for tests / single-shot evaluation (None = read DB).
_lang_override: str | None = None

_UI_LOCALES = frozenset({"zh-TW", "en"})


def clear_ui_language_cache() -> None:
    """Invalidate cached UI language (call after /lang or API language set)."""
    global _lang_cache, _lang_cache_mono
    _lang_cache = None
    _lang_cache_mono = 0.0


def set_ui_language_override(lang: str | None) -> None:
    """Force UI language without DB (tests). Pass None to clear override."""
    global _lang_override
    if lang is not None and lang not in _UI_LOCALES and lang != "en":
        if lang == "zh-CN":
            lang = "zh-TW"
        elif lang not in _UI_LOCALES:
            lang = "zh-TW"
    _lang_override = lang
    clear_ui_language_cache()


def get_ui_language() -> str:
    """Return active UI locale for Telegram strings: 'zh-TW' or 'en'."""
    global _lang_cache, _lang_cache_mono
    if _lang_override in _UI_LOCALES:
        return _lang_override

    now = time.monotonic()
    if _lang_cache is not None and (now - _lang_cache_mono) < _LANG_CACHE_TTL_SEC:
        return _lang_cache

    lang = "zh-TW"
    try:
        from src.db.connection import scoped_read
        from src.repositories.operator_state_repo import get_response_language

        with scoped_read() as conn:
            raw = get_response_language(conn)
        if raw == "en":
            lang = "en"
        else:
            # zh-TW and zh-CN share the Traditional Chinese UI pack for now.
            lang = "zh-TW"
    except Exception:
        logger.debug("get_ui_language: falling back to zh-TW", exc_info=True)
        lang = "zh-TW"

    _lang_cache = lang
    _lang_cache_mono = now
    return lang


def t(key: str, lang: str | None = None) -> str:
    """Look up a translation by key for the active (or forced) UI language."""
    locale = lang if lang in _UI_LOCALES else (lang if lang else get_ui_language())
    if locale not in _UI_LOCALES:
        # Allow explicit zh-CN callers; map to zh-TW pack.
        locale = "zh-TW" if locale != "en" else "en"

    entry = _TRANSLATIONS.get(key)
    if not entry:
        return key
    if locale in entry:
        return entry[locale]
    if "zh-TW" in entry:
        return entry["zh-TW"]
    # Any remaining locale value
    return next(iter(entry.values()), key)


def button_display(english_label: str) -> str:
    """Translate a button label for display. Callback data stays English."""
    if get_ui_language() == "en":
        return english_label
    from src.telegram.i18n_cards import BUTTON_DISPLAY
    return BUTTON_DISPLAY.get(english_label, english_label)


def _register(strings: Mapping[str, str], locale: str = "zh-TW") -> None:
    """Merge a domain dict into the global translations for one locale."""
    for key, text in strings.items():
        bucket = _TRANSLATIONS.setdefault(key, {})
        bucket[locale] = text


def _register_en(strings: Mapping[str, str]) -> None:
    _register(strings, "en")


# Auto-load all domain sub-modules on import
from src.telegram import i18n_cards as _cards  # noqa: E402
from src.telegram import i18n_commands as _cmds  # noqa: E402
from src.telegram import i18n_callbacks as _cbs  # noqa: E402
from src.telegram import i18n_scheduler as _sched  # noqa: E402
from src.telegram import i18n_portfolio as _port  # noqa: E402
from src.telegram import i18n_en as _en  # noqa: E402

_register(_cards.STRINGS, "zh-TW")
_register(_cmds.STRINGS, "zh-TW")
_register(_cbs.STRINGS, "zh-TW")
_register(_sched.STRINGS, "zh-TW")
_register(_port.STRINGS, "zh-TW")
_register_en(_en.STRINGS_EN)
