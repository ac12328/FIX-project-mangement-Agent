"""LLM-visible read-only tools for verified FIX session records."""
from __future__ import annotations

from src.tools.contracts import ToolContract

GET_PROJECT_FIX_SESSIONS = ToolContract(
    name="get_project_fix_sessions",
    description="Get verified current FIX session configurations for a project. Never returns plaintext secrets.",
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}, "environment": {"type": "string", "enum": ["UAT", "PRODUCTION"]}}, "required": ["project_id"]},
    output_schema={"type": "object", "properties": {"sessions": {"type": "array"}, "pending_count": {"type": "integer"}}},
    allowed_task_types=["*"], allowed_contexts=["*"], side_effect_type="read", timeout_seconds=10,
)
GET_FIX_SESSION_HISTORY = ToolContract(
    name="get_fix_session_history",
    description="Get version history for one verified FIX session without plaintext secrets.",
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}, "environment": {"type": "string", "enum": ["UAT", "PRODUCTION"]}, "session_key": {"type": "string", "default": "order_entry"}}, "required": ["project_id", "environment"]},
    output_schema={"type": "object", "properties": {"history": {"type": "array"}}},
    allowed_task_types=["*"], allowed_contexts=["*"], side_effect_type="read", timeout_seconds=10,
)
GET_PENDING_FIX_SESSION_CANDIDATES = ToolContract(
    name="get_pending_fix_session_candidates",
    description="List unverified FIX configuration candidates awaiting operator confirmation.",
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"candidates": {"type": "array"}}},
    allowed_task_types=["*"], allowed_contexts=["*"], side_effect_type="read", timeout_seconds=10,
)
GET_FIX_ENVIRONMENT_RESOURCES = ToolContract(
    name="get_fix_environment_resources",
    description="Get approved environment resources such as distributable UAT price-feed URLs. Secret resources are never revealed.",
    input_schema={"type": "object", "properties": {"project_id": {"type": "string"}, "environment": {"type": "string", "enum": ["UAT", "PRODUCTION"]}}, "required": ["project_id"]},
    output_schema={"type": "object", "properties": {"resources": {"type": "array"}}},
    allowed_task_types=["*"], allowed_contexts=["*"], side_effect_type="read", timeout_seconds=10,
)
FIX_SESSION_TOOLS = [GET_PROJECT_FIX_SESSIONS, GET_FIX_SESSION_HISTORY, GET_PENDING_FIX_SESSION_CANDIDATES, GET_FIX_ENVIRONMENT_RESOURCES]
