"""Presentation guidance for agents rendered in the narrow dashboard drawer."""

from __future__ import annotations


def build_web_response_style(*, resource_type: str) -> dict[str, str]:
    """Return compact, evidence-safe response guidance for a web agent."""
    subject = "support ticket" if resource_type == "support" else "project"
    evidence_rule = (
        "Ground factual claims in the ticket context or retrieved evidence. "
        if resource_type == "support"
        else "Ground factual claims in project context or retrieved evidence. "
    )
    return {
        "role": "system",
        "content": (
            "<web-chat-response-style>"
            f"Your response is displayed in a narrow dashboard drawer for this {subject}. "
            "Answer the operator's question in the first sentence. "
            "Use short paragraphs and compact bullet or numbered lists when useful. "
            "Do not use Markdown tables; use bold labels or bullets instead. "
            "Avoid repeating metadata already visible on the page unless it is relevant. "
            "Clearly separate verified facts from recommendations. "
            f"{evidence_rule}"
            "Never invent a sender, date, urgency, follow-up, action, or evidence detail. "
            "Keep routine answers concise and offer no more than three relevant next steps."
            "</web-chat-response-style>"
        ),
    }
