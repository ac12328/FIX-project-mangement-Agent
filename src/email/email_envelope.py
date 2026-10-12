"""Parse and classify inbound email envelope (From/To/Cc) for ownership intent."""

from __future__ import annotations

import logging
import sqlite3
from email.utils import getaddresses
from typing import Any

from src.email.inbox_fetcher import AttachmentData, FetchedEmail
from src.email.sender_trust import is_internal_company_sender, sender_domain
from src.triage.client_emails import is_vendor_email

logger = logging.getLogger(__name__)

# Cap extracted attachment text fed into the LLM.
_MAX_EXTRACTED_CHARS_PER_FILE = 6000
_MAX_EXTRACTED_CHARS_TOTAL = 12000


def parse_address_list(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for _name, addr in getaddresses([header_value]):
        email = (addr or "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            out.append(email)
    return out


def classify_address_org(email: str, conn: sqlite3.Connection | None = None) -> str:
    if is_internal_company_sender(email):
        return "internal"
    if is_vendor_email(email, conn):
        return "vendor"
    domain = sender_domain(email)
    if domain:
        return "client"
    return "unknown"


def build_envelope_context(
    fetched: FetchedEmail,
    *,
    project_id: str,
    conn: sqlite3.Connection | None = None,
) -> str:
    to_emails = list(getattr(fetched, "to_emails", None) or [])
    cc_emails = parse_address_list(getattr(fetched, "cc", None))
    sender = (fetched.sender_email or "").lower()
    sender_org = classify_address_org(sender, conn)

    def _line(email: str, role: str) -> str:
        return f"  - {role}: {email} ({classify_address_org(email, conn)})"

    lines = [
        "<email-envelope>",
        f"project_id: {project_id}",
        f"from: {sender} (sender_org: {sender_org})",
        "to:",
    ]
    lines.extend(_line(e, "to") for e in to_emails)
    if cc_emails:
        lines.append("cc:")
        lines.extend(_line(e, "cc") for e in cc_emails)
    lines.append(
        "rule: ball_with Internal means Demo Connectivity acts next. "
        "Client-domain to/cc recipients indicate client-internal coordination — "
        "not a ball transfer to Demo Connectivity unless artifacts are delivered to us."
    )
    lines.append("</email-envelope>")
    return "\n".join(lines)


def build_attachment_context(
    attachments: list[AttachmentData] | list[Any] | None,
) -> str:
    """Build LLM context from attachment filenames/types + extractable text.

    Always lists filenames so the model can distinguish conformance reports
    from network screenshots even when content cannot be extracted (e.g. xlsx).
    """
    if not attachments:
        return ""

    from src.email.attachment_extractor import can_extract, extract_text_from_bytes

    lines = ["<attachments>"]
    total_extracted = 0
    for att in attachments:
        filename = getattr(att, "filename", None) or "unknown"
        content_type = getattr(att, "content_type", None) or "application/octet-stream"
        size_bytes = int(getattr(att, "size_bytes", 0) or 0)
        data = getattr(att, "data", None) or b""
        lines.append(f"- {filename} ({content_type}, {size_bytes} bytes)")

        if not data or not can_extract(content_type, filename):
            continue
        if total_extracted >= _MAX_EXTRACTED_CHARS_TOTAL:
            lines.append("  (further extracted text omitted — budget reached)")
            break
        try:
            result = extract_text_from_bytes(data, content_type, filename)
        except Exception:
            logger.debug("Attachment extract failed for %s", filename, exc_info=True)
            continue
        if not result.get("success") or not result.get("text"):
            if result.get("error"):
                lines.append(f"  extract_error: {result['error']}")
            continue
        budget = min(
            _MAX_EXTRACTED_CHARS_PER_FILE,
            _MAX_EXTRACTED_CHARS_TOTAL - total_extracted,
        )
        text = (result["text"] or "").strip()
        if not text:
            continue
        if len(text) > budget:
            text = text[: budget - 1] + "…"
        total_extracted += len(text)
        lines.append(f'  <extracted-text filename="{filename}">')
        lines.append(text)
        lines.append("  </extracted-text>")

    lines.append(
        "rule: Prefer attachment filenames and extracted content over older quoted "
        "thread snippets when classifying network vs conformance vs other evidence. "
        "Names like *conformance*, *test result*, *test report* indicate M5 evidence "
        "even if the body quotes historical telnet/IP connectivity."
    )
    lines.append("</attachments>")
    return "\n".join(lines)


def build_inbound_analysis_text(
    *,
    project_id: str,
    sender_email: str,
    subject: str,
    body: str,
    fetched: FetchedEmail | None = None,
    conn: sqlite3.Connection | None = None,
    tag: str = "[INBOUND EMAIL]",
) -> str:
    """Build user text for inbound email analysis with envelope + attachments."""
    envelope = ""
    att_block = ""
    if fetched is not None:
        envelope = build_envelope_context(fetched, project_id=project_id, conn=conn)
        att_block = build_attachment_context(getattr(fetched, "attachments", None))
    analysis_ref = (
        str(getattr(fetched, "email_id", "") or "").strip()
        if fetched is not None else ""
    )
    parts = [tag, f"Project: {project_id}"]
    if analysis_ref:
        parts.append(f"Analysis ref: {analysis_ref}")
    parts.extend([f"From: {sender_email}", f"Subject: {subject}"])
    if envelope:
        parts.extend(["", envelope])
    if att_block:
        parts.extend(["", att_block])
    parts.extend(["", body])
    return "\n".join(parts)