"""Explicit entry-point permissions for Telegram card callbacks.

The dispatcher normalizes CONFIRMED_ before this policy is consulted. Unknown
profile/action pairs are denied rather than inheriting another profile's rights.
"""

from __future__ import annotations

from src.auth.rbac import P_BUSINESS_WRITE, P_PERSONAL_WRITE, P_SHARED_READ
from src.telegram.callback_protocol import CallbackData, is_generic_cancel


# Actions are grouped by their *effects*, not button labels. In particular,
# dismissal of a shared card and initiation of a shared draft are business writes.
_BUSINESS_ACTIONS: dict[str, frozenset[str]] = {
    "A": frozenset({"APPROVE", "REJECT", "MODIFY", "EDIT"}),
    "B": frozenset({"ASSIGN"}),
    "TK": frozenset({"ASSIGN"}),
    "C": frozenset({"SEND", "CONFIRM_SEND", "EDIT", "DISCARD"}),
    "D": frozenset({"SEND", "CONFIRM_SEND", "EDIT", "DISCARD"}),
    "CP": frozenset({"SEND", "CONFIRM_SEND", "EDIT", "DISCARD"}),
    "E": frozenset({"CONFIRM", "MERGE", "IGNORE", "OVERRIDE", "FLAG"}),
    "F": frozenset({"CREATE", "CONFIRM", "DISCARD"}),
    "G": frozenset({
        "DONE", "SNOOZE", "PICK_OWNER", "REASSIGN", "CHANGE_OWNER",
        "DRAFT_EMAIL", "DRAFT EMAIL",
    }),
    # H uses the action token itself as a project ID; handled below.
    "H": frozenset(),
    "I": frozenset({"DISMISS", "SNOOZE 7DAYS", "SNOOZE_7DAYS"}),
    "IA": frozenset({"DISMISS"}),
    "J": frozenset({"POSTPONE", "ACK"}),
    "L": frozenset({
        "CONFIRM_UPDATE", "EDIT", "UPDATE_PROJECT", "DRAFT_REPLY",
        "UNDO", "REJECT",
    }),
    "M": frozenset({
        "SNOOZE_1DAY", "SNOOZE", "MARK_DONE", "ESCALATE", "ACK",
        "DRAFT_STATUS_EMAIL", "DRAFT STATUS EMAIL",
    }),
    "N": frozenset({"DISMISS"}),
    "O": frozenset({"DRAFT_EMAIL", "I_HANDLE", "ASK_MORE"}),
    "P": frozenset({
        "DRAFT_REPLY", "ESCALATE", "RECLASSIFY", "RECLASS_SELECT", "I_HANDLE",
    }),
    "Q": frozenset({"CONFIRM", "DISMISS"}),
    "CU": frozenset({"CONFIRM", "CANCEL"}),
    "BATCH": frozenset({"CONFIRM_ALL", "REJECT_ALL", "APPROVE", "REJECT", "EDIT"}),
    "ATTACH": frozenset({"SAVE"}),
    "PREF": frozenset({"YES"}),
}

_READ_ACTIONS: dict[str, frozenset[str]] = {
    "B": frozenset({"DETAILS"}),
    "TK": frozenset({"DETAILS"}),
    "C": frozenset({"DETAILS"}),
    "D": frozenset({"DETAILS"}),
    "CP": frozenset({"DETAILS"}),
    "I": frozenset({"RE-EVALUATE"}),
    "IA": frozenset({"VIEW_BRIEF"}),
    "J": frozenset({
        "OPEN CLOSING REVIEW", "OPEN_CLOSING_REVIEW",
        "COMPLETE CLOSING", "COMPLETE_CLOSING",
    }),
    "L": frozenset({
        "VIEW_FULL_REPLY", "DISMISS", "ATTACH_TO_TICKET",
        "📎_ATTACH_TO_TICKET", "📎 ATTACH TO TICKET",
    }),
    "N": frozenset({"RETRY"}),
    "ATTACH": frozenset({"PICK"}),
    "PREF": frozenset({"NO"}),
}

_PERSONAL_ACTIONS: dict[str, frozenset[str]] = {
    "K": frozenset({"CONFIRM_RUN", "MODIFY_FILTERS", "CANCEL"}),
}


def permission_for_callback(parsed: CallbackData | None) -> str | None:
    """Return the required RBAC permission, or None for unsupported callbacks."""
    if parsed is None or (
        parsed.profile not in _BUSINESS_ACTIONS
        and parsed.profile not in _PERSONAL_ACTIONS
    ):
        return None
    if is_generic_cancel(parsed):
        return P_SHARED_READ  # Dispatcher returns without calling a handler.
    if parsed.profile == "H":
        return P_BUSINESS_WRITE  # Project IDs are dynamic action tokens.
    if parsed.action in _BUSINESS_ACTIONS.get(parsed.profile, ()):
        return P_BUSINESS_WRITE
    if parsed.action in _PERSONAL_ACTIONS.get(parsed.profile, ()):
        return P_PERSONAL_WRITE
    if parsed.action in _READ_ACTIONS.get(parsed.profile, ()):
        return P_SHARED_READ
    return None
