"""Support ticket evidence tool contracts — on-demand log/email snippet reads."""

from src.tools.contracts import ToolContract

GET_TICKET_EVIDENCE = ToolContract(
    name="get_ticket_evidence",
    description=(
        "Load evidence items for a support ticket (emails, log snippets, notes, resolutions). "
        "Use when investigating a support issue or reading client-provided FIX logs."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string", "description": "Support ticket ID (e.g. ST-20260403-001)"},
            "evidence_type": {
                "type": "string",
                "description": "Optional filter: email | note | log_snippet | screenshot | resolution",
            },
            "limit": {"type": "integer", "description": "Max items (default 10)"},
        },
        "required": ["ticket_id"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    query_safe=True,
    timeout_seconds=10,
)

SEARCH_TICKET_EVIDENCE = ToolContract(
    name="search_ticket_evidence",
    description=(
        "Search support ticket evidence by keyword (log lines, email bodies, notes). "
        "Prefer log_snippet and email types when troubleshooting FIX sessions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keyword or phrase to search"},
            "ticket_id": {
                "type": "string",
                "description": "Optional: restrict to one ticket",
            },
            "evidence_type": {
                "type": "string",
                "description": "Optional filter: email | log_snippet | note | resolution",
            },
            "limit": {"type": "integer", "description": "Max results (default 8)"},
        },
        "required": ["query"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["*"],
    side_effect_type="read",
    query_safe=True,
    timeout_seconds=10,
)

EVIDENCE_TOOLS = [GET_TICKET_EVIDENCE, SEARCH_TICKET_EVIDENCE]