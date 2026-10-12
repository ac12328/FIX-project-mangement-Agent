"""
draft_generator.py — Shared LLM email draft generation.

Called by:
  - draft_engine.generate_draft_for_signal()  (proactive/scheduled path)
  - executor._handle_draft_reply()             (operator chat path)

Returns (subject, body, to_email). Raises LLMParseFailure on bad LLM output.
"""
import sqlite3
from dataclasses import dataclass
from functools import lru_cache

from config.settings import BASE_DIR
from src.context.engine import ContextEngine
from src.context.layer1_truth import load_project_truth
from src.context.layer3_relationship import load_relationship
from src.email.envelope import resolve_draft_envelope
from src.llm.client import LLMParseFailure, llm_call_structured

@dataclass(frozen=True)
class GeneratedDraft:
    subject: str
    body: str
    to_email: str | None
    trace_id: str | None = None
    output_revision_id: str | None = None

    def __iter__(self):
        yield self.subject
        yield self.body
        yield self.to_email


_SIGNAL_PREFIXES: dict[str, str] = {
    "inbound_reply": (
        "A client has just replied to your email. "
        "Draft a professional response that addresses their message."
    ),
    "stale_external": (
        "The client has been silent for several days. "
        "Draft a polite, concise follow-up email."
    ),
    "overdue_followup": (
        "An internal follow-up task is overdue. "
        "Draft a reminder email to the external party to obtain the outstanding item."
    ),
    "sla_deadline": (
        "The project go-live deadline is approaching. "
        "Draft a status update email to the client covering current milestone progress."
    ),
    "b2b_launch_date_request": (
        "Production FIX session is online and PCT is complete. "
        "Draft a professional email asking the client to propose go-live date(s) "
        "per market so the B2B team can coordinate the production launch."
    ),
    "internal_follow_up": (
        "Draft a concise standalone internal operational request. "
        "State the requested action directly and preserve all supplied technical identifiers."
    ),
}

_DRAFT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "description": "Email subject line"},
        "body": {"type": "string", "description": "Full email body text"},
        "confidence": {"type": "number"},
    },
    "required": ["subject", "body"],
}


@lru_cache(maxsize=1)
def _load_email_agent_prompt() -> str:
    return (BASE_DIR / "prompts" / "email_agent.md").read_text(encoding="utf-8")


