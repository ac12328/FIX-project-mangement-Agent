from src.tools.contracts import ToolContract
from src.models.internal_action import OWNER_TEAMS

_TEAMS_ENUM = list(OWNER_TEAMS)

CHAT_CREATE_INTERNAL_ACTION = ToolContract(
    name="create_internal_action",
    description=(
        "Create an internal action item for a team (IT, Compliance, Demo Provisioning Support, etc.). "
        "Use when the PM needs to track an internal task with a deadline — e.g. "
        "'IT Network needs to whitelist client IP by Friday' or "
        "'Compliance must approve counterparty by end of week'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "What the team needs to do (concise, actionable)",
            },
            "owner_team": {
                "type": "string",
                "enum": _TEAMS_ENUM,
                "description": "Which internal team owns this action",
            },
            "project_id": {
                "type": "string",
                "description": "Project ID this action is related to (optional)",
            },
            "deadline": {
                "type": "string",
                "description": "ISO 8601 date when this must be done e.g. '2026-06-01' (optional)",
            },
        },
        "required": ["title", "owner_team"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
)

CHAT_LIST_INTERNAL_ACTIONS = ToolContract(
    name="list_internal_actions",
    description=(
        "List internal action items. Filter by project or status. "
        "Use when the PM asks 'what's pending with IT?' or "
        "'any outstanding internal tasks for project X?'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "Filter by project ID (optional)",
            },
            "status": {
                "type": "string",
                "enum": ["open", "done", "cancelled"],
                "description": "Filter by status (default: open)",
            },
        },
        "required": [],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="read",
)

CHAT_COMPLETE_INTERNAL_ACTION = ToolContract(
    name="complete_internal_action",
    description=(
        "Mark an internal action item as done or cancelled. "
        "Use when the PM confirms IT has whitelisted the IP, "
        "Compliance has approved, etc."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action_id": {
                "type": "string",
                "description": "The action_id to update (starts with 'ia_')",
            },
            "outcome": {
                "type": "string",
                "enum": ["done", "cancelled"],
                "description": "'done' if completed, 'cancelled' if no longer needed",
            },
        },
        "required": ["action_id", "outcome"],
    },
    output_schema={"type": "object"},
    allowed_task_types=["*"],
    allowed_contexts=["chat"],
    side_effect_type="write",
)

INTERNAL_ACTION_TOOLS = [
    CHAT_CREATE_INTERNAL_ACTION,
    CHAT_LIST_INTERNAL_ACTIONS,
    CHAT_COMPLETE_INTERNAL_ACTION,
]
