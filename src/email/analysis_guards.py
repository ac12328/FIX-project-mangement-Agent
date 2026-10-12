"""Post-LLM guards for inbound email analysis (confidence + auto-apply safety)."""

from __future__ import annotations

import re

from src.email.body_cleaner import clean_email_body

_ORDER_REJECTION_RE = re.compile(
    r"instrument record not found|instrument not found|order rejected|order rejection"
    r"|3999:|ordstatus\s*[:=]\s*8|exec type\s*[:=]\s*8",
    re.IGNORECASE,
)

# Above auto_write threshold (0.95) but below perfect auto-apply (1.00).
_PERFECT_CONFIDENCE_CAP = 0.96
_INTERNAL_HANDOFF_TO_EXTERNAL_INTENT = "pm_handoff_to_external"
_INTERNAL_HANDOFF_ACTION = (
    r"(?:try|test|retest|retry|confirm|verify|connect|telnet|"
    r"proceed(?:\s+with)?|take\s+(?:the\s+)?(?:pct|production connectivity test))"
)
_INTERNAL_HANDOFF_RE = re.compile(
    rf"(?:\b(?:please|kindly)\s+{_INTERNAL_HANDOFF_ACTION}\b|"
    rf"\b(?:can|could|would|will|may)\s+you\s+(?:please\s+)?"
    rf"{_INTERNAL_HANDOFF_ACTION}\b|"
    rf"\byou(?:r\s+(?:team|side))?\s+"
    rf"(?:(?:can|may|could|should)\s+|need(?:s)?\s+to\s+|"
    rf"are\s+(?:now\s+)?able\s+to\s+)"
    rf"{_INTERNAL_HANDOFF_ACTION}\b)",
    re.IGNORECASE,
)

_INTENT_BLOCKS_INTERNAL_BALL = frozenset({
    "client_internal_delegation",
    "client_fyi",
    "ambiguous",
    "client_commitment",
})

_DELEGATION_RE = re.compile(
    r"轉發|转发|forward(?:ed)?\s+to|cc['\"]?\s+my|內部同事|内部同事|"
    r"internal\s+colleague|交畀\s*IT|交给\s*IT",
    re.IGNORECASE,
)


def email_reports_order_rejection(body: str | None) -> bool:
    if not body:
        return False
    return bool(_ORDER_REJECTION_RE.search(body))


def email_explicitly_hands_off_to_external(body: str | None) -> bool:
    """Return whether the newest authored text asks the recipient to act next."""
    authored_text = clean_email_body(body)
    return bool(authored_text and _INTERNAL_HANDOFF_RE.search(authored_text))


def adjust_email_analysis_result(
    body: str | None,
    confidence: float,
    proposed_fields: dict,
    *,
    sender_email: str = "",
    email_intent: str = "",
    has_attachments: bool = False,
    assessment=None,
    project_id: str | None = None,
    source_ref: str | None = None,
) -> tuple[float, dict]:
    """Cap confidence and soften risky field proposals for rejection / PM-forward emails."""
    from src.email.sender_trust import is_internal_company_sender

    adjusted = dict(proposed_fields)
    from src.email.assessment_ownership import evaluated_owner
    proven_owner = evaluated_owner(assessment, project_id=project_id, source_ref=source_ref,
        body=body, proposed_fields=proposed_fields)

    if email_reports_order_rejection(body):
        confidence = min(confidence, _PERFECT_CONFIDENCE_CAP)
        # Client sent rejection logs — ball usually stays External until they retry.
        if adjusted.get("ball_with") == "Internal":
            adjusted.pop("ball_with", None)

    intent = (email_intent or "").strip()
    if intent in _INTENT_BLOCKS_INTERNAL_BALL:
        adjusted.pop("ball_with", None)
        if intent in ("client_internal_delegation", "ambiguous"):
            confidence = min(confidence, _PERFECT_CONFIDENCE_CAP)
    elif (
        not has_attachments
        and _DELEGATION_RE.search(body or "")
        and adjusted.get("ball_with") == "Internal"
        and not is_internal_company_sender(sender_email)
    ):
        adjusted.pop("ball_with", None)
        confidence = min(confidence, _PERFECT_CONFIDENCE_CAP)

    # Internal forwards may update progress fields but do not transfer ownership.
    # Preserve only an explicit internally-authored external handoff.
    if is_internal_company_sender(sender_email) and not (
        intent == _INTERNAL_HANDOFF_TO_EXTERNAL_INTENT
        and adjusted.get("ball_with") == "External"
        and email_explicitly_hands_off_to_external(body)
    ):
        adjusted.pop("ball_with", None)

    if proven_owner is not None:
        adjusted["ball_with"] = proven_owner
    return confidence, adjusted


_WIKI_QUERY_TOPICS = re.compile(
    r"\b(?:FIX|UAT|PCT|conformance|test(?:ing|s)?|report|reject(?:ed|ion)?|"
    r"logon|heartbeat|session|mapping|routing|throttle|CompID|completed|passed|"
    r"TLS|IP|port|Demo Provisioning|ER|Tag\s*\d+)\b|"
    r"\u6e2c\u8a66|\u6d4b\u8bd5|\u62d2\u7d55|\u62d2\u7edd|"
    r"\u767b\u5165|\u767b\u9304|\u767b\u5f55|\u9023\u7dda|\u8fde\u63a5|"
    r"\u5b8c\u6210|\u5831\u544a|\u62a5\u544a|\u8def\u7531|\u8c41\u514d",
    re.IGNORECASE,
)


def wiki_query_for_email_body(body: str | None, project_id: str) -> str:
    """Preserve authored FIX facts instead of inventing a rejection diagnosis.

    The project ID is scope metadata supplied by the caller, not a search keyword.
    This is a bounded retrieval query, never a replacement for source evidence.
    """
    authored = clean_email_body(body)
    if not authored:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+|[\u3002\uff01\uff1f]|\r?\n", authored)
    relevant = [line.strip() for line in sentences if _WIKI_QUERY_TOPICS.search(line)]
    text = " ".join(dict.fromkeys(relevant)) if relevant else authored
    # Preserve both ends when the authored technical discussion exceeds budget.
    if len(text) > 2000:
        return text[:990] + " ... " + text[-990:]
    return text


_WIKI_MILESTONE_QUERY = "FIX onboarding milestone completion criteria"
_WORKFLOW_SIGNAL = re.compile(
    r"\b(?:milestone|PCT|conformance|test report|basic (?:application )?test(?:ing)?|"
    r"production session|FIX logon)\b|\u6e2c\u8a66|\u6d4b\u8bd5|\u7d50\u6848|\u7ed3\u6848",
    re.IGNORECASE,
)


def wiki_queries_for_email_body(body: str | None, project_id: str) -> tuple[str, ...]:
    """Separate milestone rules from symptoms instead of guessing a stage."""
    query = wiki_query_for_email_body(body, project_id)
    if not query:
        return ()
    if _WORKFLOW_SIGNAL.search(query):
        return (_WIKI_MILESTONE_QUERY, query)
    return (query,)