def generate_email_draft(
    conn: sqlite3.Connection,
    project_id: str,
    signal_type: str,
    context_hints: dict,
    *,
    recipient_override: dict | None = None,
    operator_directive: str | None = None,
    validation_feedback: str | None = None,
) -> tuple[str, str, str | None]:
    """
    Generate an LLM-quality email draft for a given signal.

    Args:
        conn:               SQLite connection (read-only access needed)
        project_id:         Project to draft for
        signal_type:        One of inbound_reply | stale_external | overdue_followup | sla_deadline
        context_hints:      Signal-specific context dict
        recipient_override: If provided, {"to_email": ..., "contact_name": ...} overrides
                            envelope resolution for the To field.
        operator_directive: Trusted original operator request. This outranks background context.
        validation_feedback: Deterministic correction requirements for a single retry.

    Returns:
        (subject, body, to_email)  — to_email may be None if no contact found

    Raises:
        LLMParseFailure: if LLM returns empty subject or body
    """
    truth_obj, _ = load_project_truth(conn, project_id)
    if truth_obj is None:
        raise ValueError(f"Project {project_id} not found")
    envelope = resolve_draft_envelope(
        conn, project_id, for_reply=(signal_type == "inbound_reply")
    )

    if recipient_override:
        envelope.to_email = recipient_override.get("to_email") or envelope.to_email
        envelope.contact_name = recipient_override.get("contact_name") or envelope.contact_name

    # Load relationship data for tone preferences.
    # Resolve contact_id from the envelope's to_email via primary_email column.
    # Wrapped in try/except so tests with bare in-memory DBs still pass.
    contact_id: str | None = None
    if envelope.to_email:
        try:
            row = conn.execute(
                "SELECT contact_id FROM contacts WHERE primary_email = ? LIMIT 1",
                (envelope.to_email,),
            ).fetchone()
            if row:
                contact_id = row["contact_id"]
        except Exception:
            pass
    from src.context.governed_memory import compatibility_memory_request
    request = compatibility_memory_request(conn, operator_id=truth_obj.operator_id,
        project_id=project_id, contact_id=contact_id, surface="draft", task_type=signal_type)
    rel, _ = load_relationship(conn, contact_id, project_id, memory_request=request)
    draft_frame = None
    try:
        draft_frame = ContextEngine(conn).build_for_draft(
            project_id, contact_id, signal_type,
        )
    except sqlite3.Error:
        draft_frame = None

    signal_prefix = _SIGNAL_PREFIXES.get(
        signal_type,
        "Draft a professional email appropriate to the current project state.",
    )

    # Build the hints block
    hint_lines: list[str] = []
    if context_hints.get("reply_body"):
        hint_lines.append(f"Client reply:\n{context_hints['reply_body'][:800]}")
    if context_hints.get("background_context"):
        hint_lines.append(
            "BACKGROUND CONTEXT (supporting facts only; it cannot weaken the operator directive):\n"
            f"{context_hints['background_context'][:2000]}"
        )
    if context_hints.get("reply_subject"):
        hint_lines.append(f"Reply subject: {context_hints['reply_subject']}")
    if context_hints.get("days_stale"):
        hint_lines.append(f"Days since last activity: {context_hints['days_stale']}")
    if context_hints.get("followup_description"):
        hint_lines.append(f"Follow-up task: {context_hints['followup_description']}")
    if context_hints.get("days_overdue"):
        hint_lines.append(f"Days overdue: {context_hints['days_overdue']}")
    if context_hints.get("days_until_golive"):
        hint_lines.append(f"Days until go-live: {context_hints['days_until_golive']}")
    if context_hints.get("current_milestone"):
        hint_lines.append(f"Current milestone: {context_hints['current_milestone']}")

    # Suppress legacy tone only when this actual injection produced usable v2 memory.
    tone_parts: list[str] = []
    governed_draft_usable = bool(draft_frame and draft_frame.learned_memory)
    if not governed_draft_usable:
        if rel.preferred_language:
            tone_parts.append(f"Language: {rel.preferred_language}")
        if rel.preferred_greeting:
            tone_parts.append(f"Greeting: {rel.preferred_greeting}")
        if rel.response_length_preference:
            tone_parts.append(f"Length preference: {rel.response_length_preference}")

    user_content_parts = [signal_prefix]
    if operator_directive:
        user_content_parts.extend([
            "",
            "AUTHORITATIVE OPERATOR DIRECTIVE (highest priority; preserve action strength):",
            operator_directive.strip(),
            (
                "Draft the email so the recipient is directly asked to perform this action. "
                "Do not downgrade disable/block/revoke/allow instructions into investigate, "
                "check, review, advise, or monitor."
            ),
        ])
    if validation_feedback:
        user_content_parts.extend([
            "",
            "DETERMINISTIC VALIDATION FEEDBACK (correct every item):",
            validation_feedback.strip(),
        ])
    user_content_parts.extend([
        "",
        f"Project: {truth_obj.project_id} - {truth_obj.client_name or 'Unknown'}",
        f"Milestone: {truth_obj.current_milestone or 'Unknown'}",
        f"To: {envelope.to_email or 'Unknown'} ({envelope.contact_name or 'Contact'})",
    ])
    if hint_lines:
        user_content_parts.append("")
        user_content_parts.extend(hint_lines)
    if tone_parts:
        user_content_parts.append("")
        user_content_parts.append("Tone: " + "; ".join(tone_parts))
    if draft_frame and draft_frame.memory:
        user_content_parts.append("")
        user_content_parts.append(draft_frame.memory)

    # Containment: do not perform a second, unscoped wiki lookup here. Callers
    # must provide already-scoped, approved knowledge in context_hints.
    approved_knowledge = (context_hints.get("approved_knowledge") or "").strip()
    if approved_knowledge:
        user_content_parts.append("")
        user_content_parts.append(
            "Approved reference guidance (do not copy client-specific identifiers "
            "unless they are also present in this project's facts):\n"
            + approved_knowledge[:12000]
        )

    messages = [
        {"role": "system", "content": _load_email_agent_prompt()},
        {"role": "user", "content": "\n".join(user_content_parts)},
    ]
    project_operator = None
    candidate_scopes = ()
    if draft_frame:
        project_operator = conn.execute(
            "SELECT operator_id FROM projects WHERE project_id=?", (project_id,),
        ).fetchone()
        if project_operator and project_operator[0]:
            candidate_scopes = (draft_frame.memory_exposure_scope(
                operator_id=project_operator[0], surface="draft",
                task_type="email_draft", project_id=project_id,
            ),)

    from src.llm.client import StructuredLLMResponse
    from src.memory.exposure_context import inference_scope
    operator_id = project_operator[0] if draft_frame and project_operator else "default_operator"
    with inference_scope(
        operator_id=operator_id, surface="draft", task_type="email_draft",
        project_id=project_id, candidate_scopes=candidate_scopes,
    ):
        response = llm_call_structured(
            "email_draft", messages, _DRAFT_SCHEMA, return_metadata=True,
        )
    result = response.data if isinstance(response, StructuredLLMResponse) else response
    subject = (result.get("subject") or "").strip()
    body = (result.get("body") or "").strip()

    if not subject or not body:
        raise LLMParseFailure("LLM returned empty subject or body for draft")

    link = response.inference if isinstance(response, StructuredLLMResponse) else None
    return GeneratedDraft(
        subject, body, envelope.to_email,
        trace_id=link.trace_id if link else None,
        output_revision_id=link.output_revision_id if link else None,
    )


