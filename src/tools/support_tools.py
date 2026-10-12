"""
Support ticket tool contracts — chat-only tools for managing support tickets.
"""

from src.tools.contracts import ToolContract


CHAT_CREATE_TICKET = ToolContract(
    name="create_ticket",
    description=(
        "Create a new support ticket for a technical issue. "
        "Category: fix_session, network, cert, trade, post_trade, config, access, outreach, other. "
        "Severity: critical, high, medium, low."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short summary of the issue"},
            "category": {
                "type": "string",
                "description": "fix_session | network | cert | trade | post_trade | config | access | other",
            },
            "severity": {
                "type": "string",
                "description": "critical | high | medium | low (default: medium)",
            },
            "description": {"type": "string", "description": "Detailed description of the issue"},
            "counterparty": {"type": "string", "description": "Affected counterparty name"},
            "related_project_id": {"type": "string", "description": "Project ID if related to a project"},
        },
        "required": ["title", "category"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=15,
)

CHAT_UPDATE_TICKET = ToolContract(
    name="update_ticket",
    description=(
        "Update fields on an existing support ticket. "
        "Valid fields: status (open/investigating/resolved/closed), severity, "
        "assigned_team, escalated_to, root_cause, resolution, description."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string", "description": "The support ticket ID (e.g. ST-20260403-001)"},
            "field": {"type": "string", "description": "Field to update"},
            "value": {"type": "string", "description": "New value"},
        },
        "required": ["ticket_id", "field", "value"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=15,
)

CHAT_SEARCH_TICKETS = ToolContract(
    name="search_tickets",
    description=(
        "Search support tickets by text (title, description, counterparty), "
        "status, or category. Returns matching tickets."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Free text search"},
            "status": {"type": "string", "description": "Filter by status: open, investigating, resolved, closed"},
            "category": {"type": "string", "description": "Filter by category"},
            "limit": {"type": "integer", "description": "Max results (default 10)"},
        },
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="read",
    timeout_seconds=10,
)

CHAT_GET_ADVISORY = ToolContract(
    name="get_advisory",
    description=(
        "Get context-aware advisory for a support ticket or project. "
        "Returns recommended next steps, escalation suggestions, and similar past cases."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string", "description": "Support ticket ID to advise on"},
            "project_id": {"type": "string", "description": "Project ID to advise on (if no ticket)"},
        },
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="read",
    query_safe=True,
    timeout_seconds=15,
)

CHAT_RECORD_RESOLUTION = ToolContract(
    name="record_resolution",
    description=(
        "Record how a support issue was resolved. Extracts a reusable resolution pattern "
        "and stores it for future reference. Optionally links to a ticket."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "resolution_text": {
                "type": "string",
                "description": "Description of how the issue was resolved",
            },
            "ticket_id": {"type": "string", "description": "Support ticket to link resolution to"},
            "category": {
                "type": "string",
                "description": "Category if no ticket: fix_session | network | cert | trade | post_trade | config | access | other",
            },
        },
        "required": ["resolution_text"],
    },
    output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=15,
)


OUTREACH_EMAIL_DRAFT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "to_email": {"type": "string", "description": "Recipient email address"},
        "counterparty": {"type": "string", "description": "Potential client / company name"},
        "subject": {"type": "string", "description": "Email subject line"},
        "body": {"type": "string", "description": "Email body text (plain text)"},
        "cc": {
            "type": "array",
            "items": {"type": "string"},
            "description": "CC email addresses (optional)",
        },
        "doc_key": {
            "type": "string",
            "description": "Reference doc to attach (e.g. fix_spec_connectivity_acceptance). Omit for no attachment.",
        },
    },
    "required": ["to_email", "counterparty", "subject", "body"],
}

OUTREACH_EMAIL_DRAFT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "text": {"type": "string"},
        "draft_id": {"type": "string"},
        "ticket_id": {"type": "string"},
        "outcome": {"type": "string"},
        "committed": {"type": "boolean"},
        "error": {"type": "string"},
    },
}

CREATE_OUTREACH_EMAIL_DRAFT = ToolContract(
    name="create_outreach_email_draft",
    description=(
        "Create a pre-sales outreach email draft and an OUTREACH support ticket for audit. "
        "This tool never sends email. The operator must review and approve the draft in "
        "Dashboard before the separate approved-draft sending workflow can deliver it. "
        "No project ID is required. Call list_reference_docs first when a reference "
        "document may need to be attached."
    ),
    input_schema=OUTREACH_EMAIL_DRAFT_INPUT_SCHEMA,
    output_schema=OUTREACH_EMAIL_DRAFT_OUTPUT_SCHEMA,
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
    timeout_seconds=30,
)

# Backward-compatible execution alias only. It remains registered so saved/replayed
# tool calls still work, but it is hidden from the model because the old name falsely
# implied an external send. A future real send primitive should be named
# `send_approved_outreach_email` and require an approved draft/action boundary.
SEND_OUTREACH_EMAIL_DEPRECATED_ALIAS = ToolContract(
    name="send_outreach_email",
    description=(
        "Deprecated alias for create_outreach_email_draft. Creates a draft only; "
        "it does not send email."
    ),
    input_schema=OUTREACH_EMAIL_DRAFT_INPUT_SCHEMA,
    output_schema=OUTREACH_EMAIL_DRAFT_OUTPUT_SCHEMA,
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    llm_visible=False,
    side_effect_type="write",
    timeout_seconds=30,
)


SUPPORT_TOOLS: list[ToolContract] = [
    CHAT_CREATE_TICKET,
    CHAT_UPDATE_TICKET,
    CHAT_SEARCH_TICKETS,
    CHAT_GET_ADVISORY,
    CHAT_RECORD_RESOLUTION,
    CREATE_OUTREACH_EMAIL_DRAFT,
]

# Execution-only aliases remain registered separately so canonical tool bundles
# and model-visible support tool counts are not polluted by deprecated names.
SUPPORT_TOOL_ALIASES: list[ToolContract] = [
    SEND_OUTREACH_EMAIL_DEPRECATED_ALIAS,
]
