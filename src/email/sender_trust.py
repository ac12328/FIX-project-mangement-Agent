"""Sender trust boundary for inbound email agent paths."""

from __future__ import annotations

import re

from src.wiki.domain_index import get_wiki_domain_index

_SENDER_LINE_RE = re.compile(r"^From:\s*(\S+)", re.MULTILINE | re.IGNORECASE)


def extract_sender_from_inbound_text(user_text: str) -> str | None:
    """Parse ``From:`` line from ``[INBOUND EMAIL]`` / ``[INBOUND REPLY]`` agent text."""
    match = _SENDER_LINE_RE.search(user_text or "")
    return match.group(1).strip().lower() if match else None


def sender_domain(sender_email: str) -> str:
    email_lower = (sender_email or "").lower()
    return email_lower.split("@")[-1] if "@" in email_lower else ""


def is_internal_company_sender(sender_email: str | None) -> bool:
    if not sender_email:
        return False
    domain = sender_domain(sender_email)
    return bool(domain) and get_wiki_domain_index().is_internal(domain)


def sender_org_type(sender_email: str | None) -> str:
    if not sender_email:
        return "unknown"
    if is_internal_company_sender(sender_email):
        return "internal"
    return "client"


def build_sender_trust_context(sender_email: str | None) -> str:
    """Fenced block injected into email specialist context."""
    if not sender_email:
        return ""
    org = sender_org_type(sender_email)
    if org == "internal":
        return (
            "<sender-trust>\n"
            f"sender_email: {sender_email}\n"
            "sender_org_type: internal\n"
            "trust_level: pm_forward — operator relaying client material\n"
            "rule: The PM is NOT the client. Analyze forwarded log/content and you MAY "
            "propose next_action, current_milestone, notes when the forwarded material "
            "clearly supports it. Never propose ball_with — only a direct client reply "
            "can move the ball. Do not treat the PM's send as a client commitment.\n"
            "</sender-trust>"
        )
    return (
        "<sender-trust>\n"
        f"sender_email: {sender_email}\n"
        "sender_org_type: client\n"
        "trust_level: client_inbound — field updates allowed when clearly evidenced\n"
        "</sender-trust>"
    )


def evidence_summary_for_sender(sender_email: str, subject: str, *, max_subject: int = 120) -> str:
    """Attribute evidence to PM forward vs client inbound."""
    subj = (subject or "")[:max_subject]
    if is_internal_company_sender(sender_email):
        return f"PM forward from {sender_email}: {subj}"
    return f"Inbound email from {sender_email}: {subj}"