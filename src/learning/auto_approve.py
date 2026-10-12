"""Shared auto-approve gate for learned rules.

Canonical injectable statuses after resolution:
  approved | pending_review | rejected | archived
Legacy ``active`` is not injectable and should be normalized by migration.
"""

from __future__ import annotations

import re
import sqlite3

AUTO_APPROVE_MIN_CONF = 0.85

_POLICY_HINT = re.compile(
    r"\b(tag\s*\d+|port|reject|park|pre-?order|pct|session|milestone|"
    r"heartbeat|conformance|firewall|whitelist|tls|nnab|ftp|"
    r"grey\s*market|production\s*ports?|time\s*slots?)\b",
    re.I,
)
_ONE_OFF_FACT = re.compile(
    r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|account\s*#?\s*\d{5,})\b",
    re.I,
)

PREFERRED_TRIGGERS = frozenset({
    "post_send_learning",
    "operator_feedback",
    "discard_learning",
    "procedure",
    "preference",
    "exception",
    "warning",
})


def should_auto_approve(
    *,
    rule: str,
    confidence: float,
    trigger_type: str | None = None,
    condition: str | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason) for whether a new rule may be auto-approved."""
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        return False, "invalid_confidence"
    if conf < AUTO_APPROVE_MIN_CONF:
        return False, "confidence_below_threshold"
    text = f"{condition or ''} {rule or ''}"
    if _ONE_OFF_FACT.search(text):
        return False, "one_off_fact"
    if len((rule or "").strip()) < 24:
        return False, "rule_too_short"
    if not _POLICY_HINT.search(text) and (trigger_type or "") not in PREFERRED_TRIGGERS:
        return False, "no_policy_hint"
    # Preferred triggers still need some substance; allow if policy hint OR preferred
    if not _POLICY_HINT.search(text) and (trigger_type or "") in PREFERRED_TRIGGERS:
        # Soft pass for explicit durable trigger types with long enough rule text
        if len((rule or "").strip()) >= 40:
            return True, "ok_preferred_trigger"
        return False, "no_policy_hint"
    return True, "ok"


def resolve_rule_status(
    conn: sqlite3.Connection,
    *,
    rule: str,
    confidence: float,
    trigger_type: str | None = None,
    condition: str | None = None,
) -> tuple[str, str | None]:
    """Resolve insert status.

    Returns:
      ("merge", existing_rule_id) — caller should merge_into_existing and skip insert
      ("approved", None) — auto-approve
      ("pending_review", None) — needs operator review
    """
    from src.repositories.learned_rules_repo import find_similar_rule

    similar = find_similar_rule(conn, rule)
    if similar:
        return "merge", similar["rule_id"]
    ok, _reason = should_auto_approve(
        rule=rule,
        confidence=confidence,
        trigger_type=trigger_type,
        condition=condition,
    )
    return ("approved" if ok else "pending_review"), None