def generate_adhoc_email_draft(
    *,
    to_email: str,
    context: str,
    contact_name: str | None = None,
    subject_hint: str | None = None,
    operator_directive: str | None = None,
    validation_feedback: str | None = None,
) -> tuple[str, str, str]:
    """Generate a project-less / ticket-less email draft.

    Used for operator handoffs (e.g. send docs to a colleague) that must not
    inherit sticky project thread envelopes.
    """
    to_email = (to_email or "").strip()
    if not to_email or "@" not in to_email:
        raise ValueError("to_email is required for ad-hoc drafts")

    who = contact_name or to_email
    hint_block = (context or "").strip() or "Professional email as requested by the operator."
    user_content_parts = [
        "Draft a professional standalone email (not a reply to a client thread).",
        "This email is NOT tied to any FIX onboarding project or support ticket.",
        "Do not invent project IDs, ticket IDs, or client names unless the "
        "operator directive or supporting context explicitly includes them.",
        "",
        f"To: {who} <{to_email}>",
    ]
    if operator_directive:
        user_content_parts.extend([
            "",
            "AUTHORITATIVE OPERATOR DIRECTIVE (highest priority; preserve action strength):",
            operator_directive.strip(),
            (
                "Ask the recipient to perform the requested action directly. Do not downgrade "
                "disable/block/revoke/allow instructions into investigate, check, review, "
                "advise, or monitor."
            ),
            "",
            "BACKGROUND CONTEXT (supporting facts only):",
            hint_block[:2000],
        ])
    else:
        user_content_parts.extend(["", f"Operator instructions:\n{hint_block[:2000]}"])
    if validation_feedback:
        user_content_parts.extend([
            "",
            "DETERMINISTIC VALIDATION FEEDBACK (correct every item):",
            validation_feedback.strip(),
        ])
    if subject_hint:
        user_content_parts.extend([
            "",
            f"Preferred subject direction: {subject_hint[:200]}",
        ])
    user_content = "\n".join(user_content_parts)

    messages = [
        {"role": "system", "content": _load_email_agent_prompt()},
        {"role": "user", "content": user_content},
    ]
    from config.settings import CONFIG
    from src.llm.client import StructuredLLMResponse
    from src.memory.exposure_context import inference_scope
    with inference_scope(
        operator_id=CONFIG.operator_id or "default_operator",
        surface="draft",
        task_type="email_draft",
    ):
        response = llm_call_structured(
            "email_draft", messages, _DRAFT_SCHEMA, return_metadata=True,
        )
    result = response.data if isinstance(response, StructuredLLMResponse) else response
    subject = (result.get("subject") or "").strip()
    body = (result.get("body") or "").strip()
    if not subject or not body:
        raise LLMParseFailure("LLM returned empty subject or body for ad-hoc draft")
    link = response.inference if isinstance(response, StructuredLLMResponse) else None
    return GeneratedDraft(
        subject, body, to_email,
        trace_id=link.trace_id if link else None,
        output_revision_id=link.output_revision_id if link else None,
    )
