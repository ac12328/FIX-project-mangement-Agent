"""Separate committed email updates from Telegram delivery outcomes."""
from collections.abc import Mapping
from typing import Any

AUTO_APPLIED_SENT = "auto_applied_undo_card"
AUTO_APPLIED_NOTIFICATION_PENDING = "auto_applied_notification_pending"
AUTO_APPLIED_NOTIFICATION_UNKNOWN = "auto_applied_notification_unknown"
COMMITTED_UPDATE_OUTCOMES = frozenset({
    AUTO_APPLIED_SENT, AUTO_APPLIED_NOTIFICATION_PENDING,
    AUTO_APPLIED_NOTIFICATION_UNKNOWN, "direct_write", "pct_completed",
})


def committed_notification_outcome(result: Any) -> str:
    """Only explicit adapter receipts prove send success.

Pre-send configuration/budget refusals are unsent. Other failures may have
occurred after remote acceptance and must not be blindly resent.
"""
    if not isinstance(result, Mapping):
        return AUTO_APPLIED_NOTIFICATION_UNKNOWN
    if result.get("sent") is True and result.get("telegram_message_id") is not None:
        return AUTO_APPLIED_SENT
    if result.get("sent") is False and result.get("error") in {
        "telegram_not_configured", "budget_exhausted", "bot_identity_changed",
    }:
        return AUTO_APPLIED_NOTIFICATION_PENDING
    return AUTO_APPLIED_NOTIFICATION_UNKNOWN
