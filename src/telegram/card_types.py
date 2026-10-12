"""
WS-4: Card state text helpers for three-phase callback handling.

Cards pass through three states:
  ACTIVE    → buttons shown (normal send)
  EXECUTING → ⏳ Processing... appended, buttons removed
  RESOLVED  → ✅ result appended
  ERRORED   → ❌ error appended
"""

# Canonical 6 logical card type names (len == 6, values == 6 unique types)
CARD_TYPE_NAMES: dict[str, str] = {
    "ReviewCard": "ReviewCard",
    "DraftCard": "DraftCard",
    "AlertCard": "AlertCard",
    "WorkflowCard": "WorkflowCard",
    "AdvisoryCard": "AdvisoryCard",
    "BatchCard": "BatchCard",
}

# Internal: profile letter → logical card type
_PROFILE_TO_TYPE: dict[str, str] = {
    "A": "ReviewCard",
    "B": "ReviewCard",
    "E": "ReviewCard",
    "F": "ReviewCard",
    "PREF": "ReviewCard",
    "C": "DraftCard",
    "D": "DraftCard",
    "G": "WorkflowCard",
    "H": "WorkflowCard",
    "J": "WorkflowCard",
    "K": "WorkflowCard",
    "I": "AlertCard",
    "IA": "AlertCard",
    "L": "AlertCard",
    "M": "AlertCard",
    "N": "AlertCard",
    "O": "AdvisoryCard",
    "P": "AdvisoryCard",
    "Q": "WorkflowCard",
    "CP": "DraftCard",
    "BATCH": "BatchCard",
    "ATTACH": "WorkflowCard",
}


def card_type_for_profile(profile: str) -> str:
    """Return the logical card type name for a given profile letter."""
    return _PROFILE_TO_TYPE.get(profile, "ReviewCard")


def executing_state(card_text: str) -> str:
    """Append executing indicator to existing card text (buttons already removed by edit_text)."""
    from src.telegram.i18n import t
    return card_text.rstrip() + t("card.executing")


def resolved_state(card_text: str, result_text: str) -> str:
    """Append resolved result to existing card text."""
    return card_text.rstrip() + f"\n\n✅ {result_text}"


def errored_state(card_text: str) -> str:
    """Append error indicator to existing card text."""
    from src.telegram.i18n import t
    return card_text.rstrip() + t("card.errored")
