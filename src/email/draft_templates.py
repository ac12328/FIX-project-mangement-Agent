"""
Template-based instant drafts — no LLM needed.

Uses string.Template.safe_substitute so missing variables render as
"[N/A]" instead of raising KeyError.  No raw ${...} braces remain.
"""

from __future__ import annotations

import re
from string import Template
from typing import Any


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------

_TEMPLATES: dict[str, dict[str, str]] = {
    "follow_up_stale": {
        "description": "Follow up on a stale project with no response",
        "subject": "Follow-up: $project_name — FIX Onboarding Status",
        "body": (
            "Dear $contact_name,\n\n"
            "I hope this message finds you well. It has been $days_waiting days "
            "since our last correspondence regarding the FIX onboarding for "
            "$project_name.\n\n"
            "Could you kindly provide an update on the current status? "
            "If there are any blockers on your side, please let us know so we "
            "can assist.\n\n"
            "Looking forward to your reply.\n\n"
            "Best regards,\n"
            "$sender_name"
        ),
    },
    "request_credentials": {
        "description": "Request UAT credentials from counterparty",
        "subject": "Request: UAT Credentials for $project_name",
        "body": (
            "Dear $contact_name,\n\n"
            "We are ready to proceed with UAT testing for $project_name. "
            "Could you please provide the following UAT environment credentials "
            "at your earliest convenience?\n\n"
            "- Host / IP address\n"
            "- Port\n"
            "- CompID / SenderCompID\n"
            "- Password (if applicable)\n\n"
            "Please share these details securely. If you have any questions "
            "about the setup requirements, feel free to reach out.\n\n"
            "Best regards,\n"
            "$sender_name"
        ),
    },
    "request_test_report": {
        "description": "Request conformance test report from counterparty",
        "subject": "Request: Conformance Test Report — $project_name",
        "body": (
            "Dear $contact_name,\n\n"
            "We have completed the conformance test cases for $project_name. "
            "Could you please share the test report so we can proceed to the "
            "next milestone?\n\n"
            "If there are any outstanding issues from your testing, please "
            "let us know and we will address them promptly.\n\n"
            "Best regards,\n"
            "$sender_name"
        ),
    },
    "remind_deadline": {
        "description": "Remind counterparty of an approaching deadline",
        "subject": "Reminder: Upcoming Deadline — $project_name",
        "body": (
            "Dear $contact_name,\n\n"
            "This is a friendly reminder that the deadline for "
            "$milestone_name on the $project_name onboarding is "
            "$deadline_date.\n\n"
            "Please ensure all required deliverables are submitted before "
            "the deadline. Let us know if you need any assistance.\n\n"
            "Best regards,\n"
            "$sender_name"
        ),
    },
    "acknowledge_receipt": {
        "description": "Acknowledge receipt of information or documents",
        "subject": "Re: $original_subject — Received",
        "body": (
            "Dear $contact_name,\n\n"
            "Thank you for sending over the $item_description for "
            "$project_name. We have received it and will review shortly.\n\n"
            "We will follow up if anything further is needed from your side.\n\n"
            "Best regards,\n"
            "$sender_name"
        ),
    },
}

# Pre-compiled pattern to detect leftover raw ${...} or $identifier
_RAW_VAR_RE = re.compile(r"\$\{?\w+\}?")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_template_names() -> list[str]:
    """Return sorted list of all registered template names."""
    return sorted(_TEMPLATES.keys())


def get_template(name: str) -> dict[str, str] | None:
    """Return template dict (subject, body, description) or None if unknown."""
    tpl = _TEMPLATES.get(name)
    if tpl is None:
        return None
    # Return a copy so callers cannot mutate the registry
    return dict(tpl)


def render_template(name: str, context: dict[str, Any] | None = None) -> dict[str, str] | None:
    """Render a template with the given context variables.

    Missing variables are replaced with ``[N/A]``.
    Returns ``{"subject": ..., "body": ...}`` or ``None`` if unknown template.
    """
    tpl = _TEMPLATES.get(name)
    if tpl is None:
        return None

    ctx = context or {}

    subject = _safe_render(tpl["subject"], ctx)
    body = _safe_render(tpl["body"], ctx)

    return {"subject": subject, "body": body}


def match_template(*, task_type: str, **kwargs: Any) -> str | None:
    """Try to auto-select a template name based on task metadata.

    Returns template name or ``None`` if no rule matches (e.g. complex
    tasks like ``reply_to_client`` that need LLM reasoning).
    """
    # Complex task types that always need LLM
    _COMPLEX_TYPES = {"reply_to_client", "answer_status", "analysis"}
    if task_type in _COMPLEX_TYPES:
        return None

    if task_type == "draft_follow_up":
        days_waiting = kwargs.get("days_waiting", 0)
        if days_waiting >= 3:
            return "follow_up_stale"

    if task_type == "request_credentials":
        return "request_credentials"

    if task_type == "request_test_report":
        return "request_test_report"

    if task_type == "remind_deadline":
        return "remind_deadline"

    if task_type == "acknowledge_receipt":
        return "acknowledge_receipt"

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_render(template_str: str, context: dict[str, Any]) -> str:
    """Render *template_str* with safe_substitute, then replace any
    leftover ``$variable`` tokens with ``[N/A]``.
    """
    result = Template(template_str).safe_substitute(context)
    # Replace any remaining $var or ${var} with [N/A]
    result = _RAW_VAR_RE.sub("[N/A]", result)
    return result
